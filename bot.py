import asyncio
import json
import logging
import logging.handlers
import os
import random
import re
import secrets
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from tracker import fetch_status

COOKIES_FILE = Path("/data/cookies.json")


def md(s: str) -> str:
    return escape_markdown(s, version=2)


def code(s: str) -> str:
    """Экранирование для содержимого внутри ``...`` (только ` и \\)."""
    return s.replace("\\", "\\\\").replace("`", "\\`")


def _track_url(url_or_tid: str) -> str:
    """Возвращает полную ссылку на трекинг по URL или по чистому track-id."""
    if url_or_tid.startswith("http"):
        return url_or_tid
    return f"https://tracking.ozon.ru/?track={url_or_tid}"


def link_tid(tid_or_url: str) -> str:
    """`[mdv2-track-id](url)` — кликабельный track id."""
    if tid_or_url.startswith("http"):
        url = tid_or_url
        tid = _track_id(tid_or_url)
    else:
        tid = tid_or_url
        url = _track_url(tid)
    return f"[{md(tid)}]({url})"

load_dotenv()

LOG_DIR = Path(os.environ.get("LOG_DIR", "/data/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[1;31m",  # bold red
    }
    RESET = "\033[0m"
    GREY = "\033[90m"
    BLUE = "\033[34m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        ts = self.formatTime(record, "%H:%M:%S")
        level = f"{color}{record.levelname:<7}{self.RESET}"
        name = f"{self.BLUE}{record.name}{self.RESET}"
        return f"{self.GREY}{ts}{self.RESET} {level} {name}: {record.getMessage()}"


_plain = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_root = logging.getLogger()
_root.setLevel(logging.DEBUG)
_stream = logging.StreamHandler()
_stream.setFormatter(_ColorFormatter())
_root.addHandler(_stream)
# Ротация: 5MB × 3 файла = ~15MB суммарно.
_file = logging.handlers.RotatingFileHandler(
    LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
)
_file.setFormatter(_plain)
_root.addHandler(_file)
# Подавляем шумные библиотеки.
for noisy in ("httpx", "httpcore", "telegram.ext.Application", "telegram.Bot",
              "telegram.ext.ExtBot", "telegram.ext.Updater", "telegram.request"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("ozon-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
TRACKS_FILE = Path(os.environ.get("TRACKS_FILE", "/data/tracks.json"))
POLL_MIN_SEC = int(os.environ.get("POLL_MIN_SEC", "1800"))   # 30 мин
POLL_MAX_SEC = int(os.environ.get("POLL_MAX_SEC", "3540"))   # 59 мин
CONCURRENCY = max(1, min(5, int(os.environ.get("CONCURRENCY", "5"))))

REFRESH_CB = "refresh"
ACK_CB = "ack"
REFRESH_COOLDOWN_SEC = 10

# Глобальная метка последнего успешного обновления (любой источник).
_last_success_at: float | None = None
# Метки авто-поллера.
_last_autopoll_at: float | None = None
_next_autopoll_at: float | None = None
# Глобальный лок ручного обновления — чтобы разные чаты не запускали fetch параллельно.
_global_refresh_lock = asyncio.Lock()
# Cooldown — per-chat.
_last_refresh: dict[int, float] = {}


def _fmt_ts(ts: float) -> str:
    import time
    return time.strftime("%H:%M %d.%m", time.localtime(ts))


def _track_id(url: str) -> str:
    if "track=" in url:
        return url.split("track=", 1)[1].split("&", 1)[0]
    return url


def _state_pin_path() -> Path:
    return STATE_FILE.parent / "pinned_message.json"


def load_pinned() -> dict:
    p = _state_pin_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_pinned(data: dict) -> None:
    p = _state_pin_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_tracks() -> dict[str, dict]:
    """Возвращает {uid: {"url": str, "state": str|None}}. Терпит старый формат {uid: str}."""
    if not TRACKS_FILE.exists():
        return {}
    try:
        raw = json.loads(TRACKS_FILE.read_text())
    except Exception:
        log.exception("failed to read tracks file")
        return {}
    out: dict[str, dict] = {}
    for uid, val in raw.items():
        if isinstance(val, str):
            out[uid] = {"url": val, "state": None, "last": None}
        elif isinstance(val, dict):
            out[uid] = {
                "url": val.get("url", ""),
                "state": val.get("state"),
                "last": val.get("last"),
            }
    return out


def _strip_for_storage(r: dict) -> dict:
    """Копия r без бинарных полей (png) — для сохранения в tracks.json."""
    return {k: v for k, v in r.items() if k != "png"}


def _results_from_cache() -> list[tuple[str, str, dict]]:
    """Соберём «результаты» для рендера пина из кэша tracks.json — без сетевых запросов."""
    tracks = load_tracks()
    out: list[tuple[str, str, dict]] = []
    for uid, e in tracks.items():
        url = e.get("url") or ""
        last = e.get("last")
        if last:
            r = dict(last)
        else:
            # Нового трека ещё не пуляли — покажем как «ожидаем данные».
            r = {"label": None, "desc": "", "date": None, "eta": None,
                 "stage": None, "total": 19, "error": "ожидание данных"}
        out.append((uid, url, r))
    return out


def save_tracks(tracks: dict[str, dict]) -> None:
    TRACKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRACKS_FILE.write_text(json.dumps(tracks, ensure_ascii=False, indent=2))


def _migrate_legacy_state() -> None:
    """Если есть старый state.json — переносим его в tracks[*].state и удаляем файл."""
    if not STATE_FILE.exists():
        return
    try:
        old = json.loads(STATE_FILE.read_text())
    except Exception:
        log.exception("failed to read legacy state.json")
        return
    if isinstance(old, dict) and old:
        tracks = load_tracks()
        merged = 0
        for uid, entry in tracks.items():
            tid = _track_id(entry.get("url", ""))
            if not entry.get("state") and tid in old:
                entry["state"] = old[tid]
                merged += 1
        save_tracks(tracks)
        log.info("merged legacy state.json into tracks.json (%d entries)", merged)
    try:
        STATE_FILE.unlink()
    except Exception:
        pass


def _new_uid(existing: set[str]) -> str:
    while True:
        uid = secrets.token_hex(3)
        if uid not in existing:
            return uid


def _seed_tracks_from_env() -> None:
    if TRACKS_FILE.exists():
        return
    env = os.environ.get("TRACK_URLS", "").strip()
    tracks: dict[str, dict] = {}
    if env:
        for u in env.split(","):
            u = u.strip()
            if not u:
                continue
            tracks[_new_uid(set(tracks))] = {"url": u, "state": None}
    save_tracks(tracks)
    log.info("seeded tracks.json from env: %d entries", len(tracks))


TRACK_URL_RE = re.compile(r"https?://tracking\.ozon\.ru/\?track=([\w\-]+)")
TRACK_ID_RE = re.compile(r"\b(\d{6,}-\d{2,}-\d+)\b")


_sema = asyncio.Semaphore(CONCURRENCY)


FETCH_HARD_TIMEOUT_SEC = int(os.environ.get("FETCH_HARD_TIMEOUT_SEC", "180"))


async def _one(uid: str, url: str) -> tuple[str, str, dict]:
    async with _sema:
        log.info("fetching status for %s", url)
        try:
            res = await asyncio.wait_for(fetch_status(url), timeout=FETCH_HARD_TIMEOUT_SEC)
            log.info("got status for %s: %s", _track_id(url),
                     {k: v for k, v in res.items() if k != "png"})
            return uid, url, res
        except asyncio.TimeoutError:
            log.error("hard timeout (%ds) for %s", FETCH_HARD_TIMEOUT_SEC, url)
            return uid, url, {"label": None, "date": None, "eta": None,
                              "error": f"timeout {FETCH_HARD_TIMEOUT_SEC}s"}
        except Exception as e:
            log.exception("fetch failed for %s", url)
            short = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            return uid, url, {"label": None, "date": None, "eta": None, "error": short}


async def _edit_all_pinned(app_bot, text: str, kb: InlineKeyboardMarkup, except_msg: tuple[int, int] | None = None) -> None:
    """Перерисовать закреплённое сообщение во всех чатах. except_msg=(chat_id, msg_id) пропускается."""
    for chat_id, msg_id in load_pinned().items():
        if except_msg and (int(chat_id), msg_id) == except_msg:
            continue
        try:
            await app_bot.edit_message_text(
                text, chat_id=int(chat_id), message_id=msg_id,
                reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            log.debug("could not edit pinned message in chat %s", chat_id)


async def check_all() -> list[tuple[str, str, dict]]:
    global _last_success_at
    tracks = load_tracks()
    items = [(uid, e["url"]) for uid, e in tracks.items() if e.get("url")]
    if not items:
        return []
    results = await asyncio.gather(*(_one(uid, url) for uid, url in items))
    if results and all(r.get("error") is None for _, _, r in results):
        import time
        _last_success_at = time.time()
    return list(results)


def _label_from_flat(flat: str | None) -> str:
    if not flat:
        return "—"
    return flat.split(" | ", 1)[0]


async def _detect_and_notify(app_bot, results: list[tuple[str, str, dict]]) -> None:
    """Сравнить с сохранённым state, обновить state и при изменениях прислать алёрт в CHAT_ID."""
    tracks = load_tracks()
    label_changes: list[tuple[str, str, str, str, str, bytes | None]] = []
    eta_changes: list[tuple[str, str, str]] = []  # tid, old_eta, new_eta
    for uid, url, r in results:
        tid = _track_id(url)
        if r.get("error"):
            continue
        flat = _flat(r)
        prev_last = tracks.get(uid, {}).get("last") or {}
        prev_label = prev_last.get("label")
        prev_eta = prev_last.get("eta")
        new_label = r.get("label") or "—"
        new_eta = r.get("eta")
        new_desc = r.get("desc") or ""
        when = r.get("dt_short") or r.get("date") or ""
        if prev_label and prev_label != new_label:
            log.info("label changed for %s: %r -> %r", tid, prev_label, new_label)
            label_changes.append((tid, prev_label, new_label, new_desc, when, r.get("png")))
        if prev_eta and prev_eta != new_eta:
            log.info("ETA changed for %s: %r -> %r", tid, prev_eta, new_eta)
            eta_changes.append((tid, prev_eta, new_eta or "—"))
        if uid in tracks:
            tracks[uid]["state"] = flat
            tracks[uid]["last"] = _strip_for_storage(r)
    save_tracks(tracks)
    for tid, old, new, desc, date, png in label_changes:
        msg = (
            f"Изменение статуса *{link_tid(tid)}*\n"
            f"`├ `~{md(old)}~\n"
            f"`└ {code(new)}`"
        )
        if desc:
            msg += f"\n`  └ `||{md(desc)}||"
        if date:
            msg += f"\n\nДата {md(date)}"
        sent = False
        if png:
            try:
                from io import BytesIO
                await app_bot.send_photo(
                    CHAT_ID, photo=BytesIO(png), caption=msg,
                    parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_ACK_KB,
                )
                sent = True
            except Exception:
                log.exception("failed to send notification with photo, fallback to text")
        if not sent:
            try:
                await app_bot.send_message(
                    CHAT_ID, msg, parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=_ACK_KB,
                )
            except Exception:
                log.exception("failed to send notification")
    for tid, old_eta, new_eta in eta_changes:
        msg = (
            f"Изменена дата доставки *{link_tid(tid)}*\n"
            f"`├ `~ETA {md(old_eta)}~\n"
            f"`└ ETA {md(new_eta)}`"
        )
        try:
            await app_bot.send_message(
                CHAT_ID, msg, parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_ACK_KB,
            )
        except Exception:
            log.exception("failed to send ETA notification")


def _kb(loading: bool = False) -> InlineKeyboardMarkup:
    if loading:
        label = "⏳ Обновление..."
    else:
        label = "🔄 Обновить"
        if _last_success_at:
            label += f" (last: {_fmt_ts(_last_success_at)})"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=REFRESH_CB)]])


