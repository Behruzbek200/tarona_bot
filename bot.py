import os
import io
import csv
import glob
import html
import time
import random
import secrets
import shutil
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler
import psycopg2
from psycopg2.extras import execute_values
import telebot
from telebot import types, apihelper
import yt_dlp
import imageio_ffmpeg
from shazamio import Shazam

# ==========================================
# 1. RENDER VA SUPABASE MUHIT SOZLAMALARI
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "7565914467"))
PORT = int(os.environ.get("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN muhit o'zgaruvchisi kiritilmagan!")

apihelper.CONNECT_TIMEOUT = 20
apihelper.READ_TIMEOUT = 90
apihelper.WRITE_TIMEOUT = 90

os.makedirs("bin", exist_ok=True)
os.makedirs("downloads", exist_ok=True)

FFMPEG_SRC = imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG_BIN = os.path.abspath("bin/ffmpeg.exe" if os.name == "nt" else "bin/ffmpeg")
if not os.path.exists(FFMPEG_BIN):
    shutil.copyfile(FFMPEG_SRC, FFMPEG_BIN)
    if os.name != "nt":
        os.chmod(FFMPEG_BIN, 0o755)

DOWNLOAD_SEMAPHORE = threading.Semaphore(16)
SUB_CHECK_POOL = ThreadPoolExecutor(max_workers=24)
TOP_FETCH_LOCKS = {}
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=48)
BOT_USERNAME_CACHE = None

def get_bot_username() -> str:
    global BOT_USERNAME_CACHE
    if not BOT_USERNAME_CACHE:
        BOT_USERNAME_CACHE = bot.get_me().username or "bot"
    return BOT_USERNAME_CACHE

# ==========================================
# 2. TEZKOR RAM XOTIRA VA ASINXRON SYNC NAVBATI
# ==========================================
MEM_LOCK = threading.RLock()
MEM = {
    "settings": {"show_bot_tag": "1", "total_requests": "0"},
    "users": {},            # user_id -> dict
    "admins": {SUPER_ADMIN_ID: "Super Admin"},
    "channels": {},         # channel_id -> dict
    "join_requests": set(),   # (user_id, channel_id)
    "sub_log_keys": set(),    # (user_id, channel_id)
    "sub_logs": [],         # list of dicts
    "ads": {},              # ad_id -> dict
    "favorites": {},        # user_id -> list of dict
    "media_cache": {},      # cache_key -> (file_id, media_type, title)
    "title_cache": {},      # normalized_title -> (file_id, media_type, title)
    "sub_ttl": {},          # (user_id, channel_id) -> {"ts": float, "ok": bool}
    "popup_cd": {}          # user_id -> float
}

SYNC_QUEUE = {
    "users": {},
    "sub_logs": [],
    "join_req": [],
    "ch_counts": set(),
    "ad_views": {},
    "media_cache": {},
    "fav_add": [],
    "fav_del": []
}

SEARCH_CACHE = {}
QUERY_RESULT_CACHE = {}     # norm_query -> {"ts": float, "entries": list}
INLINE_RESULTS_CACHE = {}
TOP_COUNTRY_CACHE = {}
ADMIN_STATE = {}
BROADCAST_STOP = False
TOP_CACHE_TTL = 900.0
QUERY_CACHE_TTL = 600.0

TOP_COUNTRIES = [
    {
        "code": "uz", "flag": "🇺🇿",
        "name_uz": "O'zbekiston", "name_ru": "Узбекистан", "name_en": "Uzbekistan",
        "queries": [
            "yangi ozbek qoshiqlar 2026 official",
            "uzbek music 2026 klip",
            "xamdam sobirov jaloliddin ahmadaliyev 2026",
            "shohruhxon munisa rizayeva ozoda nursaidova 2026"
        ]
    },
    {
        "code": "ru", "flag": "🇷🇺",
        "name_uz": "Rossiya", "name_ru": "Россия", "name_en": "Russia",
        "queries": [
            "русские хиты 2026 премьера клипа",
            "новинки русской музыки 2026 official",
            "miyagi macan anna asti 2026"
        ]
    },
    {
        "code": "kz", "flag": "🇰🇿",
        "name_uz": "Qozog'iston", "name_ru": "Казахстан", "name_en": "Kazakhstan",
        "queries": [
            "казахские песни 2026 жаңа әндер",
            "qazaq music 2026 official",
            "kairat nurtas sdr 2026"
        ]
    },
    {
        "code": "tr", "flag": "🇹🇷",
        "name_uz": "Turkiya", "name_ru": "Турция", "name_en": "Turkey",
        "queries": [
            "türkçe pop 2026 yeni şarkılar official",
            "turkce hit sarkilar 2026 klip",
            "semicenk lvbel c5 2026"
        ]
    },
    {
        "code": "tj", "flag": "🇹🇯",
        "name_uz": "Tojikiston", "name_ru": "Таджикистан", "name_en": "Tajikistan",
        "queries": [
            "сурудхои нави точики 2026",
            "tajik music 2026 official",
            "shabnami surayo nigina amonqulova 2026"
        ]
    }
]

BANNED_MIX_WORDS = (
    "top hits", "top 100", "top 50", "top 30", "top 20", "top 10",
    "playlist", "megamix", "nonstop", "non-stop", "mashup",
    "сборник", "плейлист", "топлам", "toplam", "mix 202"
)

LANG_TEXTS = {
    "uz": {
        "choose_lang": "🌐 <b>Tilni tanlang / Выберите язык / Choose language:</b>",
        "lang_saved": "✅ <b>Til tanlandi: O'zbekcha 🇺🇿</b>",
        "sub_req": "🔒 <b>Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling yoki so'rov yuboring:</b>",
        "sub_btn": "✅ Tasdiqlash",
        "sub_not_yet": "❌ Hali barcha kanallarga a'zo bo'lmadingiz yoki so'rov yubormadingiz!",
        "sub_ok": "✅ <b>Obuna tasdiqlandi! Endi qo'shiq nomi/matni, havola yoki ovoz yuboring.</b>",
        "start_msg": (
            "🔥 <b>Assalomu alaykum! @{bot_user} ga xush kelibsiz!</b>\n\n"
            "📥 <b>Video, rasm va media yuklash:</b>\n"
            "<b>Instagram, YouTube, TikTok, Facebook, Pinterest, Snapchat, Likee, Telegram</b> va <b>Threads</b> havolalarini yuboring — tez yuklab beraman. Videodan <b>MP3</b> ajratish ham bor!\n\n"
            "🎶 <b>Qo'shiq qidirish va yuklash:</b>\n"
            "Qo'shiq nomi, ijrochi yoki <b>istalgan matni/so'zlari</b> bo'yicha topib, yuqori sifatda yuklab oling.\n\n"
            "🎙 <b>Shazam — musiqani aniqlash:</b>\n"
            "<b>Ovoz, audio</b> yoki <b>video</b> yuboring — musiqani aniqlab, yuklab olish tugmasini beraman.\n\n"
            "⚡ <b>Inline rejim:</b> Istalgan chatda <code>@{bot_user} qo'shiq nomi</code> yozib darhol musiqa yuboring!\n"
            "🔥 <b>/top — 5 davlat TOP qo'shiqlari</b> (🇺🇿 🇷🇺 🇰🇿 🇹🇷 🇹🇯)\n"
            "❤️ <b>/fav — Sevimli musiqalar</b> | 🌐 <b>/lang — Til</b>\n\n"
            "🚀 <b>Havola, qo'shiq matni yoki ovoz yuboring!</b>"
        ),
        "add_group": "GURUHGA QO'SHISH ⤴️",
        "inline_try_btn": "🔎 Istalgan chatda qidirish",
        "share_inline_btn": "📤 Do'stga yuborish",
        "loading": "⏳ Yuklanmoqda",
        "top_loading": "🔥 <b>Eng mashhur qo'shiqlar yuklanmoqda...</b>",
        "dl_audio_err": "❌ <b>Musiqani yuklab bo'lmadi.</b>",
        "dl_video_err": "❌ <b>Videoni yuklab bo'lmadi.</b>",
        "dl_video_btn": "🎬 Videosini yuklab olish",
        "dl_mp3_btn": "🎵 Musiqasini yuklab olish",
        "fav_add": "❤️ Sevimlilar",
        "fav_added": "❤️ Sevimlilarga qo'shildi!",
        "fav_removed": "💔 Sevimlilardan o'chirildi!",
        "fav_empty": "💔 <b>Sevimli musiqalar ro'yxati hozircha bo'sh.</b>",
        "fav_title": "❤️ Sevimli musiqalar",
        "top_header": "🔥 Mashhur qo'shiqlar — {flag} {country}",
        "shazam_found": "🎵 <b>Qo'shiq topildi!</b>\n\n🎤 <b>Ijrochi:</b> {artist}\n🎼 <b>Nomi:</b> {title}",
        "shazam_dl_btn": "🎵 Qo'shiqni yuklab olish",
        "shazam_prompt": "🎙 <b>Shazam orqali musiqa aniqlash uchun menga ovozli xabar, audio yoki video yuboring!</b>",
        "shazam_not_found": "❌ <b>Musiqani aniqlab bo'lmadi.</b>",
        "search_not_found": "❌ <b>Hech narsa topilmadi.</b>",
        "search_err": "❌ <b>Qidiruvda xatolik yuz berdi.</b>",
        "expired": "❌ Ma'lumot eskirgan.",
        "refresh": "🔄 Yangilash",
        "excel_btn": "📥 Excel yuklab olish"
    },
    "ru": {
        "choose_lang": "🌐 <b>Выберите язык / Tilni tanlang / Choose language:</b>",
        "lang_saved": "✅ <b>Язык выбран: Русский 🇷🇺</b>",
        "sub_req": "🔒 <b>Для использования бота подпишитесь на каналы или отправьте заявку:</b>",
        "sub_btn": "✅ Проверить подписку",
        "sub_not_yet": "❌ Вы еще не подписались на все каналы или не подали заявку!",
        "sub_ok": "✅ <b>Подписка подтверждена! Отправьте ссылку, текст песни или голос.</b>",
        "start_msg": (
            "🔥 <b>Добро пожаловать в @{bot_user}!</b>\n\n"
            "📥 <b>Скачивание медиа:</b>\n"
            "<b>Instagram, YouTube, TikTok, Facebook, Pinterest, Snapchat, Likee, Telegram</b> и <b>Threads</b> — быстрая загрузка и <b>MP3</b> из видео.\n\n"
            "🎶 <b>Поиск музыки:</b> По названию или <b>любому тексту/строчке из песни</b>.\n"
            "🎙 <b>Shazam:</b> Отправьте <b>голос, аудио</b> или <b>видео</b> для распознавания.\n"
            "⚡ <b>Inline режим:</b> Введите <code>@{bot_user} название песни</code> в любом чате!\n"
            "🔥 <b>/top — ТОП 5 стран</b> (🇺🇿 🇷🇺 🇰🇿 🇹🇷 🇹🇯) | ❤️ <b>/fav</b> | 🌐 <b>/lang</b>\n\n"
            "🚀 <b>Отправьте ссылку, текст песни или голос!</b>"
        ),
        "add_group": "ДОБАВИТЬ В ГРУППУ ⤴️",
        "inline_try_btn": "🔎 Поиск в любом чате",
        "share_inline_btn": "📤 Отправить другу",
        "loading": "⏳ Загрузка",
        "top_loading": "🔥 <b>Загрузка популярных песен...</b>",
        "dl_audio_err": "❌ <b>Не удалось скачать музыку.</b>",
        "dl_video_err": "❌ <b>Не удалось скачать видео.</b>",
        "dl_video_btn": "🎬 Скачать видео",
        "dl_mp3_btn": "🎵 Скачать музыку",
        "fav_add": "❤️ В избранное",
        "fav_added": "❤️ Добавлено в избранное!",
        "fav_removed": "💔 Удалено из избранного!",
        "fav_empty": "💔 <b>Список избранного пока пуст.</b>",
        "fav_title": "❤️ Избранная музыка",
        "top_header": "🔥 Популярные песни — {flag} {country}",
        "shazam_found": "🎵 <b>Трек найден!</b>\n\n🎤 <b>Исполнитель:</b> {artist}\n🎼 <b>Название:</b> {title}",
        "shazam_dl_btn": "🎵 Скачать трек",
        "shazam_prompt": "🎙 <b>Отправьте голосовое сообщение, аудио или видео, чтобы распознать музыку через Shazam!</b>",
        "shazam_not_found": "❌ <b>Не удалось определить музыку.</b>",
        "search_not_found": "❌ <b>Ничего не найдено.</b>",
        "search_err": "❌ <b>Ошибка при поиске.</b>",
        "expired": "❌ Данные устарели.",
        "refresh": "🔄 Обновить",
        "excel_btn": "📥 Скачать Excel"
    },
    "en": {
        "choose_lang": "🌐 <b>Choose language / Tilni tanlang / Выберите язык:</b>",
        "lang_saved": "✅ <b>Language selected: English 🇬🇧</b>",
        "sub_req": "🔒 <b>Please join the channels below or send a join request to use the bot:</b>",
        "sub_btn": "✅ Verify subscription",
        "sub_not_yet": "❌ You haven't joined all required channels or sent a request yet!",
        "sub_ok": "✅ <b>Subscription verified! Send a link, song lyrics/text, or voice.</b>",
        "start_msg": (
            "🔥 <b>Welcome to @{bot_user}!</b>\n\n"
            "📥 <b>Media downloader:</b> <b>Instagram, YouTube, TikTok, Facebook, Pinterest, Snapchat, Likee, Telegram</b> & <b>Threads</b> + <b>MP3</b> extraction.\n"
            "🎶 <b>Music search:</b> By title or <b>any song lyrics/text</b>.\n"
            "🎙 <b>Shazam:</b> Send <b>voice, audio</b>, or <b>video</b> to identify any song.\n"
            "⚡ <b>Inline Mode:</b> Type <code>@{bot_user} song name</code> in any chat!\n"
            "🔥 <b>/top — Trending in 5 countries</b> (🇺🇿 🇷🇺 🇰🇿 🇹🇷 🇹🇯) | ❤️ <b>/fav</b> | 🌐 <b>/lang</b>\n\n"
            "🚀 <b>Send a link, song text, or voice message!</b>"
        ),
        "add_group": "ADD TO GROUP ⤴️",
        "inline_try_btn": "🔎 Search in any chat",
        "share_inline_btn": "📤 Share with friend",
        "loading": "⏳ Downloading",
        "top_loading": "🔥 <b>Loading trending songs...</b>",
        "dl_audio_err": "❌ <b>Failed to download music.</b>",
        "dl_video_err": "❌ <b>Failed to download video.</b>",
        "dl_video_btn": "🎬 Download video",
        "dl_mp3_btn": "🎵 Download audio",
        "fav_add": "❤️ Favorites",
        "fav_added": "❤️ Added to favorites!",
        "fav_removed": "💔 Removed from favorites!",
        "fav_empty": "💔 <b>Your favorites list is empty.</b>",
        "fav_title": "❤️ Favorite Songs",
        "top_header": "🔥 Trending Songs — {flag} {country}",
        "shazam_found": "🎵 <b>Song found!</b>\n\n🎤 <b>Artist:</b> {artist}\n🎼 <b>Title:</b> {title}",
        "shazam_dl_btn": "🎵 Download song",
        "shazam_prompt": "🎙 <b>Send a voice message, audio, or video to recognize the song with Shazam!</b>",
        "shazam_not_found": "❌ <b>Could not recognize the song.</b>",
        "search_not_found": "❌ <b>Nothing found.</b>",
        "search_err": "❌ <b>Search error occurred.</b>",
        "expired": "❌ Session expired.",
        "refresh": "🔄 Refresh",
        "excel_btn": "📥 Download Excel"
    }
}

def esc(text: str) -> str:
    return html.escape(str(text))

def normalize_title_key(title: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in (title or ""))
    return " ".join(cleaned.split())

