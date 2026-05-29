import asyncio
import json
import logging
import os
import re
from pathlib import Path

DEBUG_DUMP = os.environ.get("DEBUG_DUMP", "0").lower() in ("1", "true", "yes", "on")

from playwright.async_api import async_playwright

log = logging.getLogger("ozon-bot.tracker")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

API_URL_MARKER = "/p-api/ozon-track-bff/tracking/"

# Полная упорядоченная цепочка этапов (как на сайте Ozon, 18 шагов):
# (label, codes_aliases, description_under_spoiler).
# Пустой кортеж codes — этап есть на сайте, но соответствующий API event-код пока неизвестен.
STAGES: list[tuple[str, tuple[str, ...], str]] = [
    ("Создан", ("Created",), "Мы получили заказ, продавец уже собирает его"),
    ("Передается в доставку", ("TransferringToDelivery",),
     "Продавец собрал заказ и передаёт его в доставку. Обычно это занимает до 10 дней"),
    ("Заказ принят перевозчиком", ("WayToCity",),
     "Он отвезёт заказ на таможню. Товары пройдут таможенное оформление в стране отправления и в стране назначения."),
    ("Заказ везут на таможню в стране отправления", ("ParcelDepartureFromCarrier",),
     "Обычно это занимает до 10 дней"),
    ("Заказ привезли на таможню для экспортного таможенного оформления", ("ArrivedToOutwardExchangeOffice",),
     "Скорость оформления зависит от загруженности таможни"),
    ("Заказ покинул зону экспортного таможенного оформления", ("OutFromOutwardExchangeOffice",), ""),
    ("Заказ спешит в страну назначения", (), ""),
    ("Заказ на пути к границе страны назначения", ("OnTheWayToImportCustomsClearancePhantomStatus",),
     "Путь может занять от 1 дня до недели"),
    ("Заказ привезли в страну назначения", (), "Его отвезут на таможенное оформление"),
    ("Заказ передан на импортное таможенное оформление", ("ArrivedToInwardExchangeOffice",),
     "Его готовят к оформлению"),
    ("Заказ проходит импортное таможенное оформление", ("InwardCustomsProcessing",), ""),
    ("Заказ выпущен импортной таможней", ("OutFromInwardExchangeOffice",),
     "Его готовят к отправке на сортировочный терминал. Обычно это занимает от 8 до 12 дней"),
    ("Заказ отправили на сортировочный терминал", (),
     "Его подготовят к доставке в город получателя"),
    ("Заказ покинул сортировочный терминал", ("DepartedFromSortingTerminal",),
     "Его подготовили к доставке в город получателя"),
    ("Заказ ожидает отправки в город получателя", ("WaitingForDispatchToCity",),
     "Скорость отправки зависит от загруженности склада"),
    ("Заказ везут в город получателя", ("ArrivedToCity", "ArrivedToDeliveryCity"),
     "Его доставят в сортировочный центр"),
    ("Заказ везут", (), "Мы сообщим, когда его доставят"),
    ("Заказ в пункте выдачи", ("ArrivedToPickupPoint", "ReadyForPickup"),
     "Успейте забрать его в течение 14 дней."),
    ("Заказ получен в пункте выдачи", ("Delivered", "Received"), ""),
]
TOTAL_STAGES = len(STAGES)

# Быстрый lookup: event_code → (index_1_based, label, description).
_EVENT_INDEX: dict[str, tuple[int, str, str]] = {}
for _i, (_label, _codes, _desc) in enumerate(STAGES, start=1):
    for _c in _codes:
        _EVENT_INDEX[_c] = (_i, _label, _desc)

# Терминальные/отрицательные события вне основной цепочки.
TERMINAL_LABELS = {
    "Cancelled": "Отменён",
    "Returned": "Возвращён",
    "ReturnedToSeller": "Возвращён продавцу",
}

OFFLINE_PATTERNS = (
    re.compile(r"похоже,?\s*нет\s*соединения", re.IGNORECASE),
    re.compile(r"fab_ichlg", re.IGNORECASE),
    re.compile(r"antibot challenge", re.IGNORECASE),
)

