"""Subscription-based Telegram downloader for public TikTok and YouTube Shorts videos."""
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
from urllib.parse import urlparse

import aiosqlite
import yt_dlp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_IDS = {int(v.strip()) for v in os.getenv('ADMIN_IDS', '').split(',') if v.strip().isdigit()}
DATABASE = os.getenv('DB_FILE', 'data/bot.sqlite3')
MAX_MB = max(1, int(os.getenv('MAX_FILE_MB', '48')))
DOWNLOAD_LIMIT = max(1, int(os.getenv('PARALLEL_DOWNLOADS', '2')))
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('shorts-bot')
router = Router()
semaphore = asyncio.Semaphore(DOWNLOAD_LIMIT)
URL_PATTERN = re.compile(r'https?://[^\s<>"\']+', re.I)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='👤 Мой профиль', callback_data='profile')],
        [InlineKeyboardButton(text='📥 Как скачать', callback_data='help')],
    ])


async def init_db() -> None:
    Path(DATABASE).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at INTEGER NOT NULL, expires_at INTEGER NOT NULL DEFAULT 0, downloads INTEGER NOT NULL DEFAULT 0)')
        await db.commit()


async def register(user) -> None:
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute('INSERT INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name',
                         (user.id, user.username or '', user.first_name or '', int(time.time())))
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute('SELECT expires_at, downloads FROM users WHERE user_id=?', (user_id,)) as cur:
            return await cur.fetchone()