def normalize_tg_url(raw_url: str) -> str:
    u = (raw_url or "").strip()
    if not u:
        return ""
    if u.startswith("@"):
        return f"https://t.me/{u[1:]}"
    if u.startswith("t.me/"):
        return f"https://{u}"
    if not u.startswith(("http://", "https://")):
        return f"https://t.me/{u.lstrip('/')}"
    return u

def format_duration(seconds) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    return f" {m}:{s:02d}"

def format_user_name(user) -> str:
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    return f"{first} {last}".strip() or "Foydalanuvchi"

def format_username(user) -> str:
    uname = getattr(user, "username", None)
    return f"@{uname}" if uname else "Username yo'q"

def generate_stat_token(prefix: str = "st") -> str:
    return f"{prefix}_{secrets.token_urlsafe(6)}"

def parse_url_buttons_text(raw_text: str):
    markup = types.InlineKeyboardMarkup()
    if not raw_text or raw_text.strip() == "-":
        return None
    added = 0
    for line in raw_text.strip().splitlines():
        if "-" in line:
            parts = line.rsplit("-", 1)
            btn_name = parts[0].strip()
            btn_url = normalize_tg_url(parts[1].strip())
            if btn_name and btn_url.startswith("http"):
                markup.add(types.InlineKeyboardButton(text=btn_name[:45], url=btn_url))
                added += 1
    return markup if added > 0 else None

# ==========================================
# 3. SUPABASE ULANISH, YUKLASH VA BATCH SYNC
# ==========================================
def get_pg_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL muhit o'zgaruvchisi kiritilmagan!")
    url = DATABASE_URL
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return psycopg2.connect(url, connect_timeout=15)

