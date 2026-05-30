import asyncio
import json
import logging
import os
import re
from pathlib import Path

DEBUG_DUMP = os.environ.get("DEBUG_DUMP", "0").lower() in ("1", "true", "yes", "on")

# Опциональный прокси для Chromium (обход RU-blocked IP)
# PROXY_SERVER: "http://host:port" | "socks5://host:port"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or None
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "").strip() or None
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "").strip() or None


def _proxy_config() -> dict | None:
    if not PROXY_SERVER:
        return None
    cfg: dict = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        cfg["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        cfg["password"] = PROXY_PASSWORD
    return cfg

from playwright.async_api import async_playwright

log = logging.getLogger("ozon-bot.tracker")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

API_URL_MARKER = "/p-api/ozon-track-bff/tracking/"

# Терминальные/отрицательные коды — fallback на случай, если DOM не отдаст этапы.
TERMINAL_LABELS = {
    "Cancelled": "Отменён",
    "Returned": "Возвращён",
    "ReturnedToSeller": "Возвращён продавцу",
}

# JS-сниппет: тянет полный список этапов из DOM (zag3p — детальная вертикальная лента).
# Класс-хэши Ozon регулярно меняются — поэтому матчим по подстрокам внутри class*=.
STAGES_JS = r"""
(() => {
  const items = document.querySelectorAll('[class*="itemContainer"][class*="zag3p"]');
  return [...items].map(el => {
    const status = el.querySelector('[class*="status"][class*="zag3p"]');
    const date   = el.querySelector('[class*="date"][class*="zag3p"]');
    const desc   = el.querySelector('[class*="description"][class*="zag3p"]');
    const inactive = status ? /_inactive/.test(status.className) : true;
    return {
      label: status ? status.textContent.trim() : null,
      date:  date   ? date.textContent.trim()   : null,
      desc:  desc   ? desc.textContent.trim()   : null,
      active: !inactive,
    };
  }).filter(s => s.label);
})()
"""

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
    domains = sorted({c["domain"] for c in result})
    names = sorted({c["name"] for c in result})
    log.info("[tracker] loaded %d cookies from domains=%s", len(result), domains)
    log.debug("[tracker] cookie names: %s", names)
    if len(result) < 10:
        log.warning(
            "[tracker] only %d cookies loaded — Cookie-Editor должен быть на www.ozon.ru, "
            "а не на tracking.ozon.ru (там пусто). Экспортируйте полный набор.",
            len(result),
        )
    return result


FETCH_TIMEOUT_SEC = int(os.environ.get("FETCH_TIMEOUT_SEC", "150"))
BODY_READ_TIMEOUT_MS = 10_000


async def _safe_close(obj, label: str) -> None:
    if obj is None:
        return
    try:
        await obj.close()
    except Exception as e:
        log.warning("[tracker] %s close failed: %s", label, e)


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
        return _error_result(f"timeout {FETCH_TIMEOUT_SEC}s")


async def _fetch_status_inner(url: str, timeout_ms: int) -> dict:
    cookies = _load_cookies()
    if not cookies:
        log.warning("[tracker] no cookies — skipping browser launch for %s", url)
        return _error_result("нет cookies — пришлите cookies.json в чат")
    log.info("[tracker] launching browser for %s", url)
    proxy = _proxy_config()
    if proxy:
        log.info("[tracker] using proxy: %s", proxy["server"])
    async with async_playwright() as p:
        launch_kwargs = dict(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--lang=ru-RU",
                "--window-size=1366,768",
            ],
        )
        if proxy:
            launch_kwargs["proxy"] = proxy
        browser = await p.chromium.launch(**launch_kwargs)
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
                    return _error_result(f"HTTP {resp.status}")
                api_data = await resp.json()
                # Ждём рендера Nuxt, потом снимаем «компактный» скрин и раскрываем коллапсы.
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                png_short = await _safe_screenshot(page)
                await _expand_collapsibles(page)
                # Тянем этапы из DOM (после раскрытия).
                stages = []
                try:
                    stages = await page.evaluate(STAGES_JS) or []
                    log.info("[tracker] extracted %d stages from DOM", len(stages))
                except Exception:
                    log.exception("[tracker] DOM stages extraction failed")
                png = await _safe_screenshot(page)
                result = _build_result(api_data, stages)
                result["png_short"] = png_short
                result["png"] = png
                log.info("[tracker] parsed: %s",
                         {k: v for k, v in result.items()
                          if k not in ("png", "png_short", "stages")})
                return result
            except Exception as e:
                log.warning("[tracker] API wait failed: %s — fallback to body parsing", e)
                await asyncio.sleep(3)
                body_text = await _safe_body(page)
                log.info("[tracker] body preview: %s", body_text[:300].replace("\n", " | "))
                if _is_antibot(body_text):
                    _dump_debug(url, await _safe_content(page), body_text)
                    return _error_result("antibot (обнови cookies.json)")
                _dump_debug(url, await _safe_content(page), body_text)
                return _error_result("статус не распознан")
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


