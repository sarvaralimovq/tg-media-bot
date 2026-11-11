"""
Личный Telegram-бот: скачивание видео/музыки (YouTube, Instagram, TikTok и др.),
кружочки из видео и голосовые из любого звука.
Работает только для одного владельца.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
import datetime as dtm
from zoneinfo import ZoneInfo
from pathlib import Path

import yt_dlp
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_API_URL = os.getenv("BOT_API_URL", "").strip()  # локальный Bot API сервер (файлы до 2 ГБ)
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent / "data"))
TMP_DIR = DATA_DIR / "tmp"
OWNER_FILE = DATA_DIR / "owner.json"
COOKIES = DATA_DIR / "cookies.txt"

# Лимиты Telegram: обычный API — 50 МБ отправка / 20 МБ получение; локальный — 2000 МБ
SEND_LIMIT = (2000 if BOT_API_URL else 50) * 1024 * 1024
GET_LIMIT = (2000 if BOT_API_URL else 20) * 1024 * 1024

DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

URL_RE = re.compile(r"https?://\S+")
PRESET = os.getenv("FFMPEG_PRESET", "veryfast")  # на слабом сервере — ultrafast
STD_HEIGHTS = [144, 240, 360, 480, 720, 1080, 1440, 2160]
JOB_TTL = 3 * 3600

jobs: dict[str, dict] = {}
sent_cache: dict[tuple, tuple[str, str]] = {}
work_sem = asyncio.Semaphore(2)


# ---------------------------------------------------------------- владелец
def load_owner() -> int | None:
    env = os.getenv("OWNER_ID", "").strip()
    if env.isdigit():
        return int(env)
    if OWNER_FILE.exists():
        try:
            return int(json.loads(OWNER_FILE.read_text())["id"])
        except Exception:
            pass
    return None


OWNER_ID = load_owner()


class OwnerOnly(BaseMiddleware):
    async def __call__(self, handler, event, data):
        global OWNER_ID
        user = data.get("event_from_user")
        if user is None:
            return
        if OWNER_ID is None:
            # Первый, кто напишет /start, становится владельцем навсегда
            if isinstance(event, Message) and (event.text or "").startswith("/start"):
                OWNER_ID = user.id
                OWNER_FILE.write_text(json.dumps({"id": user.id}))
                log.info("Владелец бота: %s (%s)", user.id, user.username)
            else:
                return
        if user.id != OWNER_ID:
            if isinstance(event, Message):
                await event.answer("⛔ Это личный бот.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔", show_alert=False)
            return
        if isinstance(event, Message) and event.text:
            asyncio.create_task(sb_safe("POST", "tg_messages", body=[
                {"owner_id": user.id, "kind": "text", "content": event.text[:4000]}]))
        return await handler(event, data)


# ---------------------------------------------------------------- утилиты
def mb(n) -> str:
    return f"{(n or 0) / 1024 / 1024:.1f}"


def new_job(**kw) -> str:
    jid = uuid.uuid4().hex[:10]
    kw["created"] = time.time()
    kw["dir"] = TMP_DIR / jid
    kw["dir"].mkdir(parents=True, exist_ok=True)
    jobs[jid] = kw
    return jid


async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for jid in [j for j, v in jobs.items() if now - v["created"] > JOB_TTL]:
            shutil.rmtree(jobs[jid]["dir"], ignore_errors=True)
            jobs.pop(jid, None)


async def run(*args: str) -> str:
    p = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await p.communicate()
    if p.returncode != 0:
        raise RuntimeError(err.decode(errors="ignore")[-600:])
    return out.decode(errors="ignore")


async def probe(path: Path) -> dict:
    """Ширина, высота, длительность, есть ли видео/звук."""
    try:
        out = await run(
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,width,height:format=duration", "-of", "json", str(path),
        )
        j = json.loads(out)
    except Exception:
        return {"w": 0, "h": 0, "dur": 0, "video": False, "audio": False}
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = any(s.get("codec_type") == "audio" for s in j.get("streams", []))
    return {
        "w": int(v.get("width") or 0),
        "h": int(v.get("height") or 0),
        "dur": float(j.get("format", {}).get("duration") or 0),
        "video": bool(v),
        "audio": a,
    }


async def safe_edit(msg: Message, text: str):
    try:
        await msg.edit_text(text)
    except Exception:
        pass


# ---------------------------------------------------------------- yt-dlp
def ydl_opts(outdir: Path, **extra) -> dict:
    o = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "playlist_items": "1",
        "outtmpl": str(outdir / "%(id).60s.%(ext)s"),
        "restrictfilenames": True,
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 5,
    }
    if COOKIES.exists():
        o["cookiefile"] = str(COOKIES)
    o.update(extra)
    return o


def first_entry(info: dict) -> dict:
    while info and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if not entries:
            break
        info = entries[0]
    return info


def get_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts(TMP_DIR, skip_download=True)) as y:
        return first_entry(y.extract_info(url, download=False))


def quality_options(info: dict) -> list[tuple[int, int]]:
    """[(высота, примерный размер в байтах)]"""
    fmts = info.get("formats") or []
    dur = info.get("duration") or 0

    def size(f):
        return f.get("filesize") or f.get("filesize_approx") or (
            (f.get("tbr") or 0) * dur * 125
        )

    audios = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    best_audio = max((size(f) for f in audios), default=0)
    by_h: dict[int, int] = {}
    for f in fmts:
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
        s = size(f) + (best_audio if f.get("acodec") == "none" else 0)
        by_h[h] = max(by_h.get(h, 0), int(s))
    hs = [h for h in by_h if h in STD_HEIGHTS] or sorted(by_h)[-4:]
    return [(h, by_h[h]) for h in sorted(hs)]


def make_hook(loop, status: Message, label: str):
    last = [0.0]

    def hook(d):
        if d.get("status") != "downloading":
            return
        now = time.time()
        if now - last[0] < 3:
            return
        last[0] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        pct = f"{done / total * 100:.0f}%" if total else ""
        text = f"⬇️ {label}… {pct} ({mb(done)}/{mb(total)} МБ)"
        asyncio.run_coroutine_threadsafe(safe_edit(status, text), loop)

    return hook


async def ytdl_download(url: str, outdir: Path, fmt: str, status: Message, label: str,
                        sort: list[str] | None = None, merge: str | None = None) -> Path:
    loop = asyncio.get_running_loop()
    extra = {"format": fmt, "progress_hooks": [make_hook(loop, status, label)]}
    if sort:
        extra["format_sort"] = sort
    if merge:
        extra["merge_output_format"] = merge

    def _do():
        with yt_dlp.YoutubeDL(ydl_opts(outdir, **extra)) as y:
            info = first_entry(y.extract_info(url, download=True))
            reqs = info.get("requested_downloads") or []
            if reqs and reqs[0].get("filepath"):
                return Path(reqs[0]["filepath"])
            return Path(y.prepare_filename(info))

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------- конвертация
async def to_video_note(src: Path, dst: Path, start: float = 0) -> Path:
    info = await probe(src)
    if not info["video"]:
        raise RuntimeError("В файле нет видео — кружок не сделать.")
    args = ["ffmpeg", "-y", "-ss", str(start), "-i", str(src), "-t", "60",
            "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=640:640,setsar=1,fps=30",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", PRESET, "-crf", "26", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(dst)]
    await run(*args)
    return dst


async def to_voice(src: Path, dst: Path) -> Path:
    info = await probe(src)
    if not info["audio"]:
        raise RuntimeError("В файле нет звука.")
    br = 64
    if info["dur"] and info["dur"] * br * 125 > SEND_LIMIT * 0.95:
        br = max(16, int(SEND_LIMIT * 0.95 / info["dur"] / 125))
    await run("ffmpeg", "-y", "-i", str(src), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "48000",
              "-c:a", "libopus", "-b:a", f"{br}k", "-application", "audio", str(dst))
    return dst


async def to_mp3(src: Path, dst: Path) -> Path:
    info = await probe(src)
    if not info["audio"]:
        raise RuntimeError("В файле нет звука.")
    br = 320 if info["dur"] and info["dur"] < 1200 else 192
    if info["dur"] and info["dur"] * br * 125 > SEND_LIMIT * 0.95:
        br = max(32, int(SEND_LIMIT * 0.95 / info["dur"] / 125))
    await run("ffmpeg", "-y", "-i", str(src), "-vn", "-map", "0:a:0",
              "-c:a", "libmp3lame", "-b:a", f"{br}k", str(dst))
    return dst


async def shrink_video(src: Path, dst: Path, dur: float) -> Path:
    """Пережать видео, чтобы влезло в лимит Telegram."""
    target_bits = SEND_LIMIT * 0.9 * 8
    v_kbps = max(150, int(target_bits / max(dur, 1) / 1000) - 128)
    await run("ffmpeg", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
              "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", PRESET,
              "-b:v", f"{v_kbps}k", "-maxrate", f"{v_kbps * 2}k", "-bufsize", f"{v_kbps * 2}k",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
              "-movflags", "+faststart", str(dst))
    return dst


# ---------------------------------------------------------------- клавиатуры
def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows]
    )


def url_keyboard(jid: str, qualities) -> InlineKeyboardMarkup:
    btns = []
    for h, s in qualities:
        warn = " ⚠️" if s > SEND_LIMIT else ""
        size = f" ~{mb(s)}МБ" if s else ""
        btns.append((f"🎬 {h}p{size}{warn}", f"v:{jid}:{h}"))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if not btns:
        rows.append([("🎬 Видео", f"v:{jid}:0")])
    rows.append([("🎵 MP3", f"a:{jid}"), ("🎤 Голосовое", f"vo:{jid}")])
    rows.append([("⭕ Кружок", f"c:{jid}")])
    return kb(rows)


def file_keyboard(jid: str, has_video: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_video:
        rows.append([("⭕ Кружок", f"c:{jid}")])
    rows.append([("🎤 Голосовое", f"vo:{jid}"), ("🎵 MP3", f"a:{jid}")])
    return kb(rows)


# ---------------------------------------------------------------- бот
session = AiohttpSession(api=TelegramAPIServer.from_base(BOT_API_URL, is_local=True)) if BOT_API_URL else None
bot = Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=None)) if BOT_TOKEN else None
dp = Dispatcher()
dp.message.outer_middleware(OwnerOnly())
dp.callback_query.outer_middleware(OwnerOnly())

HELP = (
    "👋 Привет! Это твой личный бот.\n\n"
    "🔗 Пришли ссылку (YouTube, Instagram, TikTok, и др.) — выберешь качество видео, "
    "MP3, голосовое или кружок.\n"
    "🎞 Пришли видео — сделаю ⭕ кружок, 🎤 голосовое или 🎵 MP3.\n"
    "🎵 Пришли MP3/любой звук — сделаю 🎤 голосовое.\n\n"
    "⏱ Кружок: до 60 сек. Чтобы начать не с начала — добавь в подпись к видео время, "
    "например <code>1:25</code>.\n"
    "🍪 Если YouTube/Instagram просит вход — пришли файл <code>cookies.txt</code>.\n"
    "🔄 /update — обновить загрузчик (если сайт перестал качаться)."
)


@dp.message(CommandStart())
@dp.message(Command("help"))
async def cmd_start(m: Message):
    await m.answer(HELP, parse_mode="HTML")


@dp.message(Command("update"))
async def cmd_update(m: Message):
    st = await m.answer("🔄 Обновляю yt-dlp…")
    try:
        out = await run(sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]")
        await safe_edit(st, "✅ Обновлено, перезапускаюсь…")
    except Exception as e:
        await safe_edit(st, f"❌ Не удалось: {e}")
        return
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def parse_time(text: str | None) -> float:
    if not text:
        return 0
    m = re.search(r"\b(?:(\d+):)?(\d{1,2}):(\d{2})\b", text)
    if m:
        h, mi, s = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        return h * 3600 + mi * 60 + s
    m = re.search(r"[?&]t=(\d+)", text)
    return float(m.group(1)) if m else 0


@dp.message(F.text.regexp(URL_RE))
async def on_link(m: Message):
    url = URL_RE.search(m.text).group(0)
    st = await m.answer("🔎 Смотрю ссылку…")
    try:
        info = await asyncio.to_thread(get_info, url)
    except Exception as e:
        await safe_edit(st, f"❌ Не получилось открыть ссылку.\n{str(e)[:400]}")
        return
    title = info.get("title") or "Без названия"
    dur = int(info.get("duration") or 0)
    jid = new_job(kind="url", url=url, title=title, uploader=info.get("uploader") or "",
                  start=parse_time(url))
    q = quality_options(info)
    dur_s = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
    text = f"🎬 {title}\n⏱ {dur_s}\n\nЧто скачать?"
    if any(s > SEND_LIMIT for _, s in q):
        text += f"\n⚠️ — больше {SEND_LIMIT // 1024 // 1024} МБ, будет пережато."
    try:
        await st.delete()
    except Exception:
        pass
    await m.answer(text, reply_markup=url_keyboard(jid, q))


MEDIA = F.video | F.audio | F.voice | F.video_note | F.animation | F.document


@dp.message(MEDIA)
async def on_media(m: Message):
    # cookies.txt
    if m.document and (m.document.file_name or "").lower().endswith("cookies.txt"):
        await bot.download(m.document, destination=COOKIES)
        await m.answer("🍪 cookies.txt сохранён. Теперь закрытые видео должны качаться.")
        return
    obj = m.video or m.audio or m.voice or m.video_note or m.animation or m.document
    mime = getattr(obj, "mime_type", "") or ""
    is_video = bool(m.video or m.video_note or m.animation or mime.startswith("video/"))
    is_audio = bool(m.audio or m.voice or mime.startswith("audio/"))
    if not (is_video or is_audio):
        await m.answer("🤔 Пришли видео, звук или ссылку.")
        return
    if (obj.file_size or 0) > GET_LIMIT:
        await m.answer(
            f"❌ Файл {mb(obj.file_size)} МБ. Telegram даёт ботам скачивать только до "
            f"{GET_LIMIT // 1024 // 1024} МБ (см. README — локальный Bot API снимает лимит до 2 ГБ)."
        )
        return
    name = getattr(obj, "file_name", None) or ("video.mp4" if is_video else "audio.ogg")
    jid = new_job(kind="file", file_id=obj.file_id, name=name,
                  title=Path(name).stem, start=parse_time(m.caption))
    await m.reply("Что сделать?", reply_markup=file_keyboard(jid, is_video))


async def get_file_src(job: dict) -> Path:
    if job.get("path") and Path(job["path"]).exists():
        return Path(job["path"])
    dst = job["dir"] / ("src_" + re.sub(r"[^\w.\-]", "_", job["name"]))
    await bot.download(job["file_id"], destination=dst)
    job["path"] = str(dst)
    return dst


@dp.callback_query(F.data.regexp(r"^(v|a|vo|c):"))
async def on_action(cb: CallbackQuery):
    parts = cb.data.split(":")
    action, jid = parts[0], parts[1]
    param = parts[2] if len(parts) > 2 else ""
    job = jobs.get(jid)
    if not job:
        await cb.answer("⌛ Устарело, пришли заново", show_alert=True)
        return
    await cb.answer()
    chat_id = cb.message.chat.id
    key = (job.get("url") or job.get("file_id"), action, param, job.get("start"))
    if key in sent_cache:
        method, fid = sent_cache[key]
        await getattr(bot, method)(chat_id, fid)
        return

    labels = {"v": f"Видео {param}p" if param != "0" else "Видео", "a": "MP3",
              "vo": "Голосовое", "c": "Кружок"}
    st = await bot.send_message(chat_id, f"⏳ {labels[action]}: в очереди…")
    async with work_sem:
        work = job["dir"] / uuid.uuid4().hex[:6]
        work.mkdir(parents=True, exist_ok=True)
        try:
            msg = await process(action, param, job, work, st)
            if msg:
                media = msg.video or msg.audio or msg.voice or msg.video_note or msg.document
                method = {"video": "send_video", "audio": "send_audio", "voice": "send_voice",
                          "video_note": "send_video_note", "document": "send_document"}
                for attr, meth in method.items():
                    if getattr(msg, attr, None):
                        sent_cache[key] = (meth, media.file_id)
                        break
            try:
                await st.delete()
            except Exception:
                pass
        except Exception as e:
            log.exception("job failed")
            await safe_edit(st, f"❌ Ошибка: {str(e)[:500]}")
        finally:
            shutil.rmtree(work, ignore_errors=True)


async def process(action: str, param: str, job: dict, work: Path, st: Message) -> Message | None:
    chat_id = st.chat.id
    caption = job["title"][:200]
    if job["kind"] == "url":
        caption += f"\n{job['url']}"
    big = dict(request_timeout=1800)

    # --- источник
    if job["kind"] == "url":
        url = job["url"]
        if action == "v":
            h = int(param or 0)
            sort = (["res:%d" % h] if h else []) + ["vcodec:h264", "acodec:aac", "ext:mp4:m4a"]
            src = await ytdl_download(url, work, "bv*+ba/b", st, f"Скачиваю {h}p" if h else "Скачиваю",
                                      sort=sort, merge="mp4")
        elif action == "c":
            src = await ytdl_download(url, work, "bv*+ba/b", st, "Скачиваю",
                                      sort=["res:720", "vcodec:h264", "ext:mp4:m4a"], merge="mp4")
        else:
            src = await ytdl_download(url, work, "ba/b", st, "Скачиваю звук")
    else:
        await safe_edit(st, "⬇️ Получаю файл…")
        src = await get_file_src(job)

    # --- результат
    if action == "v":
        info = await probe(src)
        if src.stat().st_size > SEND_LIMIT:
            await safe_edit(st, f"🗜 Файл {mb(src.stat().st_size)} МБ — пережимаю под лимит…")
            src = await shrink_video(src, work / "small.mp4", info["dur"])
            info = await probe(src)
        await safe_edit(st, "📤 Отправляю…")
        return await bot.send_video(chat_id, FSInputFile(src), caption=caption,
                                    width=info["w"] or None, height=info["h"] or None,
                                    duration=int(info["dur"]) or None,
                                    supports_streaming=True, **big)

    if action == "c":
        await safe_edit(st, "⭕ Делаю кружок…")
        out = await to_video_note(src, work / "note.mp4", job.get("start") or 0)
        info = await probe(out)
        msg = await bot.send_video_note(chat_id, FSInputFile(out), length=640,
                                        duration=int(info["dur"]) or None, **big)
        full = (await probe(src))["dur"]
        if full > 61:
            await bot.send_message(chat_id, "ℹ️ Кружок максимум 60 сек — взял кусок. "
                                            "Другой момент: пришли видео с подписью-временем, напр. 1:25")
        return msg

    if action == "vo":
        await safe_edit(st, "🎤 Делаю голосовое…")
        out = await to_voice(src, work / "voice.ogg")
        info = await probe(out)
        return await bot.send_voice(chat_id, FSInputFile(out), duration=int(info["dur"]) or None, **big)

    if action == "a":
        await safe_edit(st, "🎵 Делаю MP3…")
        name = re.sub(r'[\\/:*?"<>|]', "", job["title"])[:80] or "audio"
        out = await to_mp3(src, work / f"{name}.mp3")
        info = await probe(out)
        return await bot.send_audio(chat_id, FSInputFile(out, filename=f"{name}.mp3"),
                                    title=job["title"][:64], performer=(job.get("uploader") or None),
                                    duration=int(info["dur"]) or None, **big)


# ================================================================ память и напоминания
SB_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.getenv("SUPABASE_KEY", "").strip()
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "claude-haiku-5-5")
TZ_NAME = os.getenv("TZ", "UTC")
TZ = ZoneInfo(TZ_NAME)
REMIND_HOUR = int(os.getenv("REMIND_HOUR", "9"))


def utc_str(d: dtm.datetime) -> str:
    return d.astimezone(dtm.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def sb(method: str, table: str, params: dict | None = None, body=None):
    if not (SB_URL and SB_KEY):
        raise RuntimeError("не заданы SUPABASE_URL / SUPABASE_KEY")
    import aiohttp
    headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
               "Content-Type": "application/json", "Prefer": "return=representation"}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.request(method, f"{SB_URL}/rest/v1/{table}", params=params, json=body,
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
            txt = await r.text()
            if r.status >= 300:
                raise RuntimeError(f"Supabase {r.status}: {txt[:300]}")
            return json.loads(txt) if txt else []


async def sb_safe(*a, **kw):
    try:
        return await sb(*a, **kw)
    except Exception as e:
        log.warning("db: %s", e)
        return []


MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6,
          "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
DATE_RE = re.compile(
    r"(\d{1,2})\s+(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|"
    r"сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)(?:\s+(\d{4}))?", re.I)
NUM_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?!\d)")
REL_RE = re.compile(r"через\s+(\d+)\s*(?:дн|д\b)", re.I)


def month_num(word: str) -> int | None:
    w = word.lower()
    for stem, n in MONTHS.items():
        if w.startswith(stem):
            return n
    return None


def find_date(text: str, now: dtm.datetime):
    """Ищет дату в тексте. Возвращает (дата или None, текст без даты)."""
    today = now.date()
    m = DATE_RE.search(text)
    if m:
        mon = month_num(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        try:
            d = dtm.date(y, mon, int(m.group(1)))
        except (ValueError, TypeError):
            return None, text
        if not m.group(3) and d < today:
            d = dtm.date(y + 1, mon, int(m.group(1)))
        return d, (text[:m.start()] + text[m.end():]).strip()
    m = NUM_DATE_RE.search(text)
    if m:
        try:
            d = dtm.date(int(m.group(3)) if m.group(3) else today.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None, text
        if not m.group(3) and d < today:
            d = dtm.date(d.year + 1, d.month, d.day)
        return d, (text[:m.start()] + text[m.end():]).strip()
    low = text.lower()
    if "послезавтра" in low:
        return today + dtm.timedelta(days=2), re.sub("послезавтра", "", text, flags=re.I).strip()
    if "завтра" in low:
        return today + dtm.timedelta(days=1), re.sub("завтра", "", text, flags=re.I).strip()
    m = REL_RE.search(text)
    if m:
        return today + dtm.timedelta(days=int(m.group(1))), (text[:m.start()] + text[m.end():]).strip()
    return None, text


@dp.message(Command("memory"))
async def cmd_memory(m: Message):
    try:
        rows = await sb("GET", "tg_memories", params={
            "owner_id": f"eq.{m.from_user.id}", "order": "created_at.desc", "limit": "30", "select": "content"})
    except Exception as e:
        return await m.answer(f"❌ База недоступна: {e}")
    if not rows:
        return await m.answer("🧠 Пока ничего не запомнил. Напиши: «запомни что ...»")
    await m.answer("🧠 Что я помню:\n" + "\n".join(f"• {r['content']}" for r in rows))


@dp.message(Command("reminders"))
async def cmd_reminders(m: Message):
    try:
        rows = await sb("GET", "tg_reminders", params={
            "owner_id": f"eq.{m.from_user.id}", "sent": "eq.false", "order": "fire_at.asc",
            "limit": "30", "select": "text,event_date,fire_at"})
    except Exception as e:
        return await m.answer(f"❌ База недоступна: {e}")
    if not rows:
        return await m.answer("📅 Активных напоминаний нет.")
    lines = []
    for r in rows:
        fire = dtm.datetime.fromisoformat(r["fire_at"].replace("Z", "+00:00")).astimezone(TZ)
        lines.append(f"• {fire:%d.%m %H:%M} — {r['text']} (событие {r['event_date']})")
    await m.answer("📅 Ближайшие напоминания:\n" + "\n".join(lines))


@dp.message(Command("history"))
async def cmd_history(m: Message):
    try:
        rows = await sb("GET", "tg_messages", params={
            "owner_id": f"eq.{m.from_user.id}", "order": "created_at.desc", "limit": "15",
            "select": "content,created_at"})
    except Exception as e:
        return await m.answer(f"❌ База недоступна: {e}")
    if not rows:
        return await m.answer("Истории пока нет.")
    lines = []
    for r in reversed(rows):
        t = dtm.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(TZ)
        lines.append(f"{t:%d.%m %H:%M} — {(r['content'] or '')[:120]}")
    await m.answer("🗂 Последние сообщения:\n" + "\n".join(lines))


async def ask_ai(uid: int, text: str) -> str:
    import aiohttp
    mems = await sb("GET", "tg_memories", params={
        "owner_id": f"eq.{uid}", "order": "created_at.desc", "limit": "30", "select": "content"})
    facts = "\n".join("- " + r["content"] for r in mems) or "пока ничего"
    system = ("Ты личный ассистент владельца в Telegram. Отвечай по-русски, коротко и дружелюбно. "
              "Вот что ты помнишь о нём:\n" + facts)
    body = {"model": AI_MODEL, "max_tokens": 700, "system": system,
            "messages": [{"role": "user", "content": text}]}
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post("https://api.anthropic.com/v1/messages", headers=headers, json=body,
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            j = await r.json()
            if r.status >= 300:
                raise RuntimeError(str(j)[:300])
    return j["content"][0]["text"]


@dp.message(F.text)
async def on_text(m: Message):
    text = m.text.strip()
    low = text.lower()
    uid = m.from_user.id
    try:
        if low.startswith(("запомни", "запомнить")):
            content = re.sub(r"^запомни(ть)?\s*(,|что|:)?\s*", "", text, flags=re.I).strip()
            if not content:
                return await m.answer("Что запомнить? Например: «запомни что мой экзамен по математике»")
            await sb("POST", "tg_memories", body=[{"owner_id": uid, "content": content}])
            return await m.answer(f"🧠 Запомнил: {content}")

        if low.startswith(("напомни", "напоминание")):
            now = dtm.datetime.now(TZ)
            event_date, rest = find_date(text, now)
            if not event_date:
                return await m.answer(
                    "📅 Укажи дату. Примеры:\n«напомни 10 декабря сдать экзамен»\n"
                    "«напомни завтра позвонить маме»\n«напомни через 3 дня оплатить счёт»")
            what = re.sub(r"^напомни(ть)?\s*(мне)?\s*(что|про|о)?\s*", "", rest, flags=re.I).strip() or "дело"
            fires = []
            for k in (3, 2, 1):  # за 3, 2 и 1 день, в REMIND_HOUR:00
                f = dtm.datetime.combine(event_date - dtm.timedelta(days=k),
                                         dtm.time(REMIND_HOUR, 0), tzinfo=TZ)
                if f > now:
                    fires.append((k, f))
            if not fires:  # дата слишком близко — одно напоминание через минуту
                fires = [(0, now + dtm.timedelta(minutes=1))]
            rows = [{"owner_id": uid, "text": what, "event_date": event_date.isoformat(),
                     "fire_at": utc_str(f)} for _, f in fires]
            await sb("POST", "tg_reminders", body=rows)
            when = ", ".join(f"{f:%d.%m %H:%M}" for _, f in fires)
            return await m.answer(
                f"✅ Записал: «{what}» — событие {event_date:%d.%m.%Y}.\n"
                f"⏰ Напомню: {when} ({TZ_NAME})")

        if ANTHROPIC_KEY:
            return await m.answer(await ask_ai(uid, text))
        await m.answer(
            "Я понимаю:\n• «запомни что …» — сохраню в память\n"
            "• «напомни 10 декабря …» — напомню за 3, 2 и 1 день\n"
            "• /memory, /reminders, /history — посмотреть\n"
            "• ссылку, видео или звук — скачаю или сделаю кружок/голосовое\n\n"
            "Чтобы я отвечал как ИИ, добавь ANTHROPIC_API_KEY в настройки.")
    except Exception as e:
        log.exception("text handler")
        await m.answer(f"❌ Ошибка: {str(e)[:300]}")


async def reminder_loop():
    """Каждые 30 сек проверяет, что пора отправить."""
    while True:
        try:
            if OWNER_ID and SB_URL and SB_KEY:
                now = utc_str(dtm.datetime.now(dtm.timezone.utc))
                due = await sb("GET", "tg_reminders", params={
                    "sent": "eq.false", "fire_at": f"lte.{now}", "owner_id": f"eq.{OWNER_ID}",
                    "select": "id,text,event_date"})
                for r in due:
                    await bot.send_message(OWNER_ID, f"⏰ Напоминание: {r['text']}\n📅 Событие: {r['event_date']}")
                    await sb("PATCH", "tg_reminders", params={"id": f"eq.{r['id']}"}, body={"sent": True})
        except Exception as e:
            log.warning("reminder loop: %s", e)
        await asyncio.sleep(30)


@dp.message()
async def fallback(m: Message):
    await m.answer("Пришли ссылку, видео или звук. /help — подсказка.")


async def keep_alive(url: str):
    """Пингуем себя, чтобы бесплатный Render не засыпал."""
    import aiohttp
    await asyncio.sleep(60)
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                async with s.get(url + "/health", timeout=aiohttp.ClientTimeout(total=30)) as r:
                    await r.read()
            except Exception as e:
                log.warning("keep-alive: %s", e)
            await asyncio.sleep(600)


async def run_webhook(base_url: str):
    import hashlib
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    secret = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
    path = "/tg/" + hashlib.sha256(("p" + BOT_TOKEN).encode()).hexdigest()[:16]

    async def health(_):
        return web.Response(text="ok")

    async def on_startup(bot: Bot):
        await bot.set_webhook(base_url + path, secret_token=secret, drop_pending_updates=False,
                              allowed_updates=dp.resolve_used_update_types())
        log.info("Webhook установлен: %s/tg/…", base_url)

    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
    setup_application(app, dp, bot=bot)
    if os.getenv("KEEP_ALIVE", "1") == "1":
        asyncio.create_task(keep_alive(base_url))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("HTTP-сервер на порту %s", port)
    await asyncio.Event().wait()


async def main():
    if not BOT_TOKEN:
        print("❌ Нет BOT_TOKEN. Запусти setup_token.ps1 или впиши токен в файл .env")
        sys.exit(1)
    asyncio.create_task(cleanup_loop())
    asyncio.create_task(reminder_loop())
    me = await bot.get_me()
    log.info("Бот @%s запущен. Владелец: %s", me.username, OWNER_ID or "ещё не задан — напиши боту /start")
    base_url = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if base_url:
        await run_webhook(base_url)
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