def init_and_load_supabase():
    conn = get_pg_conn()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO settings (key, value) VALUES ('show_bot_tag', '1') ON CONFLICT DO NOTHING;
        INSERT INTO settings (key, value) VALUES ('total_requests', '0') ON CONFLICT DO NOTHING;

        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            chat_type TEXT DEFAULT 'private',
            lang TEXT DEFAULT '',
            is_banned INTEGER DEFAULT 0,
            is_left INTEGER DEFAULT 0,
            joined_ts DOUBLE PRECISION DEFAULT 0,
            last_active DOUBLE PRECISION DEFAULT 0
        );

        ALTER TABLE users ADD COLUMN IF NOT EXISTS is_left INTEGER DEFAULT 0;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_ts DOUBLE PRECISION DEFAULT 0;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active DOUBLE PRECISION DEFAULT 0;

        CREATE TABLE IF NOT EXISTS favorites (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            UNIQUE(user_id, url)
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            name TEXT DEFAULT 'Admin'
        );

        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT DEFAULT 'Kanal/Guruh',
            channel_type TEXT DEFAULT 'public',
            channel_url TEXT NOT NULL,
            target_limit INTEGER DEFAULT 0,
            current_count INTEGER DEFAULT 0,
            shown_count INTEGER DEFAULT 0,
            stat_token TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS join_requests (
            user_id BIGINT,
            channel_id TEXT,
            first_name TEXT DEFAULT 'Foydalanuvchi',
            username TEXT DEFAULT 'Username yo''q',
            requested_at TEXT,
            PRIMARY KEY (user_id, channel_id)
        );

        CREATE TABLE IF NOT EXISTS sub_logs (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            channel_id TEXT,
            first_name TEXT DEFAULT 'Foydalanuvchi',
            username TEXT DEFAULT 'Username yo''q',
            subscribed_at TEXT,
            UNIQUE(user_id, channel_id)
        );

        CREATE TABLE IF NOT EXISTS ads (
            id SERIAL PRIMARY KEY,
            ad_type TEXT NOT NULL,
            text TEXT NOT NULL,
            url TEXT DEFAULT '',
            media_type TEXT DEFAULT 'text',
            media_file_id TEXT DEFAULT '',
            max_views INTEGER DEFAULT 0,
            current_views INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            expires_at DOUBLE PRECISION DEFAULT 0,
            is_fallback INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            stat_token TEXT DEFAULT ''
        );

        ALTER TABLE ads ADD COLUMN IF NOT EXISTS media_type TEXT DEFAULT 'text';
        ALTER TABLE ads ADD COLUMN IF NOT EXISTS media_file_id TEXT DEFAULT '';

        CREATE TABLE IF NOT EXISTS media_cache (
            cache_key TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            title TEXT
        );
    """)
    cur.execute("INSERT INTO admins (user_id, name) VALUES (%s, 'Super Admin') ON CONFLICT DO NOTHING", (SUPER_ADMIN_ID,))

    now = time.time()
    with MEM_LOCK:
        cur.execute("SELECT key, value FROM settings")
        for k, v in cur.fetchall():
            MEM["settings"][k] = v

        cur.execute("SELECT user_id, chat_type, lang, is_banned, is_left, joined_ts, last_active FROM users")
        for uid, ctype, lang, banned, left, jts, lactive in cur.fetchall():
            MEM["users"][uid] = {
                "chat_type": ctype or "private",
                "lang": lang or "",
                "is_banned": int(banned or 0),
                "is_left": int(left or 0),
                "joined_ts": float(jts or now),
                "last_active": float(lactive or jts or now)
            }

        cur.execute("SELECT user_id, name FROM admins")
        for uid, name in cur.fetchall():
            MEM["admins"][uid] = name or "Admin"

        cur.execute("SELECT channel_id, title, channel_type, channel_url, target_limit, current_count, shown_count, stat_token FROM channels")
        for cid, title, ctype, curl, lim, cnt, shw, tok in cur.fetchall():
            MEM["channels"][cid] = {
                "channel_id": cid, "title": title, "channel_type": ctype,
                "channel_url": normalize_tg_url(curl), "target_limit": int(lim or 0),
                "current_count": int(cnt or 0), "shown_count": int(shw or 0),
                "stat_token": tok or generate_stat_token("ch")
            }

        cur.execute("SELECT user_id, channel_id FROM join_requests")
        for uid, cid in cur.fetchall():
            MEM["join_requests"].add((uid, str(cid)))

        cur.execute("SELECT user_id, channel_id, first_name, username, subscribed_at FROM sub_logs ORDER BY id ASC")
        for uid, cid, fname, uname, sat in cur.fetchall():
            cid_s = str(cid)
            MEM["sub_log_keys"].add((uid, cid_s))
            MEM["sub_logs"].append({
                "user_id": uid, "channel_id": cid_s,
                "first_name": fname, "username": uname, "subscribed_at": sat
            })

        cur.execute("SELECT id, ad_type, text, url, media_type, media_file_id, max_views, current_views, clicks, expires_at, is_fallback, is_active, stat_token FROM ads")
        for r in cur.fetchall():
            MEM["ads"][r[0]] = {
                "id": r[0], "ad_type": r[1], "text": r[2], "url": r[3] or "",
                "media_type": r[4] or "text", "media_file_id": r[5] or "",
                "max_views": int(r[6] or 0), "current_views": int(r[7] or 0),
                "clicks": int(r[8] or 0), "expires_at": float(r[9] or 0),
                "is_fallback": int(r[10] or 0), "is_active": int(r[11] or 1),
                "stat_token": r[12] or generate_stat_token("ad")
            }

        cur.execute("SELECT user_id, title, url FROM favorites ORDER BY id ASC")
        for uid, title, url in cur.fetchall():
            MEM["favorites"].setdefault(uid, []).insert(0, {"title": title, "url": url, "duration": ""})

        cur.execute("SELECT cache_key, file_id, media_type, title FROM media_cache")
        for ck, fid, mt, ttl in cur.fetchall():
            MEM["media_cache"][ck] = (fid, mt, ttl)
            if mt == "audio" and ttl:
                tk = normalize_title_key(ttl)
                if tk:
                    MEM["title_cache"][tk] = (fid, mt, ttl)

    cur.close()
    conn.close()
    print(f"✅ Supabase RAM yuklandi: {len(MEM['users'])} user, {len(MEM['channels'])} kanal, {len(MEM['media_cache'])} kesh.")

def exec_immediate_pg(sql: str, params=()):
    def _worker():
        try:
            conn = get_pg_conn()
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(sql, params)
            cur.close()
            conn.close()
        except Exception as e:
            print(f"Supabase immediate write error: {e}")
    threading.Thread(target=_worker, daemon=True).start()

def flush_sync_queue_to_supabase():
    with MEM_LOCK:
        users_batch = list(SYNC_QUEUE["users"].values())
        SYNC_QUEUE["users"].clear()

        sub_logs_batch = list(SYNC_QUEUE["sub_logs"])
        SYNC_QUEUE["sub_logs"].clear()

        join_req_batch = list(SYNC_QUEUE["join_req"])
        SYNC_QUEUE["join_req"].clear()

        ch_ids = list(SYNC_QUEUE["ch_counts"])
        SYNC_QUEUE["ch_counts"].clear()
        ch_updates = [
            (MEM["channels"][cid]["current_count"], MEM["channels"][cid]["shown_count"], cid)
            for cid in ch_ids if cid in MEM["channels"]
        ]

        ad_views_batch = list(SYNC_QUEUE["ad_views"].items())
        SYNC_QUEUE["ad_views"].clear()

        media_batch = list(SYNC_QUEUE["media_cache"].values())
        SYNC_QUEUE["media_cache"].clear()

        fav_add_batch = list(SYNC_QUEUE["fav_add"])
        SYNC_QUEUE["fav_add"].clear()

        fav_del_batch = list(SYNC_QUEUE["fav_del"])
        SYNC_QUEUE["fav_del"].clear()

        total_req_val = MEM["settings"].get("total_requests", "0")

    try:
        conn = get_pg_conn()
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute(
            "INSERT INTO settings (key, value) VALUES ('total_requests', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (total_req_val,)
        )

        if users_batch:
            execute_values(
                cur,
                """
                INSERT INTO users (user_id, chat_type, lang, is_banned, is_left, joined_ts, last_active)
                VALUES %s
                ON CONFLICT (user_id) DO UPDATE SET
                    chat_type = EXCLUDED.chat_type,
                    lang = CASE WHEN EXCLUDED.lang != '' THEN EXCLUDED.lang ELSE users.lang END,
                    is_banned = EXCLUDED.is_banned,
                    is_left = EXCLUDED.is_left,
                    last_active = GREATEST(users.last_active, EXCLUDED.last_active)
                """,
                users_batch
            )

        if join_req_batch:
            execute_values(
                cur,
                "INSERT INTO join_requests (user_id, channel_id, first_name, username, requested_at) VALUES %s ON CONFLICT DO NOTHING",
                join_req_batch
            )

        if sub_logs_batch:
            execute_values(
                cur,
                "INSERT INTO sub_logs (user_id, channel_id, first_name, username, subscribed_at) VALUES %s ON CONFLICT DO NOTHING",
                sub_logs_batch
            )

        for cur_cnt, shw_cnt, cid in ch_updates:
            cur.execute(
                "UPDATE channels SET current_count = %s, shown_count = %s WHERE channel_id = %s",
                (cur_cnt, shw_cnt, cid)
            )

        for ad_id, delta in ad_views_batch:
            cur.execute("UPDATE ads SET current_views = current_views + %s WHERE id = %s", (delta, ad_id))

        if media_batch:
            execute_values(
                cur,
                """
                INSERT INTO media_cache (cache_key, file_id, media_type, title)
                VALUES %s
                ON CONFLICT (cache_key) DO UPDATE SET
                    file_id = EXCLUDED.file_id,
                    media_type = EXCLUDED.media_type,
                    title = EXCLUDED.title
                """,
                media_batch
            )

        if fav_add_batch:
            execute_values(
                cur,
                "INSERT INTO favorites (user_id, title, url) VALUES %s ON CONFLICT DO NOTHING",
                fav_add_batch
            )

        for uid, url in fav_del_batch:
            cur.execute("DELETE FROM favorites WHERE user_id = %s AND url = %s", (uid, url))

        cur.close()
        conn.close()
    except Exception as e:
        print(f"Supabase batch sync error: {e}")

def background_supabase_sync_worker():
    while True:
        time.sleep(8)
        flush_sync_queue_to_supabase()

# ==========================================
# 4. QIDIRUV VA YUKLASH SOZLAMALARI
# ==========================================
def get_ydl_opts(mode: str = "audio", outtmpl: str = None):
    if mode == "search":
        return {
            'extractor_args': {
                'youtube': {
                    'player_client': ['web', 'mweb'],
                }
            },
            'js_runtimes': {'node': {}},
            'source_address': '0.0.0.0',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'retries': 3,
            'socket_timeout': 20,
        }

    opts = {
        'ffmpeg_location': FFMPEG_BIN,
        'extractor_args': {
            'youtube': {
                'player_client': ['tv', 'android_vr', 'web_safari'],
            }
        },
        'js_runtimes': {'node': {}},
        'source_address': '0.0.0.0',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'retries': 3,
        'fragment_retries': 3,
        'socket_timeout': 20,
        'concurrent_fragment_downloads': 8,
        'http_chunk_size': 10 * 1024 * 1024,
    }
    if mode == "audio":
        opts['format'] = 'bestaudio[ext=m4a][abr<=130]/bestaudio[ext=m4a]/bestaudio/best'
    elif mode == "video":
        opts['format'] = 'best[height<=720][ext=mp4]/best[height<=720]/best'
    if outtmpl:
        opts['outtmpl'] = outtmpl
        opts['max_filesize'] = 49 * 1024 * 1024
    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"
    elif os.path.exists("/etc/secrets/cookies.txt"):
        opts['cookiefile'] = "/etc/secrets/cookies.txt"
    return opts

# ==========================================
# 5. RAM-FIRST USER, FAOLLIK, CHIQQANLAR VA KESH
# ==========================================
def mark_user_left_status(chat_id: int, is_left_val: int):
    now = time.time()
    with MEM_LOCK:
        u = MEM["users"].get(chat_id)
        if u:
            u["is_left"] = is_left_val
            if is_left_val == 0:
                u["last_active"] = now
            SYNC_QUEUE["users"][chat_id] = (
                chat_id, u["chat_type"], u["lang"], u["is_banned"], u["is_left"], u["joined_ts"], u["last_active"]
            )

@bot.my_chat_member_handler()
def handle_my_chat_member(update: types.ChatMemberUpdated):
    chat_id = update.chat.id
    new_status = update.new_chat_member.status
    if new_status in ["kicked", "left"]:
        mark_user_left_status(chat_id, 1)
    elif new_status in ["member", "administrator"]:
        register_chat(chat_id, update.chat.type)
        mark_user_left_status(chat_id, 0)

def get_user_lang(user_id: int):
    with MEM_LOCK:
        u = MEM["users"].get(user_id)
        if u and u["lang"] in LANG_TEXTS:
            return u["lang"]
        return None

def set_user_lang(user_id: int, lang_code: str):
    if lang_code not in LANG_TEXTS:
        return
    now = time.time()
    with MEM_LOCK:
        u = MEM["users"].get(user_id)
        if not u:
            u = {"chat_type": "private", "lang": lang_code, "is_banned": 0, "is_left": 0, "joined_ts": now, "last_active": now}
            MEM["users"][user_id] = u
        else:
            u["lang"] = lang_code
            u["is_left"] = 0
            u["last_active"] = now
        SYNC_QUEUE["users"][user_id] = (user_id, u["chat_type"], u["lang"], u["is_banned"], u["is_left"], u["joined_ts"], u["last_active"])

def t(user_id: int, key: str, **kwargs) -> str:
    lang = get_user_lang(user_id) or "uz"
    template = LANG_TEXTS.get(lang, LANG_TEXTS["uz"]).get(key, LANG_TEXTS["uz"].get(key, ""))
    return template.format(**kwargs) if kwargs else template

def lang_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="set_lang:uz"),
        types.InlineKeyboardButton(text="🇷🇺 Русский", callback_data="set_lang:ru"),
        types.InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang:en")
    )
    return markup

def toggle_favorite_db(user_id: int, title: str, url: str) -> bool:
    clean_t = title[:120]
    with MEM_LOCK:
        favs = MEM["favorites"].setdefault(user_id, [])
        for i, item in enumerate(favs):
            if item["url"] == url:
                favs.pop(i)
                SYNC_QUEUE["fav_del"].append((user_id, url))
                return False
        favs.insert(0, {"title": clean_t, "url": url, "duration": ""})
        SYNC_QUEUE["fav_add"].append((user_id, clean_t, url))
        return True

def get_user_favorites(user_id: int):
    with MEM_LOCK:
        return list(MEM["favorites"].get(user_id, []))

def is_show_bot_tag() -> bool:
    with MEM_LOCK:
        return MEM["settings"].get("show_bot_tag", "1") == "1"

def toggle_show_bot_tag() -> bool:
    new_val = "0" if is_show_bot_tag() else "1"
    with MEM_LOCK:
        MEM["settings"]["show_bot_tag"] = new_val
    exec_immediate_pg(
        "INSERT INTO settings (key, value) VALUES ('show_bot_tag', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (new_val,)
    )
    return new_val == "1"

def inc_total_requests():
    with MEM_LOCK:
        cur = int(MEM["settings"].get("total_requests", "0"))
        MEM["settings"]["total_requests"] = str(cur + 1)

def build_media_caption(prefix_line: str = "", ad_line: str = "") -> str:
    bot_username = get_bot_username()
    parts = []
    if prefix_line:
        parts.append(prefix_line)
    tag_part = f"📥 @{esc(bot_username)}" if is_show_bot_tag() else ""
    clean_ad = ad_line.lstrip("\n") if ad_line else ""
    if tag_part and clean_ad:
        parts.append(f"{tag_part}\n{clean_ad}")
    elif tag_part:
        parts.append(tag_part)
    elif clean_ad:
        parts.append(clean_ad)
    return "\n\n".join(parts) if len(parts) > 1 else (parts[0] if parts else "")

def is_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    with MEM_LOCK:
        return user_id in MEM["admins"]

def get_admins():
    with MEM_LOCK:
        return list(MEM["admins"].items())

def add_admin_db(user_id: int, name: str = "Admin"):
    with MEM_LOCK:
        MEM["admins"][user_id] = name
    exec_immediate_pg(
        "INSERT INTO admins (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name",
        (user_id, name)
    )

def remove_admin_db(user_id: int):
    if user_id == SUPER_ADMIN_ID:
        return
    with MEM_LOCK:
        MEM["admins"].pop(user_id, None)
    exec_immediate_pg("DELETE FROM admins WHERE user_id = %s", (user_id,))

def register_chat(chat_id: int, chat_type: str = "private"):
    now = time.time()
    with MEM_LOCK:
        u = MEM["users"].get(chat_id)
        if not u:
            MEM["users"][chat_id] = {
                "chat_type": chat_type,
                "lang": "",
                "is_banned": 0,
                "is_left": 0,
                "joined_ts": now,
                "last_active": now
            }
            SYNC_QUEUE["users"][chat_id] = (chat_id, chat_type, "", 0, 0, now, now)
        else:
            if u["is_left"] == 1 or (now - u["last_active"] > 120):
                u["is_left"] = 0
                u["last_active"] = now
                SYNC_QUEUE["users"][chat_id] = (
                    chat_id, u["chat_type"], u["lang"], u["is_banned"], 0, u["joined_ts"], now
                )

def is_user_banned(user_id: int) -> bool:
    if is_admin(user_id):
        return False
    with MEM_LOCK:
        u = MEM["users"].get(user_id)
        return bool(u and u["is_banned"] == 1)

def get_cached_media(cache_key: str, title: str = ""):
    with MEM_LOCK:
        hit = MEM["media_cache"].get(cache_key)
        if hit:
            return hit
        if title:
            tk = normalize_title_key(title)
            if tk and tk in MEM["title_cache"]:
                return MEM["title_cache"][tk]
    return None

def save_cached_media(cache_key: str, file_id: str, media_type: str, title: str):
    with MEM_LOCK:
        MEM["media_cache"][cache_key] = (file_id, media_type, title)
        SYNC_QUEUE["media_cache"][cache_key] = (cache_key, file_id, media_type, title)
        if media_type == "audio" and title:
            tk = normalize_title_key(title)
            if tk:
                MEM["title_cache"][tk] = (file_id, media_type, title)

def get_detailed_stats():
    now = time.time()
    day_ago = now - 86400
    week_ago = now - 7 * 86400
    month_ago = now - 30 * 86400

    with MEM_LOCK:
        all_u = list(MEM["users"].values())
        priv_u = [u for u in all_u if u["chat_type"] == "private"]
        grp_u = [u for u in all_u if u["chat_type"] != "private"]

        total_priv = len(priv_u)
        total_grp = len(grp_u)

        left_priv = sum(1 for u in priv_u if u["is_left"] == 1)
        left_grp = sum(1 for u in grp_u if u["is_left"] == 1)
        banned_cnt = sum(1 for u in priv_u if u["is_banned"] == 1)

        alive_priv = max(0, total_priv - left_priv - banned_cnt)
        alive_grp = max(0, total_grp - left_grp)

        joined_24h = sum(1 for u in priv_u if u["joined_ts"] >= day_ago)
        joined_7d = sum(1 for u in priv_u if u["joined_ts"] >= week_ago)
        joined_30d = sum(1 for u in priv_u if u["joined_ts"] >= month_ago)

        active_24h = sum(1 for u in priv_u if u["is_left"] == 0 and u["last_active"] >= day_ago)
        active_7d = sum(1 for u in priv_u if u["is_left"] == 0 and u["last_active"] >= week_ago)
        active_30d = sum(1 for u in priv_u if u["is_left"] == 0 and u["last_active"] >= month_ago)

        cached = len(MEM["media_cache"])
        total_subs = len(MEM["sub_logs"])
        total_reqs = int(MEM["settings"].get("total_requests", "0"))
        ad_views = sum(a["current_views"] for a in MEM["ads"].values())

    return {
        "total_priv": total_priv,
        "total_grp": total_grp,
        "alive_priv": alive_priv,
        "alive_grp": alive_grp,
        "left_priv": left_priv,
        "left_grp": left_grp,
        "banned_cnt": banned_cnt,
        "joined_24h": joined_24h,
        "joined_7d": joined_7d,
        "joined_30d": joined_30d,
        "active_24h": active_24h,
        "active_7d": active_7d,
        "active_30d": active_30d,
        "cached": cached,
        "total_subs": total_subs,
        "total_reqs": total_reqs,
        "ad_views": ad_views
    }

def get_all_targets():
    with MEM_LOCK:
        return [uid for uid, u in MEM["users"].items() if u["is_banned"] == 0 and u["is_left"] == 0]

# ==========================================
# 6. ENG TEZ DOIMIY MAJBURIY OBUNA (PARALLEL + RAM + SUPABASE)
# ==========================================
def add_channel(channel_id: str, title: str, ch_type: str, channel_url: str, limit: int = 0):
    cid = str(channel_id)
    token = generate_stat_token("ch")
    clean_url = normalize_tg_url(channel_url)
    with MEM_LOCK:
        MEM["channels"][cid] = {
            "channel_id": cid, "title": title, "channel_type": ch_type,
            "channel_url": clean_url, "target_limit": limit,
            "current_count": 0, "shown_count": 0, "stat_token": token
        }
        MEM["sub_ttl"] = {k: v for k, v in MEM["sub_ttl"].items() if k[1] != cid}
    exec_immediate_pg(
        """
        INSERT INTO channels (channel_id, title, channel_type, channel_url, target_limit, current_count, shown_count, stat_token)
        VALUES (%s, %s, %s, %s, %s, 0, 0, %s)
        ON CONFLICT (channel_id) DO UPDATE SET
            title = EXCLUDED.title, channel_type = EXCLUDED.channel_type,
            channel_url = EXCLUDED.channel_url, target_limit = EXCLUDED.target_limit,
            current_count = 0, shown_count = 0, stat_token = EXCLUDED.stat_token
        """,
        (cid, title, ch_type, clean_url, limit, token)
    )
    return token

def remove_channel(channel_id: str):
    cid = str(channel_id)
    with MEM_LOCK:
        MEM["channels"].pop(cid, None)
        MEM["join_requests"] = {x for x in MEM["join_requests"] if x[1] != cid}
        MEM["sub_log_keys"] = {x for x in MEM["sub_log_keys"] if x[1] != cid}
        MEM["sub_logs"] = [x for x in MEM["sub_logs"] if x["channel_id"] != cid]
        MEM["sub_ttl"] = {k: v for k, v in MEM["sub_ttl"].items() if k[1] != cid}
    exec_immediate_pg("DELETE FROM channels WHERE channel_id = %s", (cid,))
    exec_immediate_pg("DELETE FROM join_requests WHERE channel_id = %s", (cid,))
    exec_immediate_pg("DELETE FROM sub_logs WHERE channel_id = %s", (cid,))

def get_channels_ram():
    with MEM_LOCK:
        return list(MEM["channels"].values())

def increment_channel_shown(channel_ids):
    if not channel_ids:
        return
    with MEM_LOCK:
        for cid in channel_ids:
            ch = MEM["channels"].get(str(cid))
            if ch:
                ch["shown_count"] += 1
                SYNC_QUEUE["ch_counts"].add(str(cid))

def record_subscription_user(user, channel_id: str):
    cid = str(channel_id)
    uid = user.id
    full_name = format_user_name(user)
    username = format_username(user)
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    limit_reached_title = None
    limit_reached_val = 0

    with MEM_LOCK:
        if (uid, cid) in MEM["sub_log_keys"]:
            return
        MEM["sub_log_keys"].add((uid, cid))
        MEM["sub_logs"].append({
            "user_id": uid, "channel_id": cid,
            "first_name": full_name, "username": username, "subscribed_at": now_str
        })
        SYNC_QUEUE["sub_logs"].append((uid, cid, full_name, username, now_str))

        ch = MEM["channels"].get(cid)
        if ch:
            ch["current_count"] += 1
            SYNC_QUEUE["ch_counts"].add(cid)
            if ch["target_limit"] > 0 and ch["current_count"] >= ch["target_limit"]:
                limit_reached_title = ch["title"]
                limit_reached_val = ch["target_limit"]
                MEM["channels"].pop(cid, None)

    if limit_reached_title:
        exec_immediate_pg("DELETE FROM channels WHERE channel_id = %s", (cid,))
        try:
            bot.send_message(
                SUPER_ADMIN_ID,
                f"✅ <b>Limit to'ldi!</b>\n\n📢 <b>{esc(limit_reached_title)}</b> ({limit_reached_val} ta obunachi) majburiy obunadan avtomatik olib tashlandi.",
                parse_mode="HTML"
            )
        except Exception:
            pass

@bot.chat_join_request_handler()
def handle_join_request(message: types.ChatJoinRequest):
    cid = str(message.chat.id)
    u = message.from_user
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    with MEM_LOCK:
        MEM["join_requests"].add((u.id, cid))
        MEM["sub_ttl"][(u.id, cid)] = {"ts": time.time(), "ok": True}
        SYNC_QUEUE["join_req"].append((u.id, cid, format_user_name(u), format_username(u), now_str))
    record_subscription_user(u, cid)

def _check_single_channel_member(uid: int, cid: str):
    try:
        member = bot.get_chat_member(chat_id=cid, user_id=uid)
        return member.status not in ["left", "kicked"]
    except Exception:
        return False

def check_subscription_fast(user, force_fresh: bool = False):
    uid = user.id
    if is_admin(uid):
        return []
    channels = get_channels_ram()
    if not channels:
        return []

    now = time.time()
    not_subscribed = []
    to_query_api = []

    with MEM_LOCK:
        for ch in channels:
            cid = ch["channel_id"]
            safe_url = normalize_tg_url(ch["channel_url"])
            cache_key = (uid, cid)

            if cache_key in MEM["join_requests"] or cache_key in MEM["sub_log_keys"]:
                MEM["sub_ttl"][cache_key] = {"ts": now, "ok": True}
                continue

            cached_sub = MEM["sub_ttl"].get(cache_key)
            if not force_fresh and cached_sub:
                ttl = 60.0 if cached_sub["ok"] else 8.0
                if now - cached_sub["ts"] < ttl:
                    if not cached_sub["ok"]:
                        not_subscribed.append((cid, ch["title"], ch["channel_type"], safe_url))
                    continue

            to_query_api.append((cid, ch["title"], ch["channel_type"], safe_url))

    if to_query_api:
        futures = {
            SUB_CHECK_POOL.submit(_check_single_channel_member, uid, item[0]): item
            for item in to_query_api
        }
        for fut in as_completed(futures, timeout=3.0):
            cid, title, ch_type, safe_url = futures[fut]
            try:
                ok = bool(fut.result())
            except Exception:
                ok = False

            with MEM_LOCK:
                MEM["sub_ttl"][(uid, cid)] = {"ts": time.time(), "ok": ok}

            if ok:
                record_subscription_user(user, cid)
            else:
                not_subscribed.append((cid, title, ch_type, safe_url))

    if not_subscribed:
        increment_channel_shown([item[0] for item in not_subscribed])

    return not_subscribed

def sub_keyboard(user_id: int, channels):
    markup = types.InlineKeyboardMarkup()
    for _, title, ch_type, url in channels:
        safe_url = normalize_tg_url(url)
        if not safe_url:
            continue
        icon = "📩" if ch_type == "request" else ("🔒" if ch_type == "private" else "📢")
        markup.add(types.InlineKeyboardButton(text=f"{icon} {title}", url=safe_url))
    markup.add(types.InlineKeyboardButton(text=t(user_id, "sub_btn"), callback_data="check_sub"))
    return markup

def gate_user(message: types.Message) -> bool:
    uid = message.from_user.id
    register_chat(message.chat.id, message.chat.type)
    inc_total_requests()
    if is_user_banned(uid):
        return False

    if message.chat.type == "private":
        if not get_user_lang(uid):
            bot.send_message(message.chat.id, LANG_TEXTS["uz"]["choose_lang"], reply_markup=lang_keyboard(), parse_mode="HTML")
            return False

        not_sub = check_subscription_fast(message.from_user)
        if not_sub:
            bot.send_message(message.chat.id, t(uid, "sub_req"), reply_markup=sub_keyboard(uid, not_sub), parse_mode="HTML")
            return False

        maybe_send_popup_ad(message.chat.id, uid)

    return True

# ==========================================
# 7. STATISTIKA VA EXCEL HISOBOT (RAM DAN)
# ==========================================
def get_channel_live_report(channel_id: str, limit_logs: int = 12, include_users: bool = True):
    cid = str(channel_id)
    with MEM_LOCK:
        ch = MEM["channels"].get(cid)
        ch_logs = [x for x in MEM["sub_logs"] if x["channel_id"] == cid]

    if not ch and not ch_logs:
        return None

    title = ch["title"] if ch else f"Kanal ({cid})"
    target = ch["target_limit"] if ch else 0
    current = max(ch["current_count"] if ch else 0, len(ch_logs))
    shown = ch["shown_count"] if ch else 0
    conv = f"{min(100, round((current / shown) * 100, 1))}%" if shown > 0 else "—"
    pct = f"{min(100, round((current / target) * 100, 1))}%" if target > 0 else "—"
    rem = max(0, target - current) if target > 0 else "∞"

    lines = [
        f"📡 <b>Real-Time Statistika: {esc(title)}</b>",
        f"👁 Ko'rganlar soni: <b>{shown}</b>",
        f"👥 Obuna bo'lganlar: <b>{current}</b> / <b>{target if target > 0 else '∞'}</b>",
        f"📈 Konversiya: <b>{conv}</b> | Limit bajarilishi: <b>{pct}</b> | Qolgan: <b>{rem}</b>",
        f"🕒 Real-time vaqt: <code>{time.strftime('%H:%M:%S')}</code>"
    ]
    if include_users:
        lines.append("\n<b>Kimlar obuna bo'ldi / o'tdi:</b>")
        recent = list(reversed(ch_logs[-limit_logs:]))
        if recent:
            for item in recent:
                lines.append(f"• <b>{esc(item['first_name'])}</b> ({esc(item['username'])}) | <code>{item['user_id']}</code>\n  └ <i>{esc(item['subscribed_at'] or '')}</i>")
        else:
            lines.append("<i>Hali yozuvlar yo'q.</i>")
    return "\n".join(lines)

def build_client_token_report(token: str):
    now = time.time()
    with MEM_LOCK:
        ad = next((a for a in MEM["ads"].values() if a["stat_token"] == token), None)
        ch = next((c for c in MEM["channels"].values() if c["stat_token"] == token), None)

    if ad:
        is_live = (ad["is_active"] == 1 and (ad["expires_at"] == 0 or now < ad["expires_at"]) and (ad["max_views"] == 0 or ad["current_views"] < ad["max_views"]))
        status = "🟢 Faol" if is_live else "🔴 Yakunlangan"
        pct = f"{min(100, round((ad['current_views'] / ad['max_views']) * 100, 1))}%" if ad["max_views"] > 0 else "—"
        rem_time = f"{max(0, round((ad['expires_at'] - now) / 3600, 1))} soat" if ad["expires_at"] > 0 else "Cheksiz"
        return (
            f"📊 <b>Real-Time Reklama Statistikasi #{ad['id']}</b>\n\n"
            f"📌 Holat: <b>{status}</b>\n"
            f"📝 Matn: <b>{esc(ad['text'])}</b>\n"
            f"👁 Ko'rishlar: <b>{ad['current_views']}</b> / <b>{ad['max_views'] if ad['max_views'] > 0 else '∞'}</b> ({pct})\n"
            f"⏳ Qolgan vaqt: <b>{rem_time}</b>\n"
            f"🕒 Real-time vaqt: <code>{time.strftime('%H:%M:%S')}</code>"
        )

    if ch:
        return get_channel_live_report(ch["channel_id"], limit_logs=10, include_users=True)

    return None

def generate_excel_csv_bytes(scope: str = "all"):
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")

    with MEM_LOCK:
        ads_list = sorted(MEM["ads"].values(), key=lambda x: x["id"], reverse=True)
        ch_list = list(MEM["channels"].values())
        logs_list = list(reversed(MEM["sub_logs"]))

    if scope == "all":
        st = get_detailed_stats()
        writer.writerow(["=== BOT UMUMIY STATISTIKASI ==="])
        writer.writerow(["Ko'rsatkich", "Qiymat"])
        writer.writerow(["Jami shaxsiy foydalanuvchilar", st["total_priv"]])
        writer.writerow(["Hozirda faol (tirik) foydalanuvchilar", st["alive_priv"]])
        writer.writerow(["Chiqib ketgan / bloklagan foydalanuvchilar", st["left_priv"]])
        writer.writerow(["Admin bloklaganlar", st["banned_cnt"]])
        writer.writerow(["Jami guruhlar", st["total_grp"]])
        writer.writerow(["Faol guruhlar", st["alive_grp"]])
        writer.writerow(["Chiqib ketgan guruhlar", st["left_grp"]])
        writer.writerow(["Bugun (24s) qo'shilganlar", st["joined_24h"]])
        writer.writerow(["Shu hafta (7k) qo'shilganlar", st["joined_7d"]])
        writer.writerow(["Shu oy (30k) qo'shilganlar", st["joined_30d"]])
        writer.writerow(["Bugun faol foydalanuvchilar", st["active_24h"]])
        writer.writerow(["7 kunda faol foydalanuvchilar", st["active_7d"]])
        writer.writerow(["30 kunda faol foydalanuvchilar", st["active_30d"]])
        writer.writerow(["Jami so'rovlar", st["total_reqs"]])
        writer.writerow(["Keshdagi fayllar", st["cached"]])

        writer.writerow([])
        writer.writerow(["=== REKLAMALAR STATISTIKASI ==="])
        writer.writerow(["ID", "Turi", "Media", "Matn", "Havola", "Ko'rishlar", "Limit", "Holat"])
        for r in ads_list:
            writer.writerow([r["id"], r["ad_type"], r["media_type"], r["text"], r["url"], r["current_views"], r["max_views"] if r["max_views"] > 0 else "Cheksiz", "Faol" if r["is_active"] == 1 else "Tugagan"])

        writer.writerow([])
        writer.writerow(["=== MAJBURIY OBUNA KANALLARI ==="])
        writer.writerow(["Kanal ID", "Nomi", "Turi", "Ko'rganlar", "Obuna bo'lganlar", "Limit", "Konversiya (%)"])
        for c in ch_list:
            shown = c["shown_count"]
            conv = round((c["current_count"] / shown) * 100, 2) if shown > 0 else 0
            writer.writerow([c["channel_id"], c["title"], c["channel_type"], shown, c["current_count"], c["target_limit"] if c["target_limit"] > 0 else "Cheksiz", conv])

        writer.writerow([])
        writer.writerow(["=== OBUNA BO'LGAN FOYDALANUVCHILAR LOGI ==="])
        writer.writerow(["Kanal ID", "User ID", "Ism", "Username", "Sana va Vaqt"])
        for l in logs_list:
            writer.writerow([l["channel_id"], l["user_id"], l["first_name"], l["username"], l["subscribed_at"]])

    elif scope.startswith("ad_"):
        r = next((a for a in ads_list if a["stat_token"] == scope), None)
        if not r:
            return None, None
        writer.writerow(["Reklama ID", "Turi", "Matn", "Havola", "Ko'rishlar", "Limit", "Holat"])
        writer.writerow([r["id"], r["ad_type"], r["text"], r["url"], r["current_views"], r["max_views"] if r["max_views"] > 0 else "Cheksiz", "Faol" if r["is_active"] == 1 else "Tugagan"])

    elif scope.startswith("ch_"):
        c = next((x for x in ch_list if x["stat_token"] == scope), None)
        if not c:
            return None, None
        shown = c["shown_count"]
        conv = round((c["current_count"] / shown) * 100, 2) if shown > 0 else 0
        writer.writerow(["Kanal", "Turi", "Ko'rganlar", "Obuna bo'lganlar", "Limit", "Konversiya (%)"])
        writer.writerow([c["title"], c["channel_type"], shown, c["current_count"], c["target_limit"] if c["target_limit"] > 0 else "Cheksiz", conv])
        writer.writerow([])
        writer.writerow(["User ID", "Ism", "Username", "Obuna vaqti"])
        for l in logs_list:
            if l["channel_id"] == c["channel_id"]:
                writer.writerow([l["user_id"], l["first_name"], l["username"], l["subscribed_at"]])

    raw_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    bio = io.BytesIO(raw_bytes)
    bio.name = f"statistika_{scope}_{time.strftime('%Y%m%d_%H%M')}.csv"
    return bio, bio.name

@bot.callback_query_handler(func=lambda call: call.data.startswith("export_xl:"))
def export_xl_cb(call: types.CallbackQuery):
    scope = call.data.split(":", 1)[1]
    if scope == "all" and not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Ruxsat yo'q.", show_alert=True)
        return
    bio, fname = generate_excel_csv_bytes(scope)
    if not bio:
        bot.answer_callback_query(call.id, "❌ Ma'lumot topilmadi.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "📥 Excel fayl tayyorlandi!")
    bot.send_document(call.message.chat.id, bio, visible_file_name=fname, caption="📊 <b>Excel Statistika hisoboti</b>", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("refresh_stat:"))
def refresh_stat_cb(call: types.CallbackQuery):
    target_key = call.data.split(":", 1)[1]
    if target_key.startswith(("ad_", "ch_")):
        report = build_client_token_report(target_key)
    else:
        report = get_channel_live_report(target_key, limit_logs=12, include_users=True)

    if not report:
        bot.answer_callback_query(call.id, "❌ Statistika topilmadi.", show_alert=True)
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text=t(call.from_user.id, "refresh"), callback_data=f"refresh_stat:{target_key}"))
    if target_key.startswith(("ad_", "ch_")):
        markup.add(types.InlineKeyboardButton(text=t(call.from_user.id, "excel_btn"), callback_data=f"export_xl:{target_key}"))
    try:
        bot.edit_message_text(report, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        bot.answer_callback_query(call.id, "✅")
    except Exception:
        bot.answer_callback_query(call.id)

# ==========================================
# 8. HAR QANDAY REKLAMA TIZIMI
# ==========================================
def add_ad_db(ad_type: str, text: str, url: str = "", max_views: int = 0, hours: float = 0, is_fallback: int = 0, media_type: str = "text", media_file_id: str = ""):
    expires_at = (time.time() + hours * 3600) if hours > 0 else 0.0
    token = generate_stat_token("ad")
    clean_url = url.strip() if ad_type == "popup" else (normalize_tg_url(url) if url else "")

    conn = get_pg_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ads (ad_type, text, url, media_type, media_file_id, max_views, current_views, clicks, expires_at, is_fallback, is_active, stat_token)
        VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, %s, 1, %s)
        RETURNING id
        """,
        (ad_type, text, clean_url, media_type, media_file_id, max_views, expires_at, is_fallback, token)
    )
    ad_id = cur.fetchone()[0]
    cur.close()
    conn.close()

    with MEM_LOCK:
        MEM["ads"][ad_id] = {
            "id": ad_id, "ad_type": ad_type, "text": text, "url": clean_url,
            "media_type": media_type, "media_file_id": media_file_id,
            "max_views": max_views, "current_views": 0, "clicks": 0,
            "expires_at": expires_at, "is_fallback": is_fallback,
            "is_active": 1, "stat_token": token
        }
    return token