DEBUG_DIR = Path("/data/debug")
COOKIES_FILE = Path("/data/cookies.json")


def _load_cookies() -> list[dict]:
    if not COOKIES_FILE.exists():
        log.warning("[tracker] cookies file not found: %s", COOKIES_FILE)
        return []
    try:
        raw = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("[tracker] failed to parse cookies file")
        return []

    items = raw["cookies"] if isinstance(raw, dict) and "cookies" in raw else raw if isinstance(raw, list) else []
    same_site_map = {
        "no_restriction": "None",
        "unspecified": "Lax",
        "lax": "Lax",
        "strict": "Strict",
        "none": "None",
    }
    result = []
    for c in items:
        name, value, domain = c.get("name"), c.get("value"), c.get("domain")
        if not name or value is None or not domain:
            continue
        out = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        ss = c.get("sameSite") or "Lax"
        if isinstance(ss, str):
            out["sameSite"] = same_site_map.get(ss.lower(), "Lax")
        exp = c.get("expires") or c.get("expirationDate")
        if exp is not None:
            try:
                out["expires"] = int(exp)
            except (TypeError, ValueError):
                pass
        result.append(out)
    log.info("[tracker] loaded %d cookies", len(result))
    return result


FETCH_TIMEOUT_SEC = int(os.environ.get("FETCH_TIMEOUT_SEC", "150"))
BODY_READ_TIMEOUT_MS = 10_000


async def _safe_close(obj, label: str) -> None:
    if obj is None:
        return
    try:
        await obj.close()
    except Exception:
        log.exception("[tracker] %s close failed", label)


async def fetch_status(url: str, timeout_ms: int = 60_000) -> dict:
    """Возвращает dict: {label, date, eta, error}. error=None при успехе.

    Жёсткие лимиты: общий таймаут FETCH_TIMEOUT_SEC, навигация/API — timeout_ms,
    чтение body — BODY_READ_TIMEOUT_MS. Браузер и контекст закрываются всегда.
    """
    try:
        return await asyncio.wait_for(
            _fetch_status_inner(url, timeout_ms), timeout=FETCH_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        log.error("[tracker] hard timeout %ds for %s", FETCH_TIMEOUT_SEC, url)
        return {"label": None, "desc": "", "date": None, "eta": None,
                "stage": None, "total": TOTAL_STAGES,
                "error": f"timeout {FETCH_TIMEOUT_SEC}s"}


async def _fetch_status_inner(url: str, timeout_ms: int) -> dict:
    cookies = _load_cookies()
    if not cookies:
        log.warning("[tracker] no cookies — skipping browser launch for %s", url)
        return {"label": None, "desc": "", "date": None, "eta": None,
                "stage": None, "total": TOTAL_STAGES,
                "error": "нет cookies — пришлите cookies.json в чат"}
    log.info("[tracker] launching browser for %s", url)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=ru-RU",
                "--window-size=1366,768",
            ],
        )
        context = None
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            )
            context.set_default_timeout(timeout_ms)
            context.set_default_navigation_timeout(timeout_ms)
            if cookies:
                try:
                    await context.add_cookies(cookies)
                    log.info("[tracker] cookies applied")
                except Exception:
                    log.exception("[tracker] failed to apply cookies")
            page = await context.new_page()
            log.info("[tracker] goto %s", url)
            try:
                async with page.expect_response(
                    lambda r: API_URL_MARKER in r.url, timeout=timeout_ms
                ) as resp_info:
                    await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                resp = await resp_info.value
                log.info("[tracker] API response %s %s", resp.status, resp.url)
                if resp.status != 200:
                    body_text = await _safe_body(page)
                    _dump_debug(url, await _safe_content(page), body_text)
                    return {"label": None, "desc": "", "date": None, "eta": None,
                            "stage": None, "total": TOTAL_STAGES,
                            "error": f"HTTP {resp.status}"}
                data = await resp.json()
                result = _status_from_api(data)
                log.info("[tracker] parsed: %s", result)
                return result
            except Exception as e:
                log.warning("[tracker] API wait failed: %s — fallback to body parsing", e)
                await asyncio.sleep(3)
                body_text = await _safe_body(page)
                log.info("[tracker] body preview: %s", body_text[:300].replace("\n", " | "))
                if _is_antibot(body_text):
                    _dump_debug(url, await _safe_content(page), body_text)
                    return {"label": None, "desc": "", "date": None, "eta": None,
                            "stage": None, "total": TOTAL_STAGES,
                            "error": "antibot (обнови cookies.json)"}
                _dump_debug(url, await _safe_content(page), body_text)
                return {"label": None, "desc": "", "date": None, "eta": None,
                        "stage": None, "total": TOTAL_STAGES,
                        "error": "статус не распознан"}
        finally:
            await _safe_close(context, "context")
            await _safe_close(browser, "browser")
            log.info("[tracker] browser closed")