def _render(results: list[tuple[str, str, dict]], loading: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    blocks = []
    for uid, url, r in results:
        tid = _track_id(url)
        del_cmd = f"||/del\\_{md(uid)}||"
        header = f"*Заказ {link_tid(url)}*:"
        if r.get("error"):
            block = header + "\n" + f"`└── `ошибка: {md(str(r['error']))} {del_cmd}"
        else:
            lines = [header]
            stage, total = r.get("stage"), r.get("total")
            if stage and total:
                lines.append(f"`├ `Этап {stage}/{total}")
            label = code(r.get("label") or "—")
            lines.append(f"`├─ {label}`")
            if r.get("desc"):
                lines.append(f"`│  └ `{md(r['desc'])}")
            eta_part = f"_ETA {md(r['eta'])}_" if r.get("eta") else "_ETA —_"
            lines.append(f"`└── `{eta_part} {del_cmd}")
            block = "\n".join(lines)
        blocks.append(block)

    text = "\n\n".join(blocks) if blocks else "Нет отслеживаемых треков\\. Пришлите ссылку tracking\\.ozon\\.ru, чтобы добавить\\."
    if _last_autopoll_at:
        text += f"\n\nПоследнее обновление: {md(_fmt_ts(_last_autopoll_at))}"
    if _next_autopoll_at:
        text += f"\nСледующее ожидается: {md(_fmt_ts(_next_autopoll_at))}"

    return text, _kb(loading=loading)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/start from chat_id=%s", update.effective_chat.id)
    await update.message.reply_text(
        "Загружаю статусы...\n"
        "Если куки протухли — пришлите cookies.json файлом или JSON-текстом."
    )
    results = await check_all()
    text, kb = _render(results)
    msg = await ctx.bot.send_message(
        update.effective_chat.id, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2
    )
    pinned = load_pinned()
    pinned[str(update.effective_chat.id)] = msg.message_id
    save_pinned(pinned)
    # Просто инициализируем state без алёрта (это первый запуск чата).
    tracks = load_tracks()
    for uid, _url, r in results:
        if uid in tracks and not r.get("error"):
            tracks[uid]["state"] = _flat(r)
            tracks[uid]["last"] = _strip_for_storage(r)
    save_tracks(tracks)


async def _set_loading_on_all_pinned(app_bot, except_msg: tuple[int, int] | None = None) -> None:
    """Меняет кнопку на «Обновление...» во всех закреплённых сообщениях."""
    loading_kb = _kb(loading=True)
    for chat_id, msg_id in load_pinned().items():
        if except_msg and (int(chat_id), msg_id) == except_msg:
            continue
        try:
            await app_bot.edit_message_reply_markup(
                chat_id=int(chat_id), message_id=msg_id, reply_markup=loading_kb
            )
        except Exception:
            log.debug("could not set loading on chat %s", chat_id)


async def on_cookies_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Принимает .json-файл или JSON-текст с куки и сохраняет в /data/cookies.json."""
    msg = update.message
    if not msg:
        return
    if update.effective_chat.id != CHAT_ID:
        log.info("ignoring cookies upload from foreign chat %s", update.effective_chat.id)
        return

    raw: str | None = None
    source = "?"
    try:
        if msg.document:
            source = f"file {msg.document.file_name}"
            tgfile = await msg.document.get_file()
            data = await tgfile.download_as_bytearray()
            raw = bytes(data).decode("utf-8", errors="replace")
        elif msg.text:
            source = "text"
            raw = msg.text
        else:
            return
    except Exception as e:
        await msg.reply_text(f"❌ Не смог получить файл: {e}")
        return

    try:
        parsed = json.loads(raw)
    except Exception as e:
        await msg.reply_text(f"❌ Это не валидный JSON: {e}")
        return

    if isinstance(parsed, list):
        count = len(parsed)
    elif isinstance(parsed, dict) and isinstance(parsed.get("cookies"), list):
        count = len(parsed["cookies"])
    else:
        await msg.reply_text(
            "❌ Ожидаю массив cookie-объектов или объект с ключом `cookies`."
        )
        return

    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(raw, encoding="utf-8")
    log.info("cookies.json updated from %s (%d cookies)", source, count)
    await _delete_silently(msg)
    await _send_ack(
        ctx, update.effective_chat.id,
        f"✅ cookies\\.json обновлён \\({count} куки\\)\\. Жми «Обновить»\\."
    )


async def _refresh_pinned(app_bot) -> None:
    """Перерисовать пин из кэша tracks.json — без сетевых запросов."""
    text, kb = _render(_results_from_cache())
    await _edit_all_pinned(app_bot, text, kb)


_ACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("Понятно", callback_data=ACK_CB)]])


async def _delete_silently(msg) -> None:
    try:
        await msg.delete()
    except Exception:
        log.debug("could not delete user message", exc_info=True)


async def _send_ack(ctx, chat_id: int, text_md: str) -> None:
    await ctx.bot.send_message(
        chat_id, text_md, reply_markup=_ACK_KB, parse_mode=ParseMode.MARKDOWN_V2
    )


async def on_ack(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
        await q.message.delete()
    except Exception:
        log.debug("could not delete ack message", exc_info=True)


async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or update.effective_chat.id != CHAT_ID:
        return
    m = re.match(r"^/del_([A-Za-z0-9]+)", msg.text or "")
    if not m:
        return
    uid = m.group(1)
    chat_id = update.effective_chat.id
    await _delete_silently(msg)
    tracks = load_tracks()
    if uid not in tracks:
        await _send_ack(ctx, chat_id, f"❌ Нет трека с id `{md(uid)}`")
        return
    entry = tracks.pop(uid)
    save_tracks(tracks)
    tid = _track_id(entry.get("url", ""))
    log.info("removed track %s (%s)", uid, tid)
    text = f"*🗑 \\-* Удалено:\n\\- {link_tid(tid)}"
    await _send_ack(ctx, chat_id, text)
    await _refresh_pinned(ctx.bot)


async def on_track_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or update.effective_chat.id != CHAT_ID:
        return
    text = msg.text or ""
    urls: list[str] = []
    for m in TRACK_URL_RE.finditer(text):
        urls.append(f"https://tracking.ozon.ru/?track={m.group(1)}")
    if not urls:
        for m in TRACK_ID_RE.finditer(text):
            urls.append(f"https://tracking.ozon.ru/?track={m.group(1)}")
    if not urls:
        return
    chat_id = update.effective_chat.id
    await _delete_silently(msg)
    tracks = load_tracks()
    existing_urls = {e["url"] for e in tracks.values()}
    added: list[tuple[str, str]] = []
    for u in urls:
        if u in existing_urls:
            continue
        uid = _new_uid(set(tracks))
        tracks[uid] = {"url": u, "state": None}
        existing_urls.add(u)
        added.append((uid, u))
    if not added:
        await _send_ack(ctx, chat_id, "ℹ️ Уже отслеживается")
        return
    save_tracks(tracks)
    log.info("added %d track(s), seeding state", len(added))
    # Сразу подтягиваем статус, чтобы автообновление не прислало ложный алёрт.
    fetch_results = await asyncio.gather(*(_one(uid, u) for uid, u in added))
    tracks = load_tracks()
    for uid, _u, r in fetch_results:
        if uid in tracks and not r.get("error"):
            tracks[uid]["state"] = _flat(r)
            tracks[uid]["last"] = _strip_for_storage(r)
    save_tracks(tracks)
    lines = [f"\\+ {link_tid(u)}" for _uid, u in added]
    out = "*📦 \\+* Добавлено:\n" + "\n".join(lines)
    await _send_ack(ctx, chat_id, out)
    await _refresh_pinned(ctx.bot)


async def on_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    import time
    q = update.callback_query
    chat = q.message.chat_id
    log.info("refresh pressed by chat_id=%s", chat)

    if _global_refresh_lock.locked():
        await q.answer("Ожидайте", show_alert=True)
        return

    if time.time() - _last_refresh.get(chat, 0) < REFRESH_COOLDOWN_SEC:
        await q.answer("Слишком часто! Подождите", show_alert=True)
        return

    async with _global_refresh_lock:
        await q.answer("Обновляю...")
        try:
            await q.edit_message_reply_markup(reply_markup=_kb(loading=True))
        except Exception:
            log.debug("could not swap to loading button", exc_info=True)
        # У других пользователей тоже подменим кнопку, чтобы они видели, что идёт обновление.
        await _set_loading_on_all_pinned(ctx.bot, except_msg=(chat, q.message.message_id))

        results = await check_all()
        text, kb = _render(results)
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            log.exception("failed to edit message, sending new one")
            await ctx.bot.send_message(
                chat, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2
            )

        await _edit_all_pinned(ctx.bot, text, kb, except_msg=(chat, q.message.message_id))
        await _detect_and_notify(ctx.bot, results)
        _last_refresh[chat] = time.time()


def _flat(r: dict) -> str:
    if r.get("error"):
        return f"ошибка: {r['error']}"
    parts = [r.get("label") or "—"]
    if r.get("date"):
        parts.append(r["date"])
    if r.get("eta"):
        parts.append(f"ETA {r['eta']}")
    return " | ".join(parts)


async def poll_loop(app: Application) -> None:
    global _last_autopoll_at, _next_autopoll_at
    import time
    log.info("poll loop started, %d urls", len(load_tracks()))
    first = True
    while True:
        if first:
            first = False
            log.info("initial auto-poll on startup")
        else:
            sleep_for = max(0, (_next_autopoll_at or time.time()) - time.time())
            log.info("sleeping %.0f sec until next auto-poll at %s", sleep_for, _fmt_ts(_next_autopoll_at) if _next_autopoll_at else "?")
            await asyncio.sleep(sleep_for)
            log.info("auto-poll running")
        results = await check_all()
        _last_autopoll_at = time.time()
        # Сразу планируем следующий — чтобы метка показывалась после первого пула.
        _next_autopoll_at = _last_autopoll_at + random.randint(POLL_MIN_SEC, POLL_MAX_SEC)
        await _detect_and_notify(app.bot, results)
        text, kb = _render(results)
        await _edit_all_pinned(app.bot, text, kb)


HEARTBEAT_FILE = Path("/data/heartbeat")
HEARTBEAT_INTERVAL_SEC = 30


async def heartbeat_loop() -> None:
    while True:
        try:
            HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_FILE.touch()
        except Exception:
            log.exception("heartbeat write failed")
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)


async def on_startup(app: Application) -> None:
    asyncio.create_task(poll_loop(app))
    asyncio.create_task(heartbeat_loop())


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    err = ctx.error
    # Типичный шум — нажатие на старую/протухшую callback-кнопку.
    msg = str(err) if err else ""
    if "Query is too old" in msg or "query id is invalid" in msg.lower():
        log.debug("stale callback ignored: %s", msg)
        return
    if "Message is not modified" in msg:
        log.debug("edit no-op ignored")
        return
    log.error("unhandled exception in handler: %s", err, exc_info=err)


def main() -> None:
    _seed_tracks_from_env()
    _migrate_legacy_state()
    tracks = load_tracks()
    log.info("starting bot, tracking %d urls", len(tracks))
    for uid, entry in tracks.items():
        log.info("  • %s → %s", uid, entry.get("url"))
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Regex(r"^/del_[A-Za-z0-9]+"), cmd_del))
    app.add_handler(CallbackQueryHandler(on_refresh, pattern=f"^{REFRESH_CB}$"))
    app.add_handler(CallbackQueryHandler(on_ack, pattern=f"^{ACK_CB}$"))
    # Загрузка cookies.json: .json-файл или текст, начинающийся с [ или {.
    app.add_handler(MessageHandler(filters.Document.ALL, on_cookies_upload))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"^\s*[\[\{]"), on_cookies_upload)
    )
    # Приём ссылок tracking.ozon.ru или голых track-id.
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"tracking\.ozon\.ru|\d{6,}-\d{2,}-\d+"),
        on_track_add,
    ))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
