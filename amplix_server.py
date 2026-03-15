"""
Wavefy Server
═══════════════════════════════════════════════════════════════
Telethon  — поиск по публичным Telegram каналам (MTProto)
HTTPX     — стриминг файлов через Telegram CDN (Bot API)
FastAPI   — REST API + раздача фронтенда

Установка:
    pip install fastapi "uvicorn[standard]" telethon httpx

Первый запуск (нужен один раз):
    python wavefy_server.py
    # Telegram пришлёт код → введи в консоль

Последующие запуски:
    python wavefy_server.py   # сессия сохранена, код не нужен
═══════════════════════════════════════════════════════════════
"""

import asyncio, hashlib, json, logging, os

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient
from telethon.tl.functions.messages import SearchRequest
from telethon.tl.types import (
    DocumentAttributeAudio,
    InputMessagesFilterMusic,
    MessageMediaDocument,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wavefy")

# ───────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────
TG_API_ID   = int(os.getenv("TG_API_ID",   "0"))
TG_API_HASH =     os.getenv("TG_API_HASH", "")
TG_PHONE    =     os.getenv("TG_PHONE",    "")
TG_SESSION  =     os.getenv("TG_SESSION",  "wavefy_session")

BOT_TOKEN   =     os.getenv("BOT_TOKEN",   "")
BASE_URL    =     os.getenv("BASE_URL",    "http://localhost:8000")
PORT        = int(os.getenv("PORT",        "8000"))

TG_BOT_API  = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

MUSIC_CHANNELS = [
    "musicbasehd",
    "lossless_music_base",
    "muzon_base",
    "music_tg_base",
    "russianrap_music",
    "zvuk_bass",
    "audiofiles_music",
    "mp3_music_bot",
]

# ───────────────────────────────────────────────
# IN-MEMORY DATABASE
# ───────────────────────────────────────────────
_db: dict = {}

def get_user(uid: int) -> dict:
    if uid not in _db:
        _db[uid] = {"tracks": [], "playlists": [], "_seq": 1}
    return _db[uid]

def next_id(u: dict) -> int:
    v = u["_seq"]; u["_seq"] += 1; return v

def uid_of(req: Request) -> int:
    v = req.headers.get("x-telegram-user-id", "0")
    return int(v) if v.isdigit() else 0

# ───────────────────────────────────────────────
# HELPERS
# ───────────────────────────────────────────────
def fmt_dur(secs: int) -> str:
    if not secs: return ""
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"

def pick_emoji(title: str, artist: str) -> str:
    s = (title + " " + artist).lower()
    for kw, e in [
        ("рок","🎸"),("rock","🎸"),("metal","🤘"),
        ("hip","🎤"),("rap","🎤"),("рэп","🎤"),
        ("electro","⚡"),("dance","⚡"),("house","⚡"),("techno","⚡"),
        ("jazz","🎷"),("soul","✨"),("r&b","✨"),
        ("lofi","🌙"),("chill","🌊"),("ambient","🌊"),
        ("classic","🎻"),("классик","🎻"),
    ]:
        if kw in s: return e
    return "🎵"

# ───────────────────────────────────────────────
# TELETHON USERBOT (только поиск)
# ───────────────────────────────────────────────
_userbot: TelegramClient | None = None
_bot_id: int | None = None

async def init_userbot():
    global _userbot
    if not all([TG_API_ID, TG_API_HASH, TG_PHONE]):
        log.error("❌  TG_API_ID / TG_API_HASH / TG_PHONE не заданы")
        return
    _userbot = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)
    await _userbot.start(phone=TG_PHONE)
    me = await _userbot.get_me()
    log.info(f"✅  Userbot: @{me.username or me.first_name}")

def _parse_audio(doc) -> tuple[str, str, int]:
    title, artist, dur = "Unknown", "Unknown", 0
    for a in doc.attributes:
        if isinstance(a, DocumentAttributeAudio):
            dur    = a.duration  or dur
            title  = a.title     or title
            artist = a.performer or artist
    return title, artist, dur

async def search_channel(channel: str, query: str) -> list[dict]:
    if not _userbot:
        return []
    results = []
    try:
        entity = await _userbot.get_entity(channel)
        found  = await _userbot(SearchRequest(
            peer=entity, q=query,
            filter=InputMessagesFilterMusic(),
            min_date=None, max_date=None,
            offset_id=0, add_offset=0, limit=10,
            max_id=0, min_id=0, hash=0,
        ))
        words = [w for w in query.lower().split() if len(w) > 1]
        for msg in found.messages:
            if not isinstance(msg.media, MessageMediaDocument):
                continue
            doc = msg.media.document
            if not doc: continue
            title, artist, dur = _parse_audio(doc)
            if words and not any(w in f"{title} {artist}".lower() for w in words):
                continue
            results.append({
                "file_id":      str(doc.id),
                "bot_file_id":  "",
                "title":        title,
                "artist":       artist,
                "duration":     dur,
                "duration_str": fmt_dur(dur),
                "emoji":        pick_emoji(title, artist),
                "source":       channel,
                "channel":      channel,
                "msg_id":       msg.id,
                "size":         doc.size,
            })
    except Exception as e:
        log.warning(f"  search [{channel}]: {e}")
    return results