def delete_ad_db(ad_id: int):
    with MEM_LOCK:
        MEM["ads"].pop(ad_id, None)
        SYNC_QUEUE["ad_views"].pop(ad_id, None)
    exec_immediate_pg("DELETE FROM ads WHERE id = %s", (ad_id,))

def get_all_ads_with_stats():
    now = time.time()
    expired_ids = []
    with MEM_LOCK:
        for a in MEM["ads"].values():
            if a["is_active"] == 1 and ((a["expires_at"] > 0 and now >= a["expires_at"]) or (a["max_views"] > 0 and a["current_views"] >= a["max_views"])):
                a["is_active"] = 0
                expired_ids.append(a["id"])
        result = sorted([dict(a) for a in MEM["ads"].values()], key=lambda x: x["id"], reverse=True)

    for eid in expired_ids:
        exec_immediate_pg("UPDATE ads SET is_active = 0 WHERE id = %s", (eid,))
    return result

def pick_ad(ad_type: str):
    ads = [a for a in get_all_ads_with_stats() if a["is_active"] == 1]
    if ad_type == "popup":
        pool = [a for a in ads if a["ad_type"] == "popup"]
    else:
        primary = [a for a in ads if a["ad_type"] == ad_type and a["is_fallback"] == 0]
        fallback = [a for a in ads if a["ad_type"] != "popup" and (a["is_fallback"] == 1 or a["ad_type"] == ad_type)]
        pool = primary if primary else fallback

    if not pool:
        return None

    chosen = random.choice(pool)
    with MEM_LOCK:
        ad_ref = MEM["ads"].get(chosen["id"])
        if ad_ref:
            ad_ref["current_views"] += 1
            SYNC_QUEUE["ad_views"][chosen["id"]] = SYNC_QUEUE["ad_views"].get(chosen["id"], 0) + 1
    return chosen

def maybe_send_popup_ad(chat_id: int, user_id: int):
    if is_admin(user_id):
        return
    now = time.time()
    with MEM_LOCK:
        last_ts = MEM["popup_cd"].get(user_id, 0)
        if now - last_ts < 900:
            return

    ad = pick_ad("popup")
    if not ad:
        return

    with MEM_LOCK:
        MEM["popup_cd"][user_id] = now

    markup = parse_url_buttons_text(ad["url"])
    mtype = ad.get("media_type", "text")
    fid = ad.get("media_file_id", "")
    caption_txt = ad["text"] if ad["text"] != "-" else None

    try:
        if mtype == "photo" and fid:
            bot.send_photo(chat_id, fid, caption=caption_txt, reply_markup=markup, parse_mode="HTML")
        elif mtype == "video" and fid:
            bot.send_video(chat_id, fid, caption=caption_txt, reply_markup=markup, parse_mode="HTML")
        elif mtype == "animation" and fid:
            bot.send_animation(chat_id, fid, caption=caption_txt, reply_markup=markup, parse_mode="HTML")
        elif mtype == "document" and fid:
            bot.send_document(chat_id, fid, caption=caption_txt, reply_markup=markup, parse_mode="HTML")
        elif mtype == "audio" and fid:
            bot.send_audio(chat_id, fid, caption=caption_txt, reply_markup=markup, parse_mode="HTML")
        elif mtype == "voice" and fid:
            bot.send_voice(chat_id, fid, caption=caption_txt, reply_markup=markup, parse_mode="HTML")
        elif caption_txt:
            bot.send_message(chat_id, caption_txt, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=False)
    except Exception:
        pass

def get_caption_ad_line() -> str:
    ad = pick_ad("caption")
    if not ad:
        return ""
    safe_url = normalize_tg_url(ad["url"]) if ad["url"] else ""
    if safe_url:
        return f"\n<a href=\"{esc(safe_url)}\">{esc(ad['text'])}</a>"
    return f"\n{esc(ad['text'])}"

def get_banner_ad_line() -> str:
    ad = pick_ad("banner")
    if not ad:
        return ""
    safe_url = normalize_tg_url(ad["url"]) if ad["url"] else ""
    if safe_url:
        return f"\n\n✨ <a href=\"{esc(safe_url)}\">{esc(ad['text'])}</a>"
    return f"\n\n✨ {esc(ad['text'])}"

def get_ad_button():
    ad = pick_ad("button")
    if ad and ad["url"]:
        safe_url = normalize_tg_url(ad["url"])
        if safe_url:
            return types.InlineKeyboardButton(text=ad["text"][:40], url=safe_url)
    return None

# ==========================================
# 9. /START, /LANG, /TOP, /FAV
# ==========================================
def get_admin_reply_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("👑 Admin Panel"))
    return kb

def send_home_menu(chat_id: int, user_id: int, is_private: bool):
    bot_username = get_bot_username()
    text = t(user_id, "start_msg", bot_user=esc(bot_username))
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(text=t(user_id, "inline_try_btn"), switch_inline_query=""),
        types.InlineKeyboardButton(text=t(user_id, "add_group"), url=f"https://t.me/{bot_username}?startgroup=true")
    )
    ad_btn = get_ad_button()
    if ad_btn:
        markup.add(ad_btn)

    if is_private and is_admin(user_id):
        bot.send_message(chat_id, "👑 <b>Admin menyusi faollashtirildi.</b>", reply_markup=get_admin_reply_keyboard(), parse_mode="HTML")

    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=["lang"])