async def allowed(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    user = await get_user(user_id)
    return bool(user and user[0] > int(time.time()))


def expiry_text(user_id: int, user) -> str:
    if is_admin(user_id):
        return '♾ Бессрочная (администратор)'
    if not user or user[0] <= int(time.time()):
        return '❌ Не активна'
    return '✅ До ' + datetime.fromtimestamp(user[0], timezone.utc).strftime('%d.%m.%Y %H:%M UTC')


async def profile_text(user) -> str:
    data = await get_user(user.id)
    name = html.escape(user.first_name or 'пользователь')
    username = '@' + html.escape(user.username) if user.username else 'не указан'
    return (f'👋 Привет, <b>{name}</b>!\n\n'
            f'👤 <b>Твой профиль</b>\n'
            f'🆔 ID: <code>{user.id}</code>\n'
            f'🔖 Username: {username}\n'
            f'💎 Подписка: {expiry_text(user.id, data)}\n'
            f'📥 Скачиваний: {data[1] if data else 0}\n\n'
            + ('Отправь ссылку на TikTok или YouTube Shorts.' if await allowed(user.id) else
               'Для скачивания нужна подписка. Обратись к администратору.'))


@router.message(CommandStart())
async def cmd_start(message: Message):
    await register(message.from_user)
    await message.answer(await profile_text(message.from_user), reply_markup=keyboard())


@router.message(Command('profile'))
async def cmd_profile(message: Message):
    await register(message.from_user)
    await message.answer(await profile_text(message.from_user), reply_markup=keyboard())


@router.callback_query(F.data == 'profile')
async def callback_profile(call: CallbackQuery):
    await register(call.from_user)
    await call.answer()
    await call.message.answer(await profile_text(call.from_user), reply_markup=keyboard())


@router.callback_query(F.data == 'help')
async def callback_help(call: CallbackQuery):
    await call.answer()
    await call.message.answer('📥 Отправь мне публичную ссылку на ролик TikTok или YouTube Shorts.\n'
                              'Подписка должна быть активной. Некоторые ролики могут быть недоступны из-за ограничений платформы.')


@router.message(Command('help'))
async def cmd_help(message: Message):
    await message.answer('Команды: /start, /profile, /help.\nАдминистратору: /grant ID ДНИ, /revoke ID, /users, /stats.\n'
                         'Для загрузки отправь ссылку на публичный TikTok или YouTube Shorts.')


@router.message(Command('grant'))
async def cmd_grant(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return await message.answer('⛔ Команда доступна только администратору.')
    parts = (message.text or '').split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit() or not (1 <= int(parts[2]) <= 3650):
        return await message.answer('Использование: <code>/grant 123456789 30</code> (1–3650 дней)')
    uid, days = int(parts[1]), int(parts[2])
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute('SELECT expires_at FROM users WHERE user_id=?', (uid,)) as cur:
            existing = await cur.fetchone()
        expiry = max(int(time.time()), existing[0] if existing else 0) + days * 86400
        await db.execute('INSERT INTO users (user_id, username, first_name, joined_at, expires_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at',
                         (uid, '', '', int(time.time()), expiry))
        await db.commit()
    await message.answer(f'✅ Подписка пользователя <code>{uid}</code> продлена на {days} дн.\nДо {datetime.fromtimestamp(expiry, timezone.utc):%d.%m.%Y %H:%M} UTC')
    try:
        await bot.send_message(uid, '💎 Ваша подписка активирована или продлена. Отправьте ссылку на видео!')
    except Exception:
        pass  # User may not have started the bot yet


@router.message(Command('revoke'))
async def cmd_revoke(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer('⛔ Команда доступна только администратору.')
    parts = (message.text or '').split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer('Использование: <code>/revoke 123456789</code>')
    uid = int(parts[1])
    if is_admin(uid):
        return await message.answer('У администратора бессрочный доступ: отозвать его можно только изменив ADMIN_IDS.')
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute('UPDATE users SET expires_at=0 WHERE user_id=?', (uid,))
        await db.commit()
    await message.answer(f'✅ Подписка <code>{uid}</code> отозвана.')


@router.message(Command('users'))
async def cmd_users(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute('SELECT user_id, username, expires_at FROM users ORDER BY joined_at DESC LIMIT 20') as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.answer('Пользователей пока нет.')
    lines = ['👥 <b>Последние 20 пользователей:</b>']
    for uid, username, expiry in rows:
        status = '♾' if is_admin(uid) else ('✅' if expiry > int(time.time()) else '❌')
        lines.append(f'{status} <code>{uid}</code> @{html.escape(username) if username else "без_юзернейма"}')
    await message.answer('\n'.join(lines))


@router.message(Command('stats'))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer('⛔ Нет доступа.')
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute('SELECT COUNT(*), COALESCE(SUM(downloads), 0), SUM(CASE WHEN expires_at > ? THEN 1 ELSE 0 END) FROM users', (int(time.time()),)) as cur:
            users, downloads, subscribers = await cur.fetchone()
    await message.answer(f'📊 Пользователей: {users}\n💎 Активных подписок: {subscribers}\n📥 Успешных загрузок: {downloads}')


def valid_link(raw: str) -> str | None:
    """Only allow known video services, never arbitrary downloader URLs."""
    match = URL_PATTERN.search(raw)
    if not match:
        return None
    candidate = match.group(0).rstrip('.,!?)]}')
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or '').lower().rstrip('.')
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port:
            return None
        tiktok = host in ('tiktok.com', 'www.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com', 'm.tiktok.com')
        youtube = host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be')
        if tiktok and (parsed.path.startswith(('/@', '/t/', '/video/')) or host in ('vm.tiktok.com', 'vt.tiktok.com')):
            return candidate
        if youtube and (parsed.path.startswith('/shorts/') or host == 'youtu.be'):
            return candidate
    except ValueError:
        pass
    return None


def download_video(url: str, directory: str) -> Path:
    """Blocking yt-dlp, called via asyncio.to_thread."""
    is_tiktok = 'tiktok.com' in (urlparse(url).hostname or '')
    opts = {
        'outtmpl': str(Path(directory) / '%(id)s.%(ext)s'),
        'format': 'bv*+ba/b',
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': False,
        'retries': 2,
        'fragment_retries': 2,
        'socket_timeout': 25,
        'max_filesize': MAX_MB * 1024 * 1024,
        'restrictfilenames': True,
        'js_runtimes': {'node': {}},
    }
    if is_tiktok:
        opts['impersonate'] = 'chrome'
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in Path(directory).iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov')]
    if not files:
        raise RuntimeError('no_media_output')
    return max(files, key=lambda p: p.stat().st_size)


@router.message(F.text)
async def handle_url(message: Message):
    await register(message.from_user)
    link = valid_link(message.text or '')
    if not link:
        return await message.answer('Отправь HTTPS-ссылку на TikTok или YouTube Shorts. Например: https://youtube.com/shorts/VIDEO_ID')
    if not await allowed(message.from_user.id):
        return await message.answer('🔒 Подписка не активна. Для доступа обратись к администратору.')
    status = await message.answer('⏳ Скачиваю видео...')
    directory = tempfile.mkdtemp(prefix='shorts_')
    try:
        async with semaphore:
            file_path = await asyncio.wait_for(asyncio.to_thread(download_video, link, directory), timeout=180)
            if file_path.stat().st_size > MAX_MB * 1024 * 1024:
                return await status.edit_text(f'⚠️ Видео больше {MAX_MB} МБ. Отправка отменена.')
            await message.answer_video(FSInputFile(file_path), caption='✅ Видео готово!', supports_streaming=file_path.suffix.lower() == '.mp4')
            async with aiosqlite.connect(DATABASE) as db:
                await db.execute('UPDATE users SET downloads=downloads+1 WHERE user_id=?', (message.from_user.id,))
                await db.commit()
            await status.delete()
    except asyncio.TimeoutError:
        await status.edit_text('⌛ Загрузка заняла слишком много времени. Попробуй позже.')
    except Exception as exc:
        log.exception('Download/send failed: %s', type(exc).__name__)
        await status.edit_text('❌ Не получилось загрузить ролик. Проверь, что он публичный. YouTube и TikTok могут ограничивать серверные загрузки. Попробуй позже.')
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def main():
    if not TOKEN:
        raise RuntimeError('Укажи BOT_TOKEN в .env')
    if not ADMIN_IDS:
        raise RuntimeError('Укажи ADMIN_IDS в .env (числовой Telegram ID)')
    await init_db()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
    dp = Dispatcher()
    dp.include_router(router)
    log.info('Bot is starting')
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