async def search_all(query: str, limit: int = 40) -> list[dict]:
    tasks = [search_channel(ch, query) for ch in MUSIC_CHANNELS]
    groups = await asyncio.gather(*tasks, return_exceptions=True)
    seen, out = set(), []
    for g in groups:
        if not isinstance(g, list): continue
        for r in g:
            key = f"{r['channel']}:{r['msg_id']}"
            if key not in seen:
                seen.add(key); out.append(r)
            if len(out) >= limit: return out
    return out

# ───────────────────────────────────────────────
# HTTPX STREAMING (Bot API → CDN)
# ───────────────────────────────────────────────
_fid_cache: dict[str, str] = {}   # "channel:msg_id" → bot file_id

async def get_bot_id() -> int | None:
    global _bot_id
    if _bot_id: return _bot_id
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{TG_BOT_API}/getMe", timeout=5)
        if r.json().get("ok"):
            _bot_id = r.json()["result"]["id"]
            return _bot_id
    return None

async def resolve_bot_fid(channel: str, msg_id: int) -> str:
    """
    Пересылаем сообщение из канала в чат бота через userbot,
    затем забираем file_id через Bot API getUpdates.
    """
    cache_key = f"{channel}:{msg_id}"
    if cache_key in _fid_cache:
        return _fid_cache[cache_key]

    if not (_userbot and BOT_TOKEN):
        return ""

    bot_id = await get_bot_id()
    if not bot_id:
        return ""

    try:
        entity = await _userbot.get_entity(channel)
        await _userbot.forward_messages(bot_id, msg_id, entity)
        await asyncio.sleep(1.5)  # даём боту время получить сообщение

        async with httpx.AsyncClient() as c:
            upd = await c.get(
                f"{TG_BOT_API}/getUpdates",
                params={"limit": 10, "offset": -10},
                timeout=8,
            )
            for u in reversed(upd.json().get("result", [])):
                audio = (u.get("message") or {}).get("audio")
                if audio:
                    fid = audio["file_id"]
                    _fid_cache[cache_key] = fid
                    return fid
    except Exception as e:
        log.warning(f"  resolve_bot_fid [{channel}:{msg_id}]: {e}")
    return ""

async def get_cdn_url(bot_fid: str) -> str:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{TG_BOT_API}/getFile",
                         json={"file_id": bot_fid}, timeout=10)
        d = r.json()
        if d.get("ok"):
            return f"{TG_FILE_API}/{d['result']['file_path']}"
    return ""