def lang_cmd(message: types.Message):
    register_chat(message.chat.id, message.chat.type)
    if is_user_banned(message.from_user.id):
        return
    bot.send_message(message.chat.id, t(message.from_user.id, "choose_lang"), reply_markup=lang_keyboard(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_lang:"))
def set_lang_cb(call: types.CallbackQuery):
    lang_code = call.data.split(":", 1)[1]
    uid = call.from_user.id
    set_user_lang(uid, lang_code)
    setup_user_commands(uid, lang_code)
    bot.answer_callback_query(call.id, "✅")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, t(uid, "lang_saved"), parse_mode="HTML")

    not_sub = check_subscription_fast(call.from_user, force_fresh=True)
    if not_sub:
        bot.send_message(call.message.chat.id, t(uid, "sub_req"), reply_markup=sub_keyboard(uid, not_sub), parse_mode="HTML")
    else:
        send_home_menu(call.message.chat.id, uid, is_private=(call.message.chat.type == "private"))

@bot.message_handler(commands=["start"])
def start_cmd(message: types.Message):
    uid = message.from_user.id
    register_chat(message.chat.id, message.chat.type)
    if is_user_banned(uid):
        return

    parts = message.text.split(maxsplit=1)
    if message.chat.type == "private" and len(parts) == 2:
        param = parts[1].strip()
        if param.startswith("stat_"):
            token = param.replace("stat_", "", 1).strip()
            report = build_client_token_report(token)
            if report:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(text=t(uid, "refresh"), callback_data=f"refresh_stat:{token}"))
                markup.add(types.InlineKeyboardButton(text=t(uid, "excel_btn"), callback_data=f"export_xl:{token}"))
                bot.send_message(message.chat.id, report, reply_markup=markup, parse_mode="HTML")
                return
            bot.send_message(message.chat.id, "❌ <b>Statistika havolasi topilmadi yoki eskirgan.</b>", parse_mode="HTML")
            return
        elif param == "shazam":
            if gate_user(message):
                bot.send_message(message.chat.id, t(uid, "shazam_prompt"), parse_mode="HTML")
            return

    if message.chat.type == "private" and not get_user_lang(uid):
        bot.send_message(message.chat.id, LANG_TEXTS["uz"]["choose_lang"], reply_markup=lang_keyboard(), parse_mode="HTML")
        return

    if message.chat.type == "private":
        not_sub = check_subscription_fast(message.from_user)
        if not_sub:
            bot.send_message(message.chat.id, t(uid, "sub_req"), reply_markup=sub_keyboard(uid, not_sub), parse_mode="HTML")
            return

    send_home_menu(message.chat.id, uid, is_private=(message.chat.type == "private"))

def get_country_info(code: str):
    for c in TOP_COUNTRIES:
        if c["code"] == code:
            return c
    return TOP_COUNTRIES[0]

def get_country_top_songs(country_code: str):
    now = time.time()
    cached = TOP_COUNTRY_CACHE.get(country_code)
    if cached and (now - cached["ts"] < TOP_CACHE_TTL) and cached["entries"]:
        return cached["entries"]

    lock = TOP_FETCH_LOCKS.setdefault(country_code, threading.Lock())
    with lock:
        now = time.time()
        cached = TOP_COUNTRY_CACHE.get(country_code)
        if cached and (now - cached["ts"] < TOP_CACHE_TTL) and cached["entries"]:
            return cached["entries"]

        c_info = get_country_info(country_code)
        combined = []
        seen = set()

        for q in c_info["queries"]:
            try:
                items = _yt_flat_search(f"ytsearch10:{q}", filter_mixes=True)
                for it in items:
                    if it["id"] not in seen:
                        seen.add(it["id"])
                        combined.append(it)
                if len(combined) >= 20:
                    break
            except Exception:
                continue

        final_list = combined[:20]
        if final_list:
            TOP_COUNTRY_CACHE[country_code] = {"ts": time.time(), "entries": final_list}
        elif cached and cached["entries"]:
            return cached["entries"]
        return final_list

def background_top_warmer():
    time.sleep(5)
    while True:
        for c in TOP_COUNTRIES:
            try:
                entries = get_country_top_songs(c["code"])
                for item in entries[:6]:
                    if not get_cached_media(f"aud:{item['url']}", item.get("title", "")):
                        try:
                            base_path = f"downloads/pre_{c['code']}_{int(time.time()*1000)}_{random.randint(10,99)}"
                            aud_file, dl_title, dur = download_file_turbo(item["url"], base_path, mode="audio")
                            if aud_file and os.path.exists(aud_file):
                                with open(aud_file, "rb") as aud:
                                    sent_msg = bot.send_audio(
                                        SUPER_ADMIN_ID,
                                        aud,
                                        title=item.get("title") or dl_title,
                                        duration=int(dur) if dur else None,
                                        disable_notification=True
                                    )
                                if sent_msg and sent_msg.audio:
                                    save_cached_media(f"aud:{item['url']}", sent_msg.audio.file_id, "audio", item.get("title") or dl_title)
                                    try:
                                        bot.delete_message(SUPER_ADMIN_ID, sent_msg.message_id)
                                    except Exception:
                                        pass
                                os.remove(aud_file)
                        except Exception:
                            pass
                        time.sleep(1.5)
            except Exception:
                pass
            time.sleep(2)
        time.sleep(600)

def open_top_for_country(chat_id: int, user_id: int, country_code: str = "uz", edit_msg_id: int = None):
    c_info = get_country_info(country_code)
    lang = get_user_lang(user_id) or "uz"
    c_name = c_info.get(f"name_{lang}", c_info["name_uz"])

    cached = TOP_COUNTRY_CACHE.get(country_code)
    temp_msg_id = edit_msg_id

    if not cached or not cached.get("entries"):
        if not temp_msg_id:
            sent_wait = bot.send_message(chat_id, t(user_id, "top_loading"), parse_mode="HTML")
            temp_msg_id = sent_wait.message_id

    entries = get_country_top_songs(country_code)
    if not entries:
        if temp_msg_id:
            try:
                bot.edit_message_text(t(user_id, "search_not_found"), chat_id, temp_msg_id, parse_mode="HTML")
                return
            except Exception:
                pass
        bot.send_message(chat_id, t(user_id, "search_not_found"), parse_mode="HTML")
        return

    header_title = t(user_id, "top_header", flag=c_info["flag"], country=c_name)
    SEARCH_CACHE[f"srch_{user_id}"] = {
        "query": header_title,
        "entries": entries,
        "mode": "top",
        "country": country_code
    }
    text, markup = build_search_page(user_id, page=0)
    if temp_msg_id:
        try:
            bot.edit_message_text(text, chat_id, temp_msg_id, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=["top"])
def top_cmd(message: types.Message):
    if not gate_user(message):
        return
    open_top_for_country(message.chat.id, message.from_user.id, country_code="uz")

@bot.callback_query_handler(func=lambda call: call.data.startswith("top_cnt:"))
def top_country_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    if call.message.chat.type == "private":
        not_sub = check_subscription_fast(call.from_user)
        if not_sub:
            bot.answer_callback_query(call.id, t(uid, "sub_not_yet"), show_alert=True)
            return
    country_code = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)
    open_top_for_country(call.message.chat.id, uid, country_code=country_code, edit_msg_id=call.message.message_id)

@bot.message_handler(commands=["fav"])
def fav_cmd(message: types.Message):
    if not gate_user(message):
        return
    uid = message.from_user.id
    favs = get_user_favorites(uid)
    if not favs:
        bot.send_message(message.chat.id, t(uid, "fav_empty"), parse_mode="HTML")
        return
    SEARCH_CACHE[f"srch_{uid}"] = {'query': t(uid, "fav_title"), 'entries': favs, 'mode': 'fav'}
    text, markup = build_search_page(uid, page=0)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("fav_toggle:"))
def fav_toggle_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    if call.message.chat.type == "private":
        not_sub = check_subscription_fast(call.from_user)
        if not_sub:
            bot.answer_callback_query(call.id, t(uid, "sub_not_yet"), show_alert=True)
            return
    fav_key = call.data.split(":", 1)[1]
    item = SEARCH_CACHE.get(fav_key)
    if not item:
        bot.answer_callback_query(call.id, t(uid, "expired"), show_alert=True)
        return
    added = toggle_favorite_db(uid, item["title"], item["url"])
    bot.answer_callback_query(call.id, t(uid, "fav_added" if added else "fav_removed"))

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    register_chat(call.message.chat.id, call.message.chat.type)
    not_sub = check_subscription_fast(call.from_user, force_fresh=True)
    if not_sub:
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=sub_keyboard(uid, not_sub))
        except Exception:
            pass
        bot.answer_callback_query(call.id, t(uid, "sub_not_yet"), show_alert=True)
    else:
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, t(uid, "sub_ok"), parse_mode="HTML")

# ==========================================
# 10. ADMIN PANEL
# ==========================================
def admin_main_keyboard():
    tag_status = "✅ Yoqilgan" if is_show_bot_tag() else "❌ O'chirilgan"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(text="📊 Batafsil Statistika", callback_data="adm:stats"),
        types.InlineKeyboardButton(text="📥 Excel Hisobot", callback_data="export_xl:all"),
        types.InlineKeyboardButton(text="📢 Kanallar / Guruhlar", callback_data="adm:channels"),
        types.InlineKeyboardButton(text="📣 Reklamalar (Har qanday)", callback_data="adm:ads"),
        types.InlineKeyboardButton(text=f"🏷 Bot nomi: {tag_status}", callback_data="adm:toggle_tag"),
        types.InlineKeyboardButton(text="📨 Universal Rassilka", callback_data="adm:broadcast"),
        types.InlineKeyboardButton(text="👮‍♂️ Adminlar", callback_data="adm:admins"),
        types.InlineKeyboardButton(text="👤 User Ban/Unban", callback_data="adm:ban_menu"),
        types.InlineKeyboardButton(text="⚡ Keshni tozalash", callback_data="adm:clear_cache"),
        types.InlineKeyboardButton(text="🔄 Supabase Sync", callback_data="adm:force_sync")
    )
    return markup

def get_admin_dashboard_text():
    st = get_detailed_stats()
    tag_txt = "Ko'rinadi (@bot)" if is_show_bot_tag() else "Yashirilgan"
    return (
        f"👑 <b>Boshqaruv Paneli (Supabase + RAM)</b>\n\n"
        f"👤 Shaxsiy: <b>{st['total_priv']}</b> (🟢 Faol: <b>{st['alive_priv']}</b> | 🔴 Chiqqan: <b>{st['left_priv']}</b>)\n"
        f"👥 Guruhlar: <b>{st['total_grp']}</b> (🟢 Faol: <b>{st['alive_grp']}</b> | 🔴 Chiqqan: <b>{st['left_grp']}</b>)\n"
        f"📈 Qo'shilgan: Bugun <b>+{st['joined_24h']}</b> | 7 kun <b>+{st['joined_7d']}</b> | 30 kun <b>+{st['joined_30d']}</b>\n"
        f"⚡ Faollik: Bugun <b>{st['active_24h']}</b> | 7 kun <b>{st['active_7d']}</b> | 30 kun <b>{st['active_30d']}</b>\n"
        f"🚀 Jami so'rovlar: <b>{st['total_reqs']}</b> | 💾 Kesh: <b>{st['cached']}</b> | 👁 Reklama: <b>{st['ad_views']}</b>\n"
        f"🏷 Media tagidagi bot nomi: <b>{tag_txt}</b>"
    )

