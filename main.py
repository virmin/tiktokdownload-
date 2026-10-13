"""Telegram TikTok/YouTube downloader with mandatory channel membership and admin editor."""
import asyncio
import base64
import binascii
import html
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, urlsplit

import asyncpg
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_IDS = {int(s.strip()) for s in os.getenv('ADMIN_IDS', '').split(',') if s.strip().isdigit()}
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
PUBLIC_URL = os.getenv('RENDER_EXTERNAL_URL', os.getenv('PUBLIC_URL', '')).strip().rstrip('/')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '').strip()
PORT = int(os.getenv('PORT', '10000'))
MAX_MB = max(1, min(49, int(os.getenv('MAX_FILE_MB', '48'))))
LIMIT = max(1, min(3, int(os.getenv('PARALLEL_DOWNLOADS', '1'))))
YOUTUBE_COOKIES_B64 = os.getenv('YOUTUBE_COOKIES_B64', '').strip()
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('shorts-render')
router = Router()
dp = Dispatcher()
dp.include_router(router)
semaphore = asyncio.Semaphore(LIMIT)
active_tasks: set[asyncio.Task] = set()
pool: asyncpg.Pool | None = None
bot: Bot | None = None
URL_PATTERN = re.compile(r'https?://[^\s<>"\']+', re.I)



def validate_database_url(dsn: str) -> None:
    """Reject malformed database URLs without logging credentials."""
    if not dsn.startswith(('postgresql://', 'postgres://')):
        raise RuntimeError('DATABASE_URL must start with postgresql:// (Supabase: Connect > Session pooler > URI)')
    authority = dsn.split('://', 1)[1].split('/', 1)[0].split('?', 1)[0]
    if '@' not in authority or ':' not in authority.split('@', 1)[0]:
        raise RuntimeError('DATABASE_URL must include PostgreSQL username and password')
    credentials, hostname_port = authority.rsplit('@', 1)
    password = credentials.split(':', 1)[1]
    if not password or 'YOUR-PASSWORD' in password.upper() or password.startswith('[') or password.endswith(']'):
        raise RuntimeError('DATABASE_URL: replace [YOUR-PASSWORD] with your actual Supabase database password, without square brackets. URL-encode special characters in the password.')
    try:
        parsed = urlsplit(dsn)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError('DATABASE_URL is malformed. Copy the Session pooler URI from Supabase and replace [YOUR-PASSWORD] without brackets; URL-encode special characters.') from None
    if not hostname or not hostname_port or not parsed.path.strip('/') or port is None:
        raise RuntimeError('DATABASE_URL must contain a host, port and database name (e.g. /postgres).')
    if hostname.startswith('db.') and hostname.endswith('.supabase.co'):
        log.warning('Using Supabase direct database host; Session pooler is recommended for Render Free')


def admin(uid):
    return uid in ADMIN_IDS


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def b(text, callback=None, url=None):
    return InlineKeyboardButton(text=text, callback_data=callback, url=url)


def keyboard(uid=None):
    rows = [[b('👤 Мой профиль', 'profile')],
            [b('📢 Обязательные каналы', 'channels')],
            [b('📥 Как скачать', 'help')]]
    if uid is not None and admin(uid):
        rows.append([b('⚙️ Управление каналами', 'admin:channels')])
    return kb(*rows)


async def register(user):
    async with pool.acquire() as conn:
        await conn.execute('''INSERT INTO users(user_id,username,first_name,joined_at)
            VALUES($1,$2,$3,$4) ON CONFLICT(user_id) DO UPDATE
            SET username=EXCLUDED.username,first_name=EXCLUDED.first_name''',
            user.id, user.username or '', user.first_name or '', int(time.time()))


async def get_user(uid):
    async with pool.acquire() as conn:
        return await conn.fetchrow('SELECT downloads FROM users WHERE user_id=$1', uid)


async def get_channels(active_only=True):
    async with pool.acquire() as conn:
        return await conn.fetch('''SELECT channel_id,title,invite_url,enabled FROM required_channels
            WHERE ($1::boolean = false OR enabled = true) ORDER BY added_at,channel_id''', active_only)