# ───────────────────────────────────────────────
# FASTAPI
# ───────────────────────────────────────────────
app = FastAPI(title="Wavefy")
app.add_middleware(CORSMiddleware,
                   allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/search")
async def api_search(q: str = "", limit: int = 40):
    if len(q.strip()) < 2:
        return {"results": []}
    return {"results": await search_all(q.strip(), limit)}

@app.get("/api/stream/{file_id:path}")
async def api_stream(file_id: str, req: Request):
    uid   = uid_of(req)
    u     = get_user(uid)
    track = next((t for t in u["tracks"] if t.get("file_id") == file_id), None)
    if not track:
        return JSONResponse({"error": "track not in library"}, status_code=404)

    # Разрешаем bot file_id (из кеша или через forward)
    bot_fid = track.get("bot_file_id") or ""
    if not bot_fid:
        bot_fid = await resolve_bot_fid(track["channel"], track["msg_id"])
        if bot_fid:
            track["bot_file_id"] = bot_fid

    if not bot_fid:
        return JSONResponse({"error": "cannot resolve file"}, status_code=502)

    cdn_url = await get_cdn_url(bot_fid)
    if not cdn_url:
        return JSONResponse({"error": "cannot get CDN URL"}, status_code=502)

    range_hdr = req.headers.get("range")
    hdrs = {"Range": range_hdr} if range_hdr else {}

    async def proxy():
        async with httpx.AsyncClient() as c:
            async with c.stream("GET", cdn_url, headers=hdrs, timeout=60) as r:
                async for chunk in r.aiter_bytes(65536):
                    yield chunk

    fname = cdn_url.split("?")[0].lower()
    ct = ("audio/flac" if ".flac" in fname else
          "audio/ogg"  if ".ogg"  in fname else
          "audio/mp4"  if ".m4a"  in fname else "audio/mpeg")

    return StreamingResponse(proxy(),
                             status_code=206 if range_hdr else 200,
                             media_type=ct,
                             headers={"Accept-Ranges": "bytes"})

# ── LIBRARY ──────────────────────────────────────
@app.get("/api/library")
async def api_library(req: Request):
    u = get_user(uid_of(req))
    return {"tracks": u["tracks"], "playlists": u["playlists"]}

@app.post("/api/library/tracks")
async def api_save_track(req: Request):
    uid = uid_of(req); u = get_user(uid)
    body = await req.json()
    fid  = body.get("file_id", "")
    ex   = next((t for t in u["tracks"] if t.get("file_id") == fid), None)
    if ex:
        return {"ok": True, "duplicate": True, "track": ex}
    track = {
        "id":           next_id(u),
        "file_id":      fid,
        "bot_file_id":  "",
        "title":        body.get("title",        "Unknown"),
        "artist":       body.get("artist",       "Unknown"),
        "duration":     body.get("duration",     0),
        "duration_str": body.get("duration_str", ""),
        "emoji":        body.get("emoji",        "🎵"),
        "source":       body.get("source",       ""),
        "channel":      body.get("channel",      ""),
        "msg_id":       body.get("msg_id",       0),
        "liked":        False,
    }
    u["tracks"].append(track)
    return {"ok": True, "track": track}

@app.delete("/api/library/tracks/{track_id}")
async def api_del_track(track_id: int, req: Request):
    u = get_user(uid_of(req))
    u["tracks"] = [t for t in u["tracks"] if t.get("id") != track_id]
    for pl in u["playlists"]:
        pl["track_ids"] = [i for i in pl.get("track_ids",[]) if i != track_id]
    return {"ok": True}

@app.patch("/api/library/tracks/{track_id}")
async def api_patch_track(track_id: int, req: Request):
    u = get_user(uid_of(req))
    t = next((x for x in u["tracks"] if x.get("id") == track_id), None)
    if not t:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await req.json()
    for k in ("liked","title","artist"):
        if k in body: t[k] = body[k]
    return {"ok": True, "track": t}

# ── PLAYLISTS ─────────────────────────────────────
@app.post("/api/library/playlists")
async def api_new_pl(req: Request):
    u = get_user(uid_of(req)); b = await req.json()
    name = b.get("name","").strip()
    if not name:
        return JSONResponse({"error":"name required"}, status_code=400)
    pl = {"id": next_id(u), "name": name,
          "emoji": b.get("emoji","🎵"), "track_ids": []}
    u["playlists"].append(pl)
    return {"ok": True, "playlist": pl}

@app.patch("/api/library/playlists/{pl_id}/tracks")
async def api_pl_tracks(pl_id: int, req: Request):
    u  = get_user(uid_of(req))
    pl = next((p for p in u["playlists"] if p["id"] == pl_id), None)
    if not pl:
        return JSONResponse({"error":"not found"}, status_code=404)
    b  = await req.json()
    tid, action = b.get("track_id"), b.get("action")
    if action == "add" and tid not in pl["track_ids"]:
        pl["track_ids"].append(tid)
    elif action == "remove":
        pl["track_ids"] = [i for i in pl["track_ids"] if i != tid]
    return {"ok": True, "playlist": pl}

@app.delete("/api/library/playlists/{pl_id}")
async def api_del_pl(pl_id: int, req: Request):
    u = get_user(uid_of(req))
    u["playlists"] = [p for p in u["playlists"] if p["id"] != pl_id]
    return {"ok": True}

# ── WEBHOOK ───────────────────────────────────────
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    msg  = data.get("message", {})
    if msg.get("text") == "/start":
        cid  = msg["chat"]["id"]
        name = msg.get("from",{}).get("first_name","друг")
        async with httpx.AsyncClient() as c:
            await c.post(f"{TG_BOT_API}/sendMessage", json={
                "chat_id": cid,
                "text": f"🎵 Привет, {name}!\n\nWavefy — музыкальный плеер с поиском по Telegram.",
                "reply_markup": json.dumps({"inline_keyboard": [[{
                    "text": "🎧 Открыть Wavefy",
                    "web_app": {"url": BASE_URL},
                }]]}),
            })
    return {"ok": True}

# ── STARTUP ───────────────────────────────────────
@app.on_event("startup")
async def startup():
    asyncio.create_task(_boot())

async def _boot():
    # Userbot
    try:
        await init_userbot()
    except Exception as e:
        log.error(f"Userbot: {e}")
    # Bot
    if BOT_TOKEN:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{TG_BOT_API}/getMe", timeout=5)
                if r.json().get("ok"):
                    log.info(f"✅  Bot: @{r.json()['result']['username']}")
                if BASE_URL and "localhost" not in BASE_URL:
                    await c.post(f"{TG_BOT_API}/setWebhook", json={
                        "url": f"{BASE_URL}/webhook",
                        "drop_pending_updates": True,
                    })
                    log.info(f"✅  Webhook → {BASE_URL}/webhook")
        except Exception as e:
            log.error(f"Bot: {e}")

app.mount("/", StaticFiles(directory="frontend", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("wavefy_server:app", host="0.0.0.0", port=PORT,
                reload=False, workers=1)