async def _expand_collapsibles(page) -> None:
    """Раскрыть «Показать больше» / «Показать ещё» / «Показать все события» и т.п."""
    texts = [
        "Показать больше",
        "Показать ещё",
        "Показать все события",
        "Показать все",
        "Показать всё",
        "Развернуть",
    ]
    for _ in range(3):  # на случай вложенных коллапсов
        clicked = False
        for t in texts:
            try:
                loc = page.locator(f'button:has-text("{t}"), a:has-text("{t}")').first
                if await loc.count() and await loc.is_visible():
                    await loc.scroll_into_view_if_needed(timeout=2_000)
                    await loc.click(timeout=2_000)
                    log.info("[tracker] expanded: %s", t)
                    await page.wait_for_timeout(400)
                    clicked = True
                    break
            except Exception:
                log.debug("[tracker] expand attempt failed for %r", t, exc_info=True)
        if not clicked:
            break


async def _safe_screenshot(page) -> bytes | None:
    try:
        return await page.screenshot(full_page=True, timeout=10_000, type="png")
    except Exception:
        log.debug("[tracker] screenshot failed", exc_info=True)
        return None


async def _safe_content(page) -> str:
    try:
        return await page.content()
    except Exception:
        log.debug("[tracker] page.content failed", exc_info=True)
        return ""


def _error_result(err: str) -> dict:
    return {"label": None, "desc": "", "date": None, "dt_short": None,
            "eta": None, "stage": None, "total": None, "stages": [], "error": err}


def _format_eta(data: dict) -> str | None:
    dbeg, dend = data.get("deliveryDateBegin"), data.get("deliveryDateEnd")
    if not (isinstance(dbeg, str) and isinstance(dend, str)):
        return None
    try:
        yb, mb, db = dbeg[:10].split("-")
        ye, me, de = dend[:10].split("-")
        return f"{db}.{mb}" if (yb, mb, db) == (ye, me, de) else f"{db}.{mb}–{de}.{me}"
    except Exception:
        return None


def _format_moment(moment: str | None) -> tuple[str | None, str | None]:
    """ISO → (date 'dd.mm.yyyy', dt_short 'dd.mm.yy HH:MM')."""
    if not isinstance(moment, str) or len(moment) < 10:
        return None, None
    date = None
    try:
        y, m, d = moment[:10].split("-")
        date = f"{d}.{m}.{y}"
    except Exception:
        pass
    dt_short = None
    try:
        from datetime import datetime
        dt_short = datetime.fromisoformat(moment).astimezone().strftime("%d.%m.%y %H:%M")
    except Exception:
        pass
    return date, dt_short


def _build_result(api_data: dict, stages: list[dict]) -> dict:
    eta = _format_eta(api_data)
    items = api_data.get("items") or []

    # Терминальное состояние (отмена/возврат) — короткий путь.
    if items:
        last_code = items[-1].get("event", "")
        if last_code in TERMINAL_LABELS:
            date, dt_short = _format_moment(items[-1].get("moment"))
            return {"label": TERMINAL_LABELS[last_code], "desc": "",
                    "date": date, "dt_short": dt_short, "eta": eta,
                    "stage": None, "total": None, "stages": [], "error": None}

    if stages:
        active = [i for i, s in enumerate(stages, 1) if s.get("active")]
        cur_idx = active[-1] if active else len(stages)
        cur = stages[cur_idx - 1]
        # Дата с сайта вида "30.05.26, 18:58" — оставляем как есть.
        date = cur.get("date") or None
        return {"label": cur.get("label") or "—", "desc": cur.get("desc") or "",
                "date": date, "dt_short": date, "eta": eta,
                "stage": cur_idx, "total": len(stages),
                "stages": stages, "error": None}

    # DOM не отдал этапов — голый fallback из API.
    if not items:
        return _error_result("нет событий")
    last = items[-1]
    code = last.get("event", "")
    date, dt_short = _format_moment(last.get("moment"))
    label = TERMINAL_LABELS.get(code) or f"Новый статус: {_humanize_event_code(code)} ({code})"
    if code and code not in TERMINAL_LABELS:
        log.warning("[tracker] DOM empty, falling back to API code: %r", code)
    return {"label": label, "desc": "", "date": date, "dt_short": dt_short,
            "eta": eta, "stage": None, "total": None, "stages": [], "error": None}


def _humanize_event_code(code: str) -> str:
    """`OnTheWayToImportCustomsClearancePhantomStatus` → `On The Way To Import Customs Clearance`."""
    if not code:
        return "неизвестно"
    s = code
    for suf in ("PhantomStatus", "Status", "Event"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    s = re.sub(r"(?<!^)(?=[A-Z])", " ", s).strip()
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