async def check_membership(uid):
    # Fail closed on API failures, except admins bypass channel checks.
    if admin(uid):
        return [], []
    missing = []
    failed = []
    for channel in await get_channels():
        try:
            member = await bot.get_chat_member(channel['channel_id'], uid)
            status = str(member.status)
            if status in ('left', 'kicked', 'banned') or (status == 'restricted' and not getattr(member, 'is_member', False)):
                missing.append(channel)
        except TelegramAPIError:
            log.warning('Channel membership verification failed for channel_id=%s', channel['channel_id'])
            failed.append(channel)
    return missing, failed


def join_keyboard(channels):
    rows = [[b('📢 ' + channel['title'][:40], url=channel['invite_url'])] for channel in channels]
    rows.append([b('✅ Проверить подписку', 'check:channels')])
    return kb(*rows)


async def gate_text(uid):
    missing, failed = await check_membership(uid)
    if not missing and not failed:
        return '✅ Подписка на обязательные каналы подтверждена. Отправь ссылку на TikTok или YouTube Shorts.', keyboard(uid), True
    if failed:
        text = ('⚠️ Не удалось проверить подписку в некоторых каналах. '
                'Попроси администратора бота убедиться, что бот добавлен администратором во все обязательные каналы.\n\n')
    else:
        text = '🔒 Для скачивания видео подпишись на все каналы ниже, затем нажми «Проверить подписку».\n\n'
    channels = list({c['channel_id']: c for c in missing + failed}.values())
    return text, join_keyboard(channels), False


async def profile(user):
    row = await get_user(user.id)
    channels = await get_channels()
    name = html.escape(user.first_name or 'пользователь')
    handle = '@' + html.escape(user.username) if user.username else 'не указан'
    return (f'👋 Привет, <b>{name}</b>!\n\n👤 <b>Твой профиль</b>\n'
            f'🆔 ID: <code>{user.id}</code>\n🔖 Username: {handle}\n'
            f'📢 Обязательных каналов: {len(channels)}\n'
            f'📥 Скачиваний: {row["downloads"] if row else 0}\n\n'
            'Использование бота бесплатное. Для загрузки нужно состоять во всех обязательных каналах.')


@router.message(CommandStart())
@router.message(Command('profile'))
async def start(message: Message):
    await register(message.from_user)
    welcome_text = await profile(message.from_user)
    banner_path = Path(__file__).with_name('banner.png')
    if banner_path.is_file():
        await message.answer_photo(
            photo=FSInputFile(banner_path),
            caption=welcome_text,
            reply_markup=keyboard(message.from_user.id),
        )
    else:
        log.warning('banner.png not found; using text-only welcome')
        await message.answer(welcome_text, reply_markup=keyboard(message.from_user.id))
    text, markup, ok = await gate_text(message.from_user.id)
    if not ok:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == 'profile')
async def cb_profile(call: CallbackQuery):
    await register(call.from_user)
    await call.answer()
    await call.message.answer(await profile(call.from_user), reply_markup=keyboard(call.from_user.id))


@router.callback_query(F.data.in_({'channels', 'check:channels'}))
async def cb_channels(call: CallbackQuery):
    await call.answer()
    text, markup, _ = await gate_text(call.from_user.id)
    await call.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == 'help')
async def cb_help(call: CallbackQuery):
    await call.answer()
    await call.message.answer('📥 Отправь публичную ссылку на TikTok или YouTube Shorts. Доступ бесплатный после подписки на обязательные каналы.')


@router.message(Command('help'))
async def help_cmd(message: Message):
    await message.answer('Команды: /start, /profile, /help. Админу: /admin, /stats, /users, /addchannel, /cancel.')


@router.message(Command('users'))
async def users(message: Message):
    if not admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id,username,downloads FROM users ORDER BY joined_at DESC LIMIT 20')
    if not rows:
        return await message.answer('Пользователей пока нет.')
    lines = ['👥 <b>Последние пользователи:</b>']
    for r in rows:
        lines.append(f'<code>{r["user_id"]}</code> @{html.escape(r["username"] or "нет")} · загрузок: {r["downloads"]}')
    await message.answer('\n'.join(lines))