async def _safe_body(page) -> str:
    try:
        return await page.inner_text("body", timeout=BODY_READ_TIMEOUT_MS)
    except Exception:
        log.debug("[tracker] inner_text failed", exc_info=True)
        return ""


async def _safe_content(page) -> str:
    try:
        return await page.content()
    except Exception:
        log.debug("[tracker] page.content failed", exc_info=True)
        return ""


def _status_from_api(data: dict) -> dict:
    items = data.get("items") or []
    if not items:
        return {"label": None, "desc": "", "date": None, "eta": None, "stage": None, "total": TOTAL_STAGES, "error": "нет событий"}
    last = items[-1]
    code = last.get("event", "")
    desc = ""
    if code in TERMINAL_LABELS:
        label = TERMINAL_LABELS[code]
        stage = None
    else:
        idx_label = _EVENT_INDEX.get(code)
        if idx_label:
            stage, label, desc = idx_label
        else:
            stage = None
            human = _humanize_event_code(code) if code else "неизвестно"
            label = f"Новый статус: {human} ({code})"
            log.warning("[tracker] unknown event code: %r", code)
    moment = last.get("moment", "")
    date = None
    dt_short = None
    if isinstance(moment, str) and len(moment) >= 10:
        try:
            y, m, d = moment[:10].split("-")
            date = f"{d}.{m}.{y}"
        except Exception:
            pass
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(moment).astimezone()
            dt_short = dt.strftime("%d.%m.%y %H:%M")
        except Exception:
            pass

    eta = None
    dbeg, dend = data.get("deliveryDateBegin"), data.get("deliveryDateEnd")
    if dbeg and dend and isinstance(dbeg, str) and isinstance(dend, str):
        try:
            yb, mb, db = dbeg[:10].split("-")
            ye, me, de = dend[:10].split("-")
            if (yb, mb, db) == (ye, me, de):
                eta = f"{db}.{mb}"
            else:
                eta = f"{db}.{mb}–{de}.{me}"
        except Exception:
            pass
    return {"label": label, "desc": desc, "date": date, "dt_short": dt_short, "eta": eta, "stage": stage, "total": TOTAL_STAGES, "error": None}


def _humanize_event_code(code: str) -> str:
    """`OnTheWayToImportCustomsClearancePhantomStatus` → `On The Way To Import Customs Clearance`."""
    import re as _re
    s = code
    # Срезаем характерные суффиксы Ozon.
    for suf in ("PhantomStatus", "Status", "Event"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    s = _re.sub(r"(?<!^)(?=[A-Z])", " ", s).strip()
    return s or code


def _is_antibot(text: str) -> bool:
    blob = text.replace("\xa0", " ")
    return any(p.search(blob) for p in OFFLINE_PATTERNS)


def _dump_debug(url: str, html: str, body_text: str) -> None:
    if not DEBUG_DUMP:
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        tid = url.split("track=", 1)[-1].split("&", 1)[0].replace("/", "_")
        (DEBUG_DIR / f"{tid}.html").write_text(html, encoding="utf-8")
        (DEBUG_DIR / f"{tid}.txt").write_text(body_text, encoding="utf-8")
        log.info("[tracker] dumped debug for %s", tid)
    except Exception:
        log.exception("[tracker] failed to dump debug")