@bot.message_handler(func=lambda m: (m.text in ["/admin", "👑 Admin Panel"]) and is_admin(m.from_user.id))
def open_admin_panel(message: types.Message):
    ADMIN_STATE.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, get_admin_dashboard_text(), reply_markup=admin_main_keyboard(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm:") and is_admin(call.from_user.id))
def admin_callbacks(call: types.CallbackQuery):
    global BROADCAST_STOP
    uid = call.from_user.id
    action = call.data.split(":")[1]
    bot_username = get_bot_username()

    if action == "back":
        ADMIN_STATE.pop(uid, None)
        bot.edit_message_text(get_admin_dashboard_text(), call.message.chat.id, call.message.message_id, reply_markup=admin_main_keyboard(), parse_mode="HTML")

    elif action == "toggle_tag":
        state = toggle_show_bot_tag()
        bot.answer_callback_query(call.id, f"🏷 Media tagida bot nomi: {'✅ Yoqildi' if state else '❌ Ochirildi'}")
        bot.edit_message_text(get_admin_dashboard_text(), call.message.chat.id, call.message.message_id, reply_markup=admin_main_keyboard(), parse_mode="HTML")

    elif action == "force_sync":
        threading.Thread(target=flush_sync_queue_to_supabase, daemon=True).start()
        bot.answer_callback_query(call.id, "✅ RAM ma'lumotlari Supabase'ga sinxronlandi!", show_alert=True)

    elif action == "stats":
        st = get_detailed_stats()
        alive_pct = f"{round((st['alive_priv'] / st['total_priv']) * 100, 1)}%" if st["total_priv"] > 0 else "100%"
        text = (
            f"📊 <b>Botning To'liq Real-Time Statistikasi</b>\n\n"
            f"<b>1. Auditoriya holati (Shaxsiy):</b>\n"
            f"• Jami ro'yxatdan o'tgan: <b>{st['total_priv']}</b>\n"
            f"• 🟢 Hozirda faol (tirik): <b>{st['alive_priv']}</b> ({alive_pct})\n"
            f"• 🔴 Chiqib ketgan / bloklagan: <b>{st['left_priv']}</b>\n"
            f"• 🚫 Admin bloklagan: <b>{st['banned_cnt']}</b>\n\n"
            f"<b>2. Guruhlar holati:</b>\n"
            f"• Jami guruhlar: <b>{st['total_grp']}</b>\n"
            f"• 🟢 Faol guruhlar: <b>{st['alive_grp']}</b> | 🔴 Chiqarilgan: <b>{st['left_grp']}</b>\n\n"
            f"<b>3. Yangi qo'shilganlar (O'sish):</b>\n"
            f"• Oxirgi 24 soat (Bugun): <b>+{st['joined_24h']}</b>\n"
            f"• Oxirgi 7 kun (Haftalik): <b>+{st['joined_7d']}</b>\n"
            f"• Oxirgi 30 kun (Oylik): <b>+{st['joined_30d']}</b>\n\n"
            f"<b>4. Foydalanuvchilar faolligi (Aktivlik):</b>\n"
            f"• Bugun faol bo'lganlar: <b>{st['active_24h']}</b>\n"
            f"• 7 kun ichida faollar: <b>{st['active_7d']}</b>\n"
            f"• 30 kun ichida faollar: <b>{st['active_30d']}</b>\n\n"
            f"<b>5. Tizim ko'rsatkichlari:</b>\n"
            f"• 🚀 Jami yuklash/qidiruv so'rovlari: <b>{st['total_reqs']}</b>\n"
            f"• 📌 Majburiy obuna loglari: <b>{st['total_subs']}</b>\n"
            f"• 👁 Reklama ko'rishlar: <b>{st['ad_views']}</b> | ⚡ Kesh: <b>{st['cached']}</b>\n"
            f"🕒 Vaqt: <code>{time.strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(text="🔄 Yangilash", callback_data="adm:stats"),
            types.InlineKeyboardButton(text="📥 Excel Hisobot", callback_data="export_xl:all")
        )
        markup.add(types.InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:back"))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif action == "channels":
        ADMIN_STATE.pop(uid, None)
        channels = get_channels_ram()
        type_names = {"public": "Ommaviy", "group": "Guruh", "private": "Maxfiy", "request": "Zayavkali"}
        lines = []
        for ch in channels:
            lim_txt = f"{ch['current_count']}/{ch['target_limit']}" if ch['target_limit'] > 0 else f"{ch['current_count']}/∞"
            client_link = f"https://t.me/{bot_username}?start=stat_{ch['stat_token']}"
            lines.append(
                f"• <b>{esc(ch['title'])}</b> [{type_names.get(ch['channel_type'], ch['channel_type'])}]\n"
                f"  👁 Ko'rgan: <b>{ch['shown_count']}</b> | 👥 Obuna: <b>{lim_txt}</b>\n"
                f"  Mijoz linki: <code>{esc(client_link)}</code>"
            )

        ch_text = "\n\n".join(lines) if lines else "Hozircha kanallar yo'q."
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(text="➕ Yangi kanal / guruh qo'shish", callback_data="adm:choose_ch_type"))
        for ch in channels:
            cid = ch['channel_id']
            markup.add(
                types.InlineKeyboardButton(text=f"📡 Statistika / Kim o'tdi: {ch['title']}", callback_data=f"adm_ch_logs:{cid}"),
                types.InlineKeyboardButton(text=f"🗑 O'chirish: {ch['title']} ({ch['current_count']}/{ch['target_limit'] if ch['target_limit']>0 else '∞'})", callback_data=f"adm_del_ch:{cid}")
            )
        markup.add(types.InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:back"))
        bot.edit_message_text(f"📢 <b>Majburiy Obuna Kanallari / Guruhlari:</b>\n\n{ch_text}", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif action == "choose_ch_type":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(text="📢 Ommaviy kanal (@username)", callback_data="adm_chtype:public"),
            types.InlineKeyboardButton(text="👥 Ommaviy / Maxfiy guruh", callback_data="adm_chtype:group"),
            types.InlineKeyboardButton(text="🔒 Maxfiy kanal (Oddiy link)", callback_data="adm_chtype:private"),
            types.InlineKeyboardButton(text="📩 Zayavkali maxfiy kanal (So'rov bilan)", callback_data="adm_chtype:request"),
            types.InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:channels")
        )
        bot.edit_message_text("📌 <b>Kanal yoki guruh turini tanlang:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif action == "ads":
        ADMIN_STATE.pop(uid, None)
        ads = get_all_ads_with_stats()
        now = time.time()
        lines = []
        for a in ads:
            status_icon = "🟢" if a["is_active"] == 1 else "🔴"
            exp_txt = f"{max(0, round((a['expires_at'] - now) / 3600, 1))}s" if a["expires_at"] > 0 else "∞"
            lim_txt = f"{a['current_views']}/{a['max_views']}" if a["max_views"] > 0 else f"{a['current_views']}/∞"
            fb_txt = " [Zaxira]" if a["is_fallback"] else ""
            client_link = f"https://t.me/{bot_username}?start=stat_{a['stat_token']}"
            lines.append(
                f"{status_icon} <b>#{a['id']}</b> [{a['ad_type']}/{a['media_type']}{fb_txt}] <b>{esc(a['text'][:45])}</b>\n"
                f"  👁 Ko'rish: <b>{lim_txt}</b> | ⏳ Vaqt: <b>{exp_txt}</b>\n"
                f"  Mijoz linki: <code>{esc(client_link)}</code>"
            )
        ad_list_txt = "\n\n".join(lines) if lines else "Hozircha reklamalar yo'q."

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(text="➕ Kaption osti reklama (Media tagida)", callback_data="adm_add_ad:caption:0"),
            types.InlineKeyboardButton(text="➕ Qidiruv/Shazam banner", callback_data="adm_add_ad:banner:0"),
            types.InlineKeyboardButton(text="➕ Tugmali reklama (URL tugma)", callback_data="adm_add_ad:button:0"),
            types.InlineKeyboardButton(text="➕ Popup Post Reklama (Rasm/Video/GIF/Matn + Tugma)", callback_data="adm_add_popup"),
            types.InlineKeyboardButton(text="🔄 Zaxira / Rotatsiya reklama", callback_data="adm_add_ad:caption:1"),
            types.InlineKeyboardButton(text="📥 Excel Reklama Statistikasi", callback_data="export_xl:all")
        )
        for a in ads:
            markup.add(types.InlineKeyboardButton(text=f"🗑 O'chirish #{a['id']} ({a['current_views']} ko'rish)", callback_data=f"adm_del_ad:{a['id']}"))
        markup.add(types.InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:back"))
        bot.edit_message_text(f"📣 <b>Reklamalar va Statistika:</b>\n\n{ad_list_txt}", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif action == "admins":
        ADMIN_STATE.pop(uid, None)
        admins = get_admins()
        adm_text = "\n".join([f"• <code>{a[0]}</code> — <b>{esc(a[1])}</b>" for a in admins])
        markup = types.InlineKeyboardMarkup(row_width=1)
        if uid == SUPER_ADMIN_ID:
            markup.add(types.InlineKeyboardButton(text="➕ Yangi admin qo'shish", callback_data="adm:add_admin"))
            for a in admins:
                if a[0] != SUPER_ADMIN_ID:
                    markup.add(types.InlineKeyboardButton(text=f"🗑 O'chirish: {a[0]}", callback_data=f"adm_del_adm:{a[0]}"))
        markup.add(types.InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:back"))
        bot.edit_message_text(f"👮‍♂️ <b>Adminlar:</b>\n\n{adm_text}", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif action == "add_admin" and uid == SUPER_ADMIN_ID:
        ADMIN_STATE[uid] = {"step": "wait_new_admin"}
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:admins"))
        bot.edit_message_text("➕ Yangi adminning <b>ID</b> raqamini yuboring yoki undan xabar forward qiling:", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif action == "broadcast":
        ADMIN_STATE[uid] = {"step": "wait_broadcast_post"}
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:back"))
        bot.edit_message_text(
            "📨 <b>Universal Rassilka (1-qadam):</b>\n\n"
            "Barcha faol foydalanuvchilar va guruhlarga yuboriladigan <b>istalgan postni</b> tashlang:\n"
            "<i>(Matn, Rasm, Video, GIF, Audio, Ovoz, Stiker, Dumaloq video, Fayl yoki Forward xabar)</i>",
            call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
        )

    elif action == "stop_broadcast":
        BROADCAST_STOP = True
        bot.answer_callback_query(call.id, "🛑 To'xtatilmoqda...")

    elif action == "ban_menu":
        ADMIN_STATE[uid] = {"step": "wait_ban_id"}
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:back"))
        bot.edit_message_text("👤 Foydalanuvchi <b>Telegram ID</b> sini yuboring:", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

    elif action == "clear_cache":
        with MEM_LOCK:
            MEM["media_cache"].clear()
            MEM["title_cache"].clear()
            MEM["sub_ttl"].clear()
            SYNC_QUEUE["media_cache"].clear()
        TOP_COUNTRY_CACHE.clear()
        QUERY_RESULT_CACHE.clear()
        INLINE_RESULTS_CACHE.clear()
        exec_immediate_pg("DELETE FROM media_cache")
        bot.answer_callback_query(call.id, "⚡ Kesh tozalandi!", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_ch_logs:") and is_admin(call.from_user.id))
def adm_ch_logs_cb(call: types.CallbackQuery):
    cid = call.data.split(":", 1)[1]
    report = get_channel_live_report(cid, limit_logs=15, include_users=True) or "❌ Loglar topilmadi."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text="🔄 Yangilash", callback_data=f"adm_ch_logs:{cid}"))
    markup.add(types.InlineKeyboardButton(text="📥 Excel Statistika", callback_data="export_xl:all"))
    markup.add(types.InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm:channels"))
    try:
        bot.edit_message_text(report, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_add_ad:") and is_admin(call.from_user.id))
def adm_add_ad_cb(call: types.CallbackQuery):
    _, ad_type, is_fb = call.data.split(":")
    ADMIN_STATE[call.from_user.id] = {"step": "wait_ad_text", "ad_type": ad_type, "is_fallback": int(is_fb)}
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:ads"))
    bot.edit_message_text("1️⃣ <b>Reklama matnini</b> yuboring:", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "adm_add_popup" and is_admin(call.from_user.id))
def adm_add_popup_cb(call: types.CallbackQuery):
    ADMIN_STATE[call.from_user.id] = {"step": "wait_popup_media", "ad_type": "popup", "is_fallback": 0}
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:ads"))
    bot.edit_message_text(
        "1️⃣ <b>Popup Post Reklama kontentini yuboring:</b>\n\n"
        "Rasm, Video, GIF, Audio, Ovoz, Fayl yoki oddiy Matn yuboring (tagida matni bilan):",
        call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_del_ad:") and is_admin(call.from_user.id))
def adm_del_ad_cb(call: types.CallbackQuery):
    delete_ad_db(int(call.data.split(":")[1]))
    bot.answer_callback_query(call.id, "🗑 Reklama o'chirildi!")
    call.data = "adm:ads"
    admin_callbacks(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_chtype:") and is_admin(call.from_user.id))
def adm_chtype_cb(call: types.CallbackQuery):
    ch_type = call.data.split(":")[1]
    ADMIN_STATE[call.from_user.id] = {"step": "wait_ch_id", "ch_type": ch_type}
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:channels"))
    bot.edit_message_text("1️⃣ Kanal/guruh <b>ID</b> si yoki <code>@username</code> ni yuboring (yoki kanaldan post forward qiling):", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_del_ch:") and is_admin(call.from_user.id))
def admin_del_ch_cb(call: types.CallbackQuery):
    remove_channel(call.data.split(":")[1])
    bot.answer_callback_query(call.id, "🗑 Kanal o'chirildi!")
    call.data = "adm:channels"
    admin_callbacks(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_del_adm:") and call.from_user.id == SUPER_ADMIN_ID)
def admin_del_adm_cb(call: types.CallbackQuery):
    remove_admin_db(int(call.data.split(":")[1]))
    bot.answer_callback_query(call.id, "🗑 Admin o'chirildi!")
    call.data = "adm:admins"
    admin_callbacks(call)

def execute_broadcast_worker(admin_chat_id: int, from_chat_id: int, message_id: int, reply_markup=None, pin_msg: bool = False):
    global BROADCAST_STOP
    BROADCAST_STOP = False
    targets = get_all_targets()
    sent, failed = 0, 0
    stop_kb = types.InlineKeyboardMarkup()
    stop_kb.add(types.InlineKeyboardButton(text="🛑 To'xtatish", callback_data="adm:stop_broadcast"))
    status_msg = bot.send_message(admin_chat_id, f"📨 Yuborilmoqda: <b>0 / {len(targets)}</b>", reply_markup=stop_kb, parse_mode="HTML")

    for i, target_uid in enumerate(targets):
        if BROADCAST_STOP:
            break
        try:
            copied = bot.copy_message(
                chat_id=target_uid,
                from_chat_id=from_chat_id,
                message_id=message_id,
                reply_markup=reply_markup
            )
            if pin_msg:
                try:
                    bot.pin_chat_message(target_uid, copied.message_id, disable_notification=True)
                except Exception:
                    pass
            sent += 1
            time.sleep(0.035)
        except Exception as err:
            failed += 1
            err_s = str(err).lower()
            if "blocked" in err_s or "deactivated" in err_s or "kicked" in err_s or "not found" in err_s:
                mark_user_left_status(target_uid, 1)

        if (i + 1) % 50 == 0:
            try:
                bot.edit_message_text(
                    f"📨 <b>{i+1} / {len(targets)}</b> | ✅ <b>{sent}</b> | ❌ <b>{failed}</b>",
                    admin_chat_id, status_msg.message_id, reply_markup=stop_kb, parse_mode="HTML"
                )
            except Exception:
                pass

    bot.edit_message_text(
        f"✅ <b>Rassilka yakunlandi!</b>\n\n📤 Yetib bordi: <b>{sent}</b>\n🚫 Bloklagan / Chiqib ketgan: <b>{failed}</b>",
        admin_chat_id, status_msg.message_id, parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("bc_run:") and is_admin(call.from_user.id))
def bc_run_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    st = ADMIN_STATE.pop(uid, None)
    if not st or "msg_id" not in st:
        bot.answer_callback_query(call.id, "❌ Ma'lumot eskirgan.", show_alert=True)
        return
    pin_flag = (call.data.split(":")[1] == "pin")
    bot.answer_callback_query(call.id, "🚀 Rassilka boshlandi!")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    threading.Thread(
        target=execute_broadcast_worker,
        args=(call.message.chat.id, st["from_chat"], st["msg_id"], st.get("markup"), pin_flag),
        daemon=True
    ).start()

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and ADMIN_STATE.get(m.from_user.id) is not None, content_types=["text", "photo", "video", "audio", "document", "voice", "animation", "sticker", "video_note"])
def handle_admin_wizard(message: types.Message):
    uid = message.from_user.id
    st = ADMIN_STATE.get(uid, {})
    step = st.get("step")
    bot_username = get_bot_username()

    if step == "wait_ch_id":
        ch_id, ch_title = None, "Kanal/Guruh"
        if message.forward_from_chat:
            ch_id = str(message.forward_from_chat.id)
            ch_title = message.forward_from_chat.title or "Kanal"
        elif message.text:
            raw = message.text.strip()
            try:
                info = bot.get_chat(raw)
                ch_id = str(info.id)
                ch_title = info.title or ch_id
            except Exception:
                ch_id = raw
        if not ch_id:
            bot.reply_to(message, "❌ ID topilmadi. Qayta yuboring:")
            return
        st.update({"ch_id": ch_id, "ch_title": ch_title, "step": "wait_ch_link"})
        bot.reply_to(message, f"✅ <b>{esc(ch_title)}</b> (<code>{esc(ch_id)}</code>)\n\n2️⃣ <b>Havola (Invite Link yoki @username)</b> ni yuboring:", parse_mode="HTML")

    elif step == "wait_ch_link" and message.text:
        link = normalize_tg_url(message.text.strip())
        if not link.startswith("http"):
            bot.reply_to(message, "❌ <code>https://t.me/...</code> yoki <code>@username</code> ko'rinishida yuboring:", parse_mode="HTML")
            return
        st.update({"ch_url": link, "step": "wait_ch_limit"})
        bot.reply_to(message, "3️⃣ <b>Obunachi limitini</b> kiriting (masalan <code>3000</code>, cheksiz uchun <code>0</code>):", parse_mode="HTML")

    elif step == "wait_ch_limit" and message.text:
        limit = int(message.text.strip()) if message.text.strip().isdigit() else 0
        token = add_channel(st["ch_id"], st["ch_title"], st["ch_type"], st["ch_url"], limit)
        ADMIN_STATE.pop(uid, None)
        client_link = f"https://t.me/{bot_username}?start=stat_{token}"
        bot.reply_to(
            message,
            f"✅ Kanal qo'shildi! Limit: <b>{limit if limit > 0 else 'Cheksiz'}</b>\n"
            f"🔗 Statistika havolasi: <code>{esc(client_link)}</code>",
            reply_markup=admin_main_keyboard(),
            parse_mode="HTML"
        )

    elif step == "wait_popup_media":
        mtype, fid, txt = "text", "", ""
        if message.photo:
            mtype, fid, txt = "photo", message.photo[-1].file_id, (message.caption or "-")
        elif message.video:
            mtype, fid, txt = "video", message.video.file_id, (message.caption or "-")
        elif message.animation:
            mtype, fid, txt = "animation", message.animation.file_id, (message.caption or "-")
        elif message.document:
            mtype, fid, txt = "document", message.document.file_id, (message.caption or "-")
        elif message.audio:
            mtype, fid, txt = "audio", message.audio.file_id, (message.caption or "-")
        elif message.voice:
            mtype, fid, txt = "voice", message.voice.file_id, (message.caption or "-")
        elif message.text:
            mtype, fid, txt = "text", "", message.text.strip()
        else:
            bot.reply_to(message, "❌ Qo'llab-quvvatlanmaydigan format.")
            return

        st.update({"media_type": mtype, "media_file_id": fid, "ad_text": txt, "step": "wait_popup_buttons"})
        bot.reply_to(
            message,
            "2️⃣ <b>Tugmalarni yuboring</b> (har bir qatorga <code>Tugma nomi - https://havola</code> ko'rinishida; tugmasiz bo'lsa <code>-</code> yuboring):",
            parse_mode="HTML"
        )

    elif step == "wait_popup_buttons" and message.text:
        st.update({"ad_url": message.text.strip(), "step": "wait_ad_views"})
        bot.reply_to(message, "3️⃣ <b>Ko'rishlar limiti</b> ni kiriting (masalan <code>5000</code>, cheksiz uchun <code>0</code>):", parse_mode="HTML")

    elif step == "wait_ad_text" and message.text:
        st.update({"ad_text": message.text.strip(), "step": "wait_ad_url"})
        bot.reply_to(message, "2️⃣ <b>Havola</b> yuboring (kanal, bot yoki guruh linki; havolasiz bo'lsa <code>-</code> yuboring):", parse_mode="HTML")

    elif step == "wait_ad_url" and message.text:
        url = "" if message.text.strip() == "-" else normalize_tg_url(message.text.strip())
        st.update({"ad_url": url, "step": "wait_ad_views"})
        bot.reply_to(message, "3️⃣ <b>Ko'rishlar limiti</b> ni kiriting (masalan <code>5000</code>, cheksiz uchun <code>0</code>):", parse_mode="HTML")

    elif step == "wait_ad_views" and message.text:
        max_views = int(message.text.strip()) if message.text.strip().isdigit() else 0
        st.update({"max_views": max_views, "step": "wait_ad_hours"})
        bot.reply_to(message, "4️⃣ <b>Amal qilish muddati (soatda)</b> ni kiriting (masalan <code>24</code> yoki <code>72</code>, cheksiz uchun <code>0</code>):", parse_mode="HTML")

    elif step == "wait_ad_hours" and message.text:
        try:
            hours = float(message.text.strip())
        except ValueError:
            hours = 0.0
        token = add_ad_db(
            st["ad_type"], st["ad_text"], st["ad_url"], st["max_views"], hours,
            st.get("is_fallback", 0), st.get("media_type", "text"), st.get("media_file_id", "")
        )
        ADMIN_STATE.pop(uid, None)
        client_link = f"https://t.me/{bot_username}?start=stat_{token}"
        bot.reply_to(
            message,
            f"✅ Reklama saqlandi!\n🔗 Statistika havolasi: <code>{esc(client_link)}</code>",
            reply_markup=admin_main_keyboard(),
            parse_mode="HTML"
        )

    elif step == "wait_new_admin":
        new_id, new_name = None, "Admin"
        if message.forward_from:
            new_id = message.forward_from.id
            new_name = message.forward_from.first_name or "Admin"
        elif message.text and message.text.strip().isdigit():
            new_id = int(message.text.strip())
        if new_id:
            add_admin_db(new_id, new_name)
            ADMIN_STATE.pop(uid, None)
            bot.reply_to(message, f"✅ <b>{esc(new_name)}</b> (<code>{new_id}</code>) admin qilindi!", parse_mode="HTML")
        else:
            bot.reply_to(message, "❌ Noto'g'ri ID.")

    elif step == "wait_ban_id" and message.text and message.text.strip().isdigit():
        target_id = int(message.text.strip())
        ADMIN_STATE.pop(uid, None)
        now = time.time()
        with MEM_LOCK:
            u = MEM["users"].get(target_id)
            new_status = 0 if (u and u["is_banned"] == 1) else 1
            if not u:
                u = {"chat_type": "private", "lang": "", "is_banned": new_status, "is_left": 0, "joined_ts": now, "last_active": now}
                MEM["users"][target_id] = u
            else:
                u["is_banned"] = new_status
            SYNC_QUEUE["users"][target_id] = (target_id, u["chat_type"], u["lang"], new_status, u["is_left"], u["joined_ts"], u["last_active"])
        bot.reply_to(message, f"👤 ID <code>{target_id}</code>: <b>{'🚫 Bloklandi' if new_status == 1 else '✅ Blokdan chiqarildi'}</b>", parse_mode="HTML")

    elif step == "wait_broadcast_post":
        st.update({"from_chat": message.chat.id, "msg_id": message.message_id, "step": "wait_broadcast_buttons"})
        bot.reply_to(
            message,
            "2️⃣ <b>Post ostiga URL tugmalar qo'shasizmi?</b>\n\n"
            "Har bir qatorga <code>Tugma nomi - https://havola</code> yozib yuboring.\n"
            "Agar tugma kerak bo'lmasa <code>-</code> yuboring:",
            parse_mode="HTML"
        )

    elif step == "wait_broadcast_buttons" and message.text:
        markup = parse_url_buttons_text(message.text)
        st["markup"] = markup
        st["step"] = "wait_broadcast_confirm"

        confirm_kb = types.InlineKeyboardMarkup(row_width=1)
        confirm_kb.add(
            types.InlineKeyboardButton(text="🚀 Oddiy yuborish (Hozir boshlash)", callback_data="bc_run:normal"),
            types.InlineKeyboardButton(text="📌 Yuborish + Chatda Pin (Qadab qo'yish)", callback_data="bc_run:pin"),
            types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:back")
        )
        bot.reply_to(
            message,
            f"3️⃣ <b>Tayyor!</b> Jami <b>{len(get_all_targets())}</b> ta faol auditoriyaga yuborish usulini tanlang:",
            reply_markup=confirm_kb,
            parse_mode="HTML"
        )

# ==========================================
# 11. KESH + OLDINDAN YUKLASH + ZAXIRA MANBA
# ==========================================
PREFETCH_INFLIGHT = {}
PREFETCH_SEM = threading.Semaphore(4)

def _prefetch_one(url: str, title: str = ""):
    key = f"aud:{url}"
    if get_cached_media(key, title):
        return
    with MEM_LOCK:
        if url in PREFETCH_INFLIGHT:
            return
        ev = threading.Event()
        PREFETCH_INFLIGHT[url] = ev
    aud_file = None
    try:
        with PREFETCH_SEM:
            base = f"downloads/pf_{int(time.time()*1000)}_{random.randint(100,999)}"
            aud_file, dl_title, dur = download_file_turbo(url, base, mode="audio")
            if aud_file and os.path.exists(aud_file):
                with open(aud_file, "rb") as a:
                    sent = bot.send_audio(
                        SUPER_ADMIN_ID, a,
                        title=title or dl_title,
                        duration=int(dur) if dur else None,
                        disable_notification=True
                    )
                if sent and sent.audio:
                    save_cached_media(key, sent.audio.file_id, "audio", title or dl_title)
                    try:
                        bot.delete_message(SUPER_ADMIN_ID, sent.message_id)
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        if aud_file and os.path.exists(aud_file):
            try:
                os.remove(aud_file)
            except Exception:
                pass
        with MEM_LOCK:
            PREFETCH_INFLIGHT.pop(url, None)
        ev.set()

def prefetch_entries(entries, n: int = 3):
    for it in entries[:n]:
        threading.Thread(target=_prefetch_one, args=(it["url"], it.get("title", "")), daemon=True).start()

def safe_animate_progress(chat_id: int, msg_id: int, label: str, stop_event: threading.Event):
    frames = [".", "..", "..."]
    idx = 0
    while not stop_event.wait(2.0):
        try:
            bot.edit_message_text(f"<b>{label}{frames[idx % len(frames)]}</b>", chat_id, msg_id, parse_mode="HTML")
        except Exception:
            break
        idx += 1

def download_file_turbo(url: str, base_path: str, mode: str = "audio"):
    t0 = time.time()
    with DOWNLOAD_SEMAPHORE:
        ydl_opts = get_ydl_opts(mode=mode, outtmpl=f"{base_path}.%(ext)s")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            print(f"⏱ {mode} yuklash: {time.time()-t0:.1f}s | {url}")
            if isinstance(info, dict) and "entries" in info and info["entries"]:
                info = info["entries"][0]
            files = sorted(
                glob.glob(f"{base_path}*"),
                key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0,
                reverse=True
            )
            valid_files = [f for f in files if not f.endswith((".part", ".ytdl", ".temp"))]
            chosen_file = valid_files[0] if valid_files else None
            return chosen_file, info.get('title', 'Media'), info.get('duration', 0)

def send_audio_by_url(
    chat_id: int,
    url: str,
    user_id: int = None,
    with_video_btn: bool = False,
    performer: str = None,
    custom_title: str = None,
    reply_to_id: int = None
):
    uid = user_id or chat_id
    cache_id = f"aud:{url}"
    cap_ad = get_caption_ad_line()
    final_caption = build_media_caption(prefix_line="", ad_line=cap_ad) or None

    def attach_buttons(song_title: str, target_url: str):
        markup = types.InlineKeyboardMarkup()
        fav_key = f"fav_{uid}_{int(time.time()*1000)}_{random.randint(10,99)}"
        SEARCH_CACHE[fav_key] = {"title": song_title, "url": target_url}
        row_btns = [types.InlineKeyboardButton(text=t(uid, "fav_add"), callback_data=f"fav_toggle:{fav_key}")]
        if with_video_btn:
            vid_key = f"vid_{chat_id}_{int(time.time()*1000)}_{random.randint(10,99)}"
            SEARCH_CACHE[vid_key] = target_url
            row_btns.append(types.InlineKeyboardButton(text=t(uid, "dl_video_btn"), callback_data=f"get_vid:{vid_key}"))
        markup.add(*row_btns)
        markup.add(types.InlineKeyboardButton(text=t(uid, "share_inline_btn"), switch_inline_query=song_title[:60]))
        ad_btn = get_ad_button()
        if ad_btn:
            markup.add(ad_btn)
        return markup

    ev = PREFETCH_INFLIGHT.get(url)
    if ev and not get_cached_media(cache_id, custom_title or ""):
        try:
            bot.send_chat_action(chat_id, "upload_voice")
        except Exception:
            pass
        ev.wait(timeout=45)
    cached = get_cached_media(cache_id, custom_title or "")
    if cached:
        file_id, _, title = cached
        final_title = custom_title or title
        try:
            bot.send_audio(
                chat_id,
                file_id,
                title=final_title,
                performer=performer,
                caption=final_caption,
                reply_markup=attach_buttons(final_title, url),
                reply_to_message_id=reply_to_id,
                parse_mode="HTML"
            )
            return
        except Exception:
            pass

    wait_msg = bot.send_message(chat_id, f"<b>{t(uid, 'loading')}.</b>", parse_mode="HTML")
    stop_ev = threading.Event()
    anim_t = threading.Thread(
        target=safe_animate_progress,
        args=(chat_id, wait_msg.message_id, t(uid, "loading"), stop_ev),
        daemon=True
    )
    anim_t.start()

    base_path = f"downloads/aud_{chat_id}_{int(time.time()*1000)}_{random.randint(10,99)}"
    aud_file = None
    active_url = url

    try:
        try:
            aud_file, title, duration = download_file_turbo(active_url, base_path, mode="audio")
        except Exception:
            aud_file, title, duration = None, custom_title or "Audio", 0

        if not aud_file or not os.path.exists(aud_file):
            if custom_title:
                active_url = f"scsearch1:{custom_title}"
                try:
                    aud_file, title, duration = download_file_turbo(active_url, base_path + "_fb", mode="audio")
                except Exception:
                    aud_file = None

        stop_ev.set()
        final_title = custom_title or title or "Audio"

        if aud_file and os.path.exists(aud_file):
            with open(aud_file, "rb") as aud:
                sent_msg = bot.send_audio(
                    chat_id,
                    aud,
                    title=final_title,
                    performer=performer,
                    duration=int(duration) if duration else None,
                    caption=final_caption,
                    reply_markup=attach_buttons(final_title, active_url),
                    reply_to_message_id=reply_to_id,
                    parse_mode="HTML",
                    timeout=90
                )
            if sent_msg and sent_msg.audio:
                save_cached_media(cache_id, sent_msg.audio.file_id, "audio", final_title)
                if active_url != url:
                    save_cached_media(f"aud:{active_url}", sent_msg.audio.file_id, "audio", final_title)
            try:
                os.remove(aud_file)
            except Exception:
                pass
            try:
                bot.delete_message(chat_id, wait_msg.message_id)
            except Exception:
                pass
        else:
            bot.edit_message_text(t(uid, "dl_audio_err"), chat_id, wait_msg.message_id, parse_mode="HTML")
    except Exception:
        stop_ev.set()
        try:
            bot.edit_message_text(t(uid, "dl_audio_err"), chat_id, wait_msg.message_id, parse_mode="HTML")
        except Exception:
            pass
        if aud_file and os.path.exists(aud_file):
            try:
                os.remove(aud_file)
            except Exception:
                pass

def send_video_by_url(chat_id: int, url: str, user_id: int = None, reply_to_id: int = None):
    uid = user_id or chat_id
    cache_id = f"vid:{url}"
    cap_ad = get_caption_ad_line()

    cache_key = f"{chat_id}_{int(time.time()*1000)}"
    SEARCH_CACHE[cache_key] = url
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text=t(uid, "dl_mp3_btn"), callback_data=f"extract_mp3:{cache_key}"))
    ad_btn = get_ad_button()
    if ad_btn:
        markup.add(ad_btn)

    cached = get_cached_media(cache_id)
    if cached:
        file_id, media_type, title = cached
        if media_type == "photo":
            cap = build_media_caption(f"🖼 <b>{esc(title)}</b>", cap_ad)
            bot.send_photo(chat_id, file_id, caption=cap, reply_to_message_id=reply_to_id, parse_mode="HTML")
        else:
            cap = build_media_caption(f"🎬 <b>{esc(title)}</b>", cap_ad)
            bot.send_video(chat_id, file_id, caption=cap, reply_to_message_id=reply_to_id, reply_markup=markup, parse_mode="HTML")
        return

    wait_msg = bot.send_message(chat_id, f"<b>{t(uid, 'loading')}.</b>", reply_to_message_id=reply_to_id, parse_mode="HTML")
    stop_ev = threading.Event()
    anim_t = threading.Thread(target=safe_animate_progress, args=(chat_id, wait_msg.message_id, t(uid, "loading"), stop_ev), daemon=True)
    anim_t.start()

    base_path = f"downloads/vid_{chat_id}_{int(time.time()*1000)}"
    downloaded_file = None
    try:
        downloaded_file, title, _ = download_file_turbo(url, base_path, mode="video")
        stop_ev.set()
        if downloaded_file and os.path.exists(downloaded_file):
            ext = os.path.splitext(downloaded_file)[1].lower()
            with open(downloaded_file, "rb") as f:
                if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    cap = build_media_caption(f"🖼 <b>{esc(title)}</b>", cap_ad)
                    sent_msg = bot.send_photo(chat_id, f, caption=cap, reply_to_message_id=reply_to_id, parse_mode="HTML", timeout=90)
                    save_cached_media(cache_id, sent_msg.photo[-1].file_id, "photo", title)
                else:
                    cap = build_media_caption(f"🎬 <b>{esc(title)}</b>", cap_ad)
                    sent_msg = bot.send_video(chat_id, f, caption=cap, reply_to_message_id=reply_to_id, reply_markup=markup, parse_mode="HTML", timeout=90)
                    save_cached_media(cache_id, sent_msg.video.file_id, "video", title)
            os.remove(downloaded_file)
            bot.delete_message(chat_id, wait_msg.message_id)
        else:
            bot.edit_message_text(t(uid, "dl_video_err"), chat_id, wait_msg.message_id, parse_mode="HTML")
    except Exception:
        stop_ev.set()
        bot.edit_message_text(t(uid, "dl_video_err"), chat_id, wait_msg.message_id, parse_mode="HTML")
        if downloaded_file and os.path.exists(downloaded_file):
            os.remove(downloaded_file)

@bot.message_handler(regexp=r'https?://[^\s]+')
def handle_url(message: types.Message):
    if not gate_user(message):
        return
    send_video_by_url(message.chat.id, message.text.strip(), user_id=message.from_user.id, reply_to_id=message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("extract_mp3:"))
def extract_mp3_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    if call.message.chat.type == "private":
        not_sub = check_subscription_fast(call.from_user)
        if not_sub:
            bot.answer_callback_query(call.id, t(uid, "sub_not_yet"), show_alert=True)
            return
    cache_key = call.data.split(":", 1)[1]
    url = SEARCH_CACHE.get(cache_key)
    if not url:
        bot.answer_callback_query(call.id, t(uid, "expired"), show_alert=True)
        return
    bot.answer_callback_query(call.id, "🎵")
    send_audio_by_url(call.message.chat.id, url, user_id=uid, with_video_btn=False, reply_to_id=None)

# ==========================================
# 12. SHAZAM, QIDIRUV, TOP (5 DAVLAT BAYROG'I) VA SEVIMLILAR
# ==========================================
async def recognize_audio(file_path: str):
    shazam = Shazam()
    return await shazam.recognize(file_path)

@bot.message_handler(content_types=["voice", "audio", "video", "video_note"])
def handle_shazam(message: types.Message):
    if not gate_user(message):
        return
    uid = message.from_user.id

    bot.send_chat_action(message.chat.id, "typing")
    file_id = (
        message.voice.file_id if message.voice else
        message.audio.file_id if message.audio else
        message.video.file_id if message.video else
        message.video_note.file_id
    )
    temp_file = f"downloads/shazam_{uid}_{message.message_id}.ogg"

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(temp_file, "wb") as f:
            f.write(downloaded)

        out = asyncio.run(recognize_audio(temp_file))
        track = out.get('track')

        if track:
            title = track.get('title', "Noma'lum")
            subtitle = track.get('subtitle', 'Ijrochi')
            cover_url = track.get('images', {}).get('coverarthq') or track.get('images', {}).get('coverart')
            full_track_name = f"{subtitle} - {title}"

            shazam_key = f"shz_{uid}_{message.message_id}"
            SEARCH_CACHE[shazam_key] = {'query': full_track_name, 'title': title, 'performer': subtitle}

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(text=t(uid, "shazam_dl_btn"), callback_data=f"dl_shazam:{shazam_key}"),
                types.InlineKeyboardButton(text=t(uid, "share_inline_btn"), switch_inline_query=full_track_name[:60])
            )
            ad_btn = get_ad_button()
            if ad_btn:
                markup.add(ad_btn)

            banner = get_banner_ad_line()
            head = t(uid, "shazam_found", artist=esc(subtitle), title=esc(title))
            caption = build_media_caption(head, banner)

            if cover_url:
                bot.send_photo(message.chat.id, cover_url, caption=caption, reply_to_message_id=message.message_id, reply_markup=markup, parse_mode="HTML")
            else:
                bot.send_message(message.chat.id, caption, reply_to_message_id=message.message_id, reply_markup=markup, parse_mode="HTML")
        else:
            bot.reply_to(message, t(uid, "shazam_not_found"), parse_mode="HTML")
    except Exception:
        bot.reply_to(message, t(uid, "shazam_not_found"), parse_mode="HTML")
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)

@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_shazam:"))
def dl_shazam_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    if call.message.chat.type == "private":
        not_sub = check_subscription_fast(call.from_user)
        if not_sub:
            bot.answer_callback_query(call.id, t(uid, "sub_not_yet"), show_alert=True)
            return
    shazam_key = call.data.split(":", 1)[1]
    data = SEARCH_CACHE.get(shazam_key)
    if not data:
        bot.answer_callback_query(call.id, t(uid, "expired"), show_alert=True)
        return
    bot.answer_callback_query(call.id, "🎵")
    send_audio_by_url(call.message.chat.id, f"ytsearch1:{data['query']}", user_id=uid, with_video_btn=True, performer=data['performer'], custom_title=data['title'], reply_to_id=None)

def _yt_flat_search(search_expr: str, filter_mixes: bool = False):
    ydl_opts = get_ydl_opts(mode="search")
    ydl_opts['extract_flat'] = True
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        res = ydl.extract_info(search_expr, download=False)
        out = []
        for e in (res or {}).get('entries', []) or []:
            if not isinstance(e, dict):
                continue
            vid_id = e.get('id')
            url = e.get('url') or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None)
            if url and not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"
            if not url:
                continue

            raw_title = e.get('title', "Noma'lum")
            dur = e.get('duration')

            if filter_mixes:
                low_t = raw_title.lower()
                if any(bad in low_t for bad in BANNED_MIX_WORDS):
                    continue
                if dur and int(dur) > 480:
                    continue

            out.append({
                'id': vid_id or url,
                'title': raw_title,
                'url': url,
                'duration': format_duration(dur)
            })
        return out

def search_music(query: str):
    clean_q = " ".join(query.strip().split())
    q_key = clean_q.lower()
    now = time.time()
    cached_q = QUERY_RESULT_CACHE.get(q_key)
    if cached_q and (now - cached_q["ts"] < QUERY_CACHE_TTL):
        return cached_q["entries"]

    combined = []
    seen = set()

    queries_to_run = [f"ytsearch15:{clean_q}"]
    if len(clean_q.split()) >= 3:
        queries_to_run.append(f"ytsearch8:{clean_q} lyrics song")

    for expr in queries_to_run:
        try:
            for item in _yt_flat_search(expr, filter_mixes=False):
                if item['id'] not in seen:
                    seen.add(item['id'])
                    combined.append(item)
        except Exception:
            continue

    final_entries = combined[:20]
    if final_entries:
        QUERY_RESULT_CACHE[q_key] = {"ts": now, "entries": final_entries}
    return final_entries

def build_search_page(user_id: int, page: int = 0):
    data = SEARCH_CACHE.get(f"srch_{user_id}")
    if not data:
        return None, None
    query = data['query']
    entries = data['entries']
    mode = data.get('mode', 'search')
    active_country = data.get('country', 'uz')

    total_pages = max(1, (len(entries) + 9) // 10)
    page = max(0, min(page, total_pages - 1))
    start = page * 10
    chunk = entries[start:start + 10]

    lines = [f"<b>{start + i + 1}. {esc(e['title'])}</b>{e['duration']}" for i, e in enumerate(chunk)]
    banner = get_banner_ad_line()
    header_prefix = "" if mode == "top" else "🔎 "
    text = f"{header_prefix}<b>{esc(query)}</b>\n\n" + "\n".join(lines) + banner

    markup = types.InlineKeyboardMarkup(row_width=5)

    if mode == "top":
        flag_btns = []
        for c in TOP_COUNTRIES:
            lbl = f"• {c['flag']} •" if c["code"] == active_country else c["flag"]
            flag_btns.append(types.InlineKeyboardButton(text=lbl, callback_data=f"top_cnt:{c['code']}"))
        markup.add(*flag_btns)

    btns = [types.InlineKeyboardButton(text=str(i + 1), callback_data=f"pick:{page}:{i}") for i in range(len(chunk))]
    markup.add(*btns)

    next_page = (page + 1) if (page + 1) < total_pages else 0
    prev_page = (page - 1) if page > 0 else (total_pages - 1)
    markup.add(
        types.InlineKeyboardButton(text="⬅️", callback_data=f"srch_page:{prev_page}"),
        types.InlineKeyboardButton(text="❌", callback_data="close_msg"),
        types.InlineKeyboardButton(text="➡️", callback_data=f"srch_page:{next_page}")
    )
    ad_btn = get_ad_button()
    if ad_btn:
        markup.add(ad_btn)
    return text, markup

@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle_text_search(message: types.Message):
    if message.chat.type != "private":
        return
    if not gate_user(message):
        return
    uid = message.from_user.id

    query = message.text.strip()
    if query.lower() not in QUERY_RESULT_CACHE:
        bot.send_chat_action(message.chat.id, "typing")
    try:
        entries = search_music(query)
        if not entries:
            bot.reply_to(message, t(uid, "search_not_found"), parse_mode="HTML")
            return

        SEARCH_CACHE[f"srch_{uid}"] = {'query': query, 'entries': entries, 'mode': 'search'}
        text, markup = build_search_page(uid, page=0)
        bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")
        prefetch_entries(entries, 3)
    except Exception:
        bot.reply_to(message, t(uid, "search_err"), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("srch_page:"))
def srch_page_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    if call.message.chat.type == "private":
        not_sub = check_subscription_fast(call.from_user)
        if not_sub:
            bot.answer_callback_query(call.id, t(uid, "sub_not_yet"), show_alert=True)
            return
    page = int(call.data.split(":")[1])
    text, markup = build_search_page(uid, page=page)
    if text:
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "close_msg")
def close_msg_cb(call: types.CallbackQuery):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("pick:"))
def pick_music_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    if call.message.chat.type == "private":
        not_sub = check_subscription_fast(call.from_user)
        if not_sub:
            bot.answer_callback_query(call.id, t(uid, "sub_not_yet"), show_alert=True)
            return

    parts = call.data.split(":")
    page, idx = int(parts[1]), int(parts[2])
    data = SEARCH_CACHE.get(f"srch_{uid}")
    if not data:
        bot.answer_callback_query(call.id, t(uid, "expired"), show_alert=True)
        return

    real_idx = page * 10 + idx
    entries = data['entries']
    if real_idx >= len(entries):
        return

    chosen = entries[real_idx]
    bot.answer_callback_query(call.id, "🎵")
    send_audio_by_url(call.message.chat.id, chosen['url'], user_id=uid, with_video_btn=True, custom_title=chosen['title'], reply_to_id=None)

@bot.callback_query_handler(func=lambda call: call.data.startswith("get_vid:"))
def get_vid_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    if call.message.chat.type == "private":
        not_sub = check_subscription_fast(call.from_user)
        if not_sub:
            bot.answer_callback_query(call.id, t(uid, "sub_not_yet"), show_alert=True)
            return
    vid_key = call.data.split(":", 1)[1]
    url = SEARCH_CACHE.get(vid_key)
    if not url:
        bot.answer_callback_query(call.id, t(uid, "expired"), show_alert=True)
        return
    bot.answer_callback_query(call.id, "🎬")
    send_video_by_url(call.message.chat.id, url, user_id=uid, reply_to_id=None)

# ==========================================
# 13. INLINE MODE
# ==========================================
@bot.inline_handler(func=lambda query: True)
def inline_music_search(inline_query: types.InlineQuery):
    uid = inline_query.from_user.id
    register_chat(uid, "private")
    if is_user_banned(uid):
        return

    q = (inline_query.query or "").strip()
    bot_username = get_bot_username()
    cap_ad = get_caption_ad_line()
    final_caption = build_media_caption(prefix_line="", ad_line=cap_ad) or f"📥 @{esc(bot_username)}"

    if not q:
        entries = get_country_top_songs("uz")[:10]
    else:
        entries = search_music(q)[:10]

    results = []
    for item in entries:
        url = item["url"]
        title = item["title"]
        dur = item.get("duration", "")
        cached = get_cached_media(f"aud:{url}", title)

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text=f"🔎 @{bot_username}", switch_inline_query=""))
        ad_btn = get_ad_button()
        if ad_btn:
            markup.add(ad_btn)

        if cached:
            file_id, _, _ = cached
            res_id = f"c_{secrets.token_hex(6)}"
            results.append(
                types.InlineQueryResultCachedAudio(
                    id=res_id,
                    audio_file_id=file_id,
                    caption=final_caption,
                    parse_mode="HTML",
                    reply_markup=markup
                )
            )
        else:
            token = f"inl_{secrets.token_hex(6)}"
            INLINE_RESULTS_CACHE[token] = {"url": url, "title": title}
            msg_content = types.InputTextMessageContent(
                message_text=f"🎵 <b>{esc(title)}</b>\n⏳ <i>Yuklanmoqda...</i>",
                parse_mode="HTML"
            )
            results.append(
                types.InlineQueryResultArticle(
                    id=token,
                    title=f"🎵 {title}",
                    description=f"Bosilganda audio yuklanadi{dur}",
                    input_message_content=msg_content,
                    reply_markup=markup
                )
            )

    try:
        bot.answer_inline_query(
            inline_query.id,
            results,
            cache_time=20,
            is_personal=True,
            switch_pm_text="🎙 Ovoz orqali qidirish (Shazam)",
            switch_pm_parameter="shazam"
        )
    except Exception:
        pass

@bot.chosen_inline_handler(func=lambda chosen: chosen.result_id.startswith("inl_"))
def chosen_inline_audio_loader(chosen: types.ChosenInlineResult):
    token = chosen.result_id
    inline_msg_id = chosen.inline_message_id
    data = INLINE_RESULTS_CACHE.get(token)
    if not data or not inline_msg_id:
        return

    url = data["url"]
    title = data["title"]
    bot_username = get_bot_username()
    cap_ad = get_caption_ad_line()
    final_caption = build_media_caption(prefix_line="", ad_line=cap_ad) or f"📥 @{esc(bot_username)}"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text=f"🔎 @{bot_username}", switch_inline_query=""))
    ad_btn = get_ad_button()
    if ad_btn:
        markup.add(ad_btn)

    cached = get_cached_media(f"aud:{url}", title)
    if cached:
        file_id, _, cached_title = cached
        final_title = title or cached_title
        try:
            media = types.InputMediaAudio(
                media=file_id,
                caption=final_caption,
                parse_mode="HTML",
                title=final_title
            )
            bot.edit_message_media(media=media, inline_message_id=inline_msg_id, reply_markup=markup)
            return
        except Exception:
            pass

    base_path = f"downloads/inl_{int(time.time()*1000)}_{random.randint(100,999)}"
    aud_file = None
    try:
        aud_file, dl_title, duration = download_file_turbo(url, base_path, mode="audio")
        final_title = title or dl_title
        if aud_file and os.path.exists(aud_file):
            with open(aud_file, "rb") as aud:
                sent_msg = bot.send_audio(
                    SUPER_ADMIN_ID,
                    aud,
                    title=final_title,
                    duration=int(duration) if duration else None,
                    disable_notification=True
                )
            file_id = sent_msg.audio.file_id
            save_cached_media(f"aud:{url}", file_id, "audio", final_title)
            try:
                bot.delete_message(SUPER_ADMIN_ID, sent_msg.message_id)
            except Exception:
                pass

            media = types.InputMediaAudio(
                media=file_id,
                caption=final_caption,
                parse_mode="HTML",
                title=final_title
            )
            bot.edit_message_media(media=media, inline_message_id=inline_msg_id, reply_markup=markup)
            os.remove(aud_file)
        else:
            bot.edit_message_text("❌ <b>Musiqani yuklab bo'lmadi.</b>", inline_message_id=inline_msg_id, parse_mode="HTML")
    except Exception:
        try:
            bot.edit_message_text("❌ <b>Musiqani yuklab bo'lmadi.</b>", inline_message_id=inline_msg_id, parse_mode="HTML")
        except Exception:
            pass
        if aud_file and os.path.exists(aud_file):
            os.remove(aud_file)

# ==========================================
# 14. RENDER HEALTH-CHECK SERVER VA ISHGA TUSHIRISH
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Tarona Bot Supabase RAM Active")

    def log_message(self, format, *args):
        return

def start_render_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    print(f"🌐 Render Health-Check Web Server {PORT}-portda ishga tushdi.")
    server.serve_forever()

def get_commands_for_lang(lang: str):
    if lang == "ru":
        return [
            types.BotCommand("start", "🚀 Запустить бота | Start"),
            types.BotCommand("top", "🔥 Популярные песни | ТОП 10"),
            types.BotCommand("fav", "❤️ Избранная музыка | Favorites"),
            types.BotCommand("lang", "🌐 Сменить язык | Til / Language")
        ]
    elif lang == "en":
        return [
            types.BotCommand("start", "🚀 Start the bot | Boshlash"),
            types.BotCommand("top", "🔥 Trending songs | TOP 10"),
            types.BotCommand("fav", "❤️ Favorite songs | Sevimlilar"),
            types.BotCommand("lang", "🌐 Change language | Til / Язык")
        ]
    return [
        types.BotCommand("start", "🚀 Botni ishga tushirish | Start"),
        types.BotCommand("top", "🔥 Eng mashhur qo'shiqlar | TOP 10"),
        types.BotCommand("fav", "❤️ Sevimli musiqalarim | Favorites"),
        types.BotCommand("lang", "🌐 Tilni o'zgartirish | Язык / Language")
    ]

def setup_user_commands(user_id: int, lang: str):
    try:
        bot.set_my_commands(get_commands_for_lang(lang), scope=types.BotCommandScopeChat(chat_id=user_id))
    except Exception:
        pass

def setup_bot_commands():
    try:
        bot.set_my_commands(get_commands_for_lang("uz"))
    except Exception:
        pass

if __name__ == "__main__":
    init_and_load_supabase()
    threading.Thread(target=start_render_web_server, daemon=True).start()
    threading.Thread(target=background_supabase_sync_worker, daemon=True).start()
    threading.Thread(target=background_top_warmer, daemon=True).start()
    print("🚀 Tarona Bot | Supabase + RAM + Render ishga tushdi...")
    while True:
        try:
            setup_bot_commands()
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=25,
                skip_pending=True,
                allowed_updates=telebot.util.update_types
            )
        except Exception as err:
            print(f"Qayta ulanilmoqda ({err})...")
            time.sleep(3)