@router.message(Command('stats'))
async def stats(message: Message):
    if not admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT COUNT(*) AS users, COALESCE(SUM(downloads),0) AS downloads FROM users')
    channels = await get_channels()
    await message.answer(f'📊 Пользователей: {row["users"]}\n📥 Загрузок: {row["downloads"]}\n📢 Обязательных каналов: {len(channels)}')


# Input state is held in memory; Render may restart, so admin can reopen the menu.
admin_inputs: dict[int, str] = {}


async def admin_channel_menu(chat_id):
    channels = await get_channels(False)
    rows = []
    for c in channels:
        icon = '✅' if c['enabled'] else '⏸'
        rows.append([b(f'{icon} {c["title"][:42]}', f'admin:ch:{c["channel_id"]}')])
    rows.append([b('➕ Добавить канал', 'admin:add')])
    rows.append([b('📊 Статистика', 'admin:stats')])
    rows.append([b('🏠 Главное меню', 'admin:back')])
    count = sum(bool(c['enabled']) for c in channels)
    await bot.send_message(chat_id, f'⚙️ <b>Обязательная подписка</b>\nВключено каналов: <b>{count}</b>.\nБот проверяет все включённые каналы.\n\nНажми на канал для изменения.', reply_markup=kb(*rows))


@router.message(Command('admin'))
async def admin_cmd(message: Message):
    if not admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    await admin_channel_menu(message.chat.id)


