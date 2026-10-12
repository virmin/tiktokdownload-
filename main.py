"""Telegram downloader with subscription management, designed for Render Web Service webhook."""
import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlsplit

import asyncpg
import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
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


def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='👤 Мой профиль', callback_data='profile')],
        [InlineKeyboardButton(text='📥 Как скачать', callback_data='help')],
    ])


async def register(user):
    async with pool.acquire() as conn:
        await conn.execute('''INSERT INTO users(user_id, username, first_name, joined_at)
            VALUES ($1,$2,$3,$4) ON CONFLICT(user_id) DO UPDATE
            SET username=EXCLUDED.username, first_name=EXCLUDED.first_name''',
            user.id, user.username or '', user.first_name or '', int(time.time()))


async def get_user(uid):
    async with pool.acquire() as conn:
        return await conn.fetchrow('SELECT expires_at, downloads FROM users WHERE user_id=$1', uid)


async def allowed(uid):
    if admin(uid):
        return True
    data = await get_user(uid)
    return bool(data and data['expires_at'] > int(time.time()))


def expiry(uid, row):
    if admin(uid):
        return '♾ Бессрочная (администратор)'
    if not row or row['expires_at'] <= int(time.time()):
        return '❌ Не активна'
    return '✅ До ' + datetime.fromtimestamp(row['expires_at'], timezone.utc).strftime('%d.%m.%Y %H:%M UTC')


async def profile(user):
    row = await get_user(user.id)
    name = html.escape(user.first_name or 'пользователь')
    handle = '@' + html.escape(user.username) if user.username else 'не указан'
    return (f'👋 Привет, <b>{name}</b>!\n\n👤 <b>Твой профиль</b>\n'
            f'🆔 ID: <code>{user.id}</code>\n🔖 Username: {handle}\n'
            f'💎 Подписка: {expiry(user.id, row)}\n'
            f'📥 Скачиваний: {row["downloads"] if row else 0}\n\n'
            + ('Отправь ссылку на TikTok или YouTube Shorts.' if await allowed(user.id)
               else '🔒 Для скачивания нужна подписка. Обратись к администратору.'))


@router.message(CommandStart())
@router.message(Command('profile'))
async def start(message: Message):
    await register(message.from_user)
    await message.answer(await profile(message.from_user), reply_markup=keyboard())


@router.callback_query(F.data == 'profile')
async def cb_profile(call: CallbackQuery):
    await register(call.from_user)
    await call.answer()
    await call.message.answer(await profile(call.from_user), reply_markup=keyboard())


@router.callback_query(F.data == 'help')
async def cb_help(call: CallbackQuery):
    await call.answer()
    await call.message.answer('📥 Отправь публичную ссылку на TikTok или YouTube Shorts. Требуется активная подписка.')


@router.message(Command('help'))
async def help_cmd(message: Message):
    await message.answer('Команды: /start, /profile, /help.\nАдмину: /grant ID ДНИ, /revoke ID, /users, /stats.')


@router.message(Command('grant'))
async def grant(message: Message):
    if not admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    parts = (message.text or '').split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit() or not 1 <= int(parts[2]) <= 3650:
        return await message.answer('Использование: <code>/grant 123456789 30</code>')
    uid, days = int(parts[1]), int(parts[2])
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Atomic extensions protect against simultaneous grant commands.
            await conn.execute('''INSERT INTO users(user_id,username,first_name,joined_at)
                VALUES($1,'','',$2) ON CONFLICT DO NOTHING''', uid, int(time.time()))
            row = await conn.fetchrow('SELECT expires_at FROM users WHERE user_id=$1 FOR UPDATE', uid)
            new_expiry = max(int(time.time()), row['expires_at']) + days * 86400
            await conn.execute('UPDATE users SET expires_at=$2 WHERE user_id=$1', uid, new_expiry)
    await message.answer(f'✅ Подписка <code>{uid}</code> продлена на {days} дней.\nДо {datetime.fromtimestamp(new_expiry, timezone.utc):%d.%m.%Y %H:%M} UTC')
    try:
        await bot.send_message(uid, '💎 Подписка активирована! Теперь можешь отправить ссылку на видео.')
    except Exception:
        pass


@router.message(Command('revoke'))
async def revoke(message: Message):
    if not admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    parts = (message.text or '').split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer('Использование: <code>/revoke 123456789</code>')
    uid = int(parts[1])
    if admin(uid):
        return await message.answer('У администратора бессрочный доступ.')
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET expires_at=0 WHERE user_id=$1', uid)
    await message.answer(f'✅ Подписка <code>{uid}</code> отозвана.')


@router.message(Command('users'))
async def users(message: Message):
    if not admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id,username,expires_at FROM users ORDER BY joined_at DESC LIMIT 20')
    if not rows:
        return await message.answer('Пользователей нет.')
    lines = ['👥 <b>Последние пользователи:</b>']
    for r in rows:
        status = '♾' if admin(r['user_id']) else ('✅' if r['expires_at'] > time.time() else '❌')
        lines.append(f'{status} <code>{r["user_id"]}</code> @{html.escape(r["username"] or "нет_username")}')
    await message.answer('\n'.join(lines))


@router.message(Command('stats'))
async def stats(message: Message):
    if not admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''SELECT COUNT(*) AS users, COALESCE(SUM(downloads),0) AS downloads,
          COUNT(*) FILTER (WHERE expires_at>$1) AS subs FROM users''', int(time.time()))
    await message.answer(f'📊 Пользователей: {row["users"]}\n💎 Активных платных подписок: {row["subs"]}\n📥 Загрузок: {row["downloads"]}')


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
        'restrictfilenames': True, 'js_runtimes': {'node': {}},
    }
    if is_tiktok:
        opts['impersonate'] = 'chrome'
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
    except Exception:
        log.exception('Download failed')
        if status:
            try:
                await status.edit_text('❌ Не удалось скачать видео. TikTok/YouTube могут ограничивать загрузку с серверов.')
            except Exception:
                pass
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@router.message(F.text)
async def receive_link(message: Message):
    await register(message.from_user)
    link = valid_link(message.text or '')
    if not link:
        return await message.answer('Отправь HTTPS-ссылку на TikTok или YouTube Shorts.')
    if not await allowed(message.from_user.id):
        return await message.answer('🔒 Подписка не активна. Обратись к администратору.')
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