@router.callback_query(F.data == 'admin:channels')
async def admin_cb(call: CallbackQuery):
    if not admin(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    await call.answer()
    await admin_channel_menu(call.message.chat.id)


@router.callback_query(F.data == 'admin:back')
async def admin_back(call: CallbackQuery):
    if not admin(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    await call.answer()
    await call.message.answer('Главное меню', reply_markup=keyboard(call.from_user.id))


@router.callback_query(F.data == 'admin:stats')
async def admin_stats(call: CallbackQuery):
    if not admin(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    await call.answer()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT COUNT(*) AS users, COALESCE(SUM(downloads),0) AS downloads FROM users')
    await call.message.answer(f'📊 Пользователей: {row["users"]}\n📥 Загрузок: {row["downloads"]}')


@router.callback_query(F.data == 'admin:add')
async def admin_add(call: CallbackQuery):
    if not admin(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    await call.answer()
    admin_inputs[call.from_user.id] = 'add'
    await call.message.answer('➕ Отправь одной строкой ID канала и ссылку для вступления через пробел.\n\nПримеры:\n<code>@my_channel https://t.me/my_channel</code>\n<code>-1001234567890 https://t.me/+invitecode</code>\n\nБот должен быть администратором канала. /cancel — отмена.')


@router.message(Command('addchannel'))
async def addchannel_cmd(message: Message):
    if not admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    admin_inputs[message.from_user.id] = 'add'
    await message.answer('Отправь <code>@channel https://t.me/channel</code> или <code>-100ID https://t.me/+invite</code>. Для отмены /cancel.')


@router.message(Command('cancel'))
async def cancel_cmd(message: Message):
    admin_inputs.pop(message.from_user.id, None)
    await message.answer('Операция отменена.')


@router.callback_query(F.data.startswith('admin:ch:'))
async def admin_open_channel(call: CallbackQuery):
    if not admin(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    try:
        cid = int(call.data.rsplit(':', 1)[-1])
    except ValueError:
        return await call.answer('Некорректный ID')
    async with pool.acquire() as conn:
        channel = await conn.fetchrow('SELECT * FROM required_channels WHERE channel_id=$1', cid)
    if not channel:
        return await call.answer('Канал уже удалён')
    await call.answer()
    toggle = '⏸ Отключить' if channel['enabled'] else '✅ Включить'
    await call.message.answer(
        f'📢 <b>{html.escape(channel["title"])}</b>\nID: <code>{cid}</code>\nСтатус: {"включён" if channel["enabled"] else "отключён"}\nСсылка: {html.escape(channel["invite_url"])}',
        reply_markup=kb([b(toggle, f'admin:toggle:{cid}')],
                        [b('🔗 Изменить ссылку', f'admin:link:{cid}')],
                        [b('🗑 Удалить', f'admin:delask:{cid}')],
                        [b('⬅️ Каналы', 'admin:channels')]))


@router.callback_query(F.data.startswith('admin:toggle:'))
async def admin_toggle(call: CallbackQuery):
    if not admin(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    cid = int(call.data.split(':')[-1])
    async with pool.acquire() as conn:
        updated = await conn.fetchrow('UPDATE required_channels SET enabled=NOT enabled WHERE channel_id=$1 RETURNING enabled', cid)
    await call.answer('Изменено' if updated else 'Не найден')
    await admin_channel_menu(call.message.chat.id)


@router.callback_query(F.data.startswith('admin:link:'))
async def admin_change_link(call: CallbackQuery):
    if not admin(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    cid = int(call.data.split(':')[-1])
    admin_inputs[call.from_user.id] = f'link:{cid}'
    await call.answer()
    await call.message.answer('Отправь новую ссылку https://t.me/... для канала. /cancel — отмена.')


@router.callback_query(F.data.startswith('admin:delask:'))
async def admin_delask(call: CallbackQuery):
    if not admin(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    cid = int(call.data.split(':')[-1])
    await call.answer()
    await call.message.answer('Точно удалить этот обязательный канал?', reply_markup=kb(
        [b('🗑 Да, удалить', f'admin:delete:{cid}')], [b('Отмена', 'admin:channels')] ))


@router.callback_query(F.data.startswith('admin:delete:'))
async def admin_delete(call: CallbackQuery):
    if not admin(call.from_user.id):
        return await call.answer('Нет доступа', show_alert=True)
    cid = int(call.data.split(':')[-1])
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM required_channels WHERE channel_id=$1', cid)
    await call.answer('Удалено')
    await admin_channel_menu(call.message.chat.id)


def valid_channel_link(url):
    try:
        p = urlparse(url)
        return p.scheme == 'https' and (p.hostname or '').lower() in ('t.me', 'www.t.me') and bool(p.path.strip('/')) and not p.username and not p.password
    except ValueError:
        return False


async def process_admin_input(message):
    action = admin_inputs.get(message.from_user.id)
    if not action or not admin(message.from_user.id):
        return False
    text = (message.text or '').strip()
    if action == 'add':
        parts = text.split()
        if len(parts) != 2 or not valid_channel_link(parts[1]) or not (parts[0].startswith('@') or re.fullmatch(r'-100\d{5,}', parts[0])):
            await message.answer('Неверный формат. Пример: <code>@my_channel https://t.me/my_channel</code>')
            return True
        chat_ref = int(parts[0]) if parts[0].startswith('-100') else parts[0]
        try:
            chat = await bot.get_chat(chat_ref)
            me = await bot.get_me()
            bot_member = await bot.get_chat_member(chat.id, me.id)
            if str(bot_member.status) not in ('administrator', 'creator'):
                await message.answer('Сначала добавь бота администратором канала, затем повтори.')
                return True
            if chat.type != 'channel':
                await message.answer('Нужен именно Telegram-канал.')
                return True
        except TelegramAPIError:
            await message.answer('Не могу найти канал или проверить права бота. Добавь бота администратором.')
            return True
        async with pool.acquire() as conn:
            existing = await conn.fetchval('SELECT COUNT(*) FROM required_channels')
            present = await conn.fetchval('SELECT 1 FROM required_channels WHERE channel_id=$1', chat.id)
            if existing >= 30 and not present:
                await message.answer('Максимум 30 каналов. Удали ненужный канал.')
                return True
            await conn.execute('''INSERT INTO required_channels(channel_id,title,invite_url,added_at,enabled)
                VALUES($1,$2,$3,$4,true) ON CONFLICT(channel_id) DO UPDATE SET
                title=EXCLUDED.title,invite_url=EXCLUDED.invite_url,enabled=true''',
                chat.id, chat.title or str(chat.id), parts[1], int(time.time()))
        admin_inputs.pop(message.from_user.id, None)
        await message.answer(f'✅ Добавлен канал: {html.escape(chat.title or str(chat.id))}')
        await admin_channel_menu(message.chat.id)
        return True
    if action.startswith('link:'):
        if not valid_channel_link(text):
            await message.answer('Нужна ссылка на Telegram-канал вида https://t.me/...')
            return True
        cid = int(action.split(':', 1)[1])
        async with pool.acquire() as conn:
            await conn.execute('UPDATE required_channels SET invite_url=$2 WHERE channel_id=$1', cid, text)
        admin_inputs.pop(message.from_user.id, None)
        await message.answer('✅ Ссылка обновлена.')
        await admin_channel_menu(message.chat.id)
        return True
    return False


def valid_link(raw):
    match = URL_PATTERN.search(raw)
    if not match:
        return None
    url = match.group(0).rstrip('.,!?)]}')
    try:
        p = urlparse(url)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme != 'https' or p.username or p.password or p.port:
            return None
        if host in ('tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'):
            if p.path.startswith(('/@','/video/','/t/')) or host in ('vm.tiktok.com', 'vt.tiktok.com'):
                return url
        if host in ('youtube.com','www.youtube.com','m.youtube.com') and p.path.startswith('/shorts/'):
            return url
        if host == 'youtu.be' and p.path.strip('/'):
            return url
    except ValueError:
        pass
    return None


def download_video(url, folder):
    is_tiktok = 'tiktok.com' in (urlparse(url).hostname or '')
    opts = {
        'outtmpl': str(Path(folder) / '%(id)s.%(ext)s'),
        'format': 'bv*+ba/b', 'merge_output_format': 'mp4',
        'noplaylist': True, 'quiet': True, 'retries': 2, 'fragment_retries': 2,
        'socket_timeout': 20, 'max_filesize': MAX_MB * 1024 * 1024,
        'restrictfilenames': True, 'js_runtimes': {'node': {'path': '/usr/local/bin/node'}},
    }
    if is_tiktok:
        opts['impersonate'] = ImpersonateTarget(client='chrome')
    elif YOUTUBE_COOKIES_B64:
        # Render secrets are passed as base64 to avoid newline corruption.
        # Keep the cookie file private, temporary, and OUT of GitHub.
        try:
            raw = base64.b64decode(YOUTUBE_COOKIES_B64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError('YOUTUBE_COOKIES_B64 is not valid base64') from exc
        if len(raw) > 512 * 1024 or not raw.startswith((b'# Netscape HTTP Cookie File', b'# HTTP Cookie File')):
            raise RuntimeError('YouTube cookies must be a Netscape-format cookies.txt (<= 512 KiB)')
        cookie_file = Path(folder) / 'youtube_cookies.txt'
        cookie_file.write_bytes(raw)
        cookie_file.chmod(0o600)
        opts['cookiefile'] = str(cookie_file)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in ('.mp4','.webm','.mkv','.mov')]
    if not files:
        raise RuntimeError('media_not_found')
    return max(files, key=lambda p: p.stat().st_size)


async def perform_download(chat_id, uid, link):
    status = None
    folder = tempfile.mkdtemp(prefix='shorts_')
    try:
        status = await bot.send_message(chat_id, '⏳ Скачиваю видео...')
        async with semaphore:
            path = await asyncio.wait_for(asyncio.to_thread(download_video, link, folder), timeout=180)
            if path.stat().st_size > MAX_MB * 1024 * 1024:
                await status.edit_text(f'⚠️ Видео больше {MAX_MB} МБ. Попробуй другой ролик.')
                return
            await bot.send_video(chat_id, FSInputFile(path), caption='✅ Видео готово!', supports_streaming=path.suffix.lower()=='.mp4', request_timeout=180)
            async with pool.acquire() as conn:
                await conn.execute('UPDATE users SET downloads=downloads+1 WHERE user_id=$1', uid)
            await status.delete()
    except asyncio.TimeoutError:
        if status:
            await status.edit_text('⌛ Скачивание заняло слишком много времени. Попробуй позже.')
    except Exception as exc:
        # yt-dlp can include request details. Never log the cookie value itself.
        log.exception('Download failed')
        problem = str(exc).lower()
        if 'sign in to confirm' in problem or 'not a bot' in problem:
            error_text = ('❌ YouTube требует подтверждения пользователя для запросов с этого сервера. '
                          'Это ограничение YouTube, а не ошибка подписки. Попробуй позже.')
        else:
            error_text = '❌ Не удалось скачать видео. TikTok/YouTube могут ограничивать загрузку с серверов.'
        if status:
            try:
                await status.edit_text(error_text)
            except Exception:
                pass
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@router.message(F.text)
async def receive_link(message: Message):
    await register(message.from_user)
    if await process_admin_input(message):
        return
    link = valid_link(message.text or '')
    if not link:
        return await message.answer('Отправь HTTPS-ссылку на TikTok или YouTube Shorts.')
    missing, failed = await check_membership(message.from_user.id)
    if missing or failed:
        text, markup, _ = await gate_text(message.from_user.id)
        return await message.answer(text, reply_markup=markup)
    # Webhook must return quickly; video is processed in a separate tracked task.
    task = asyncio.create_task(perform_download(message.chat.id, message.from_user.id, link))
    active_tasks.add(task)
    task.add_done_callback(active_tasks.discard)


async def webhook(request: web.Request):
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != WEBHOOK_SECRET:
        return web.Response(status=403)
    try:
        raw = await request.json()
        update = Update.model_validate(raw, context={'bot': bot})
        await dp.feed_update(bot, update)
    except Exception:
        log.exception('Webhook handling error')
        return web.Response(status=500)
    return web.Response(text='ok')


async def health(_request):
    return web.Response(text='ok')


async def on_start(app):
    global pool, bot
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, command_timeout=15)
    async with pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users(
          user_id BIGINT PRIMARY KEY, username TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL DEFAULT '',
          joined_at BIGINT NOT NULL, expires_at BIGINT NOT NULL DEFAULT 0, downloads INTEGER NOT NULL DEFAULT 0)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS required_channels(
          channel_id BIGINT PRIMARY KEY, title TEXT NOT NULL,
          invite_url TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT true,
          added_at BIGINT NOT NULL)''')
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
    await bot.set_webhook(PUBLIC_URL + '/telegram/' + WEBHOOK_SECRET,
                          secret_token=WEBHOOK_SECRET, drop_pending_updates=False,
                          allowed_updates=dp.resolve_used_update_types())
    log.info('Webhook registered: %s/telegram/[hidden]', PUBLIC_URL)


async def on_stop(app):
    if active_tasks:
        done, pending = await asyncio.wait(active_tasks, timeout=5)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    if bot:
        await bot.session.close()
    if pool:
        await pool.close()
    # Do not delete webhook; Render can redeploy without a gap in configuration.


def main():
    if not TOKEN or not ADMIN_IDS or not DATABASE_URL or not PUBLIC_URL or not WEBHOOK_SECRET:
        raise RuntimeError('Set BOT_TOKEN, ADMIN_IDS, DATABASE_URL, WEBHOOK_SECRET, and PUBLIC_URL or RENDER_EXTERNAL_URL')
    validate_database_url(DATABASE_URL)
    if not PUBLIC_URL.startswith('https://'):
        raise RuntimeError('PUBLIC_URL must be https://...')
    if not re.fullmatch(r'[A-Za-z0-9_-]{12,256}', WEBHOOK_SECRET):
        raise RuntimeError('WEBHOOK_SECRET must be 12-256 ASCII letters, numbers, underscore or hyphen')
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get('/', health)
    app.router.add_get('/health', health)
    app.router.add_post('/telegram/' + WEBHOOK_SECRET, webhook)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    web.run_app(app, host='0.0.0.0', port=PORT, access_log=None)


if __name__ == '__main__':
    main()
