#!/usr/bin/env python3
"""Lightweight Taobao search card sampler.

Collects product cards from the search result page without entering item
details. The goal is to get enough list-level product evidence in one page
session by scrolling the result container and de-duplicating cards.
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import scheduler  # type: ignore
import _playwright_base as pw  # type: ignore
from playwright.sync_api import Page

DEBUG_DIR = SCRIPT_DIR.parent / ".data" / "debug"

PLATFORM_CONFIGS: dict[str, dict[str, Any]] = {
    "taobao": {
        "domain": "taobao.com",
        "label": "淘宝",
        "debug_prefix": "taobao",
        "login_url": "https://login.taobao.com/member/login.jhtml",
        "home_url": "https://s.taobao.com/search",
        "search_via_home": True,
        "search_url": lambda query, offset, page_index: f"https://s.taobao.com/search?q={quote_plus(query)}" + (f"&s={offset}" if offset > 0 else ""),
        "offset_step": 44,
        "link_selectors": [
            'a[href*="item.taobao.com/item"]',
            'a[href*="detail.tmall.com/item"]',
            'a[href*="item.htm"]',
            'a[href*="item.taobao.com"]',
            'a[href*="detail.tmall.com"]',
            'a[href*="itemId="]',
            'a[href*="item_id="]',
            'a[href*="id="]',
            'a[data-nid]',
            'a[data-itemid]',
            'a[data-item-id]',
        ],
        "login_hosts": ["login.taobao.com", "login.tmall.com"],
        "search_hosts": ["s.taobao.com"],
    },
}


def _config(platform: str) -> dict[str, Any]:
    if platform == "tmall":
        raise ValueError("tmall uses tmall_sampler.py; do not route it through taobao_sampler.py")
    if platform == "jd":
        raise ValueError("jd uses jd_sampler.py; do not route it through taobao_sampler.py")
    return PLATFORM_CONFIGS.get(platform, PLATFORM_CONFIGS["taobao"])


def login_url(platform: str = "taobao") -> str:
    return str(_config(platform)["login_url"])


def verify_query(platform: str = "taobao") -> str:
    return str(_config(platform).get("verify_query") or "手机")


def scheduler_gate(action: str, platform: str = "taobao") -> dict[str, Any]:
    domain = _config(platform)["domain"]
    max_retries = 3
    for attempt in range(max_retries):
        decision = scheduler.schedule(domain, action)
        if decision.get("allowed"):
            if decision.get("wait_seconds", 0) > 0:
                time.sleep(decision["wait_seconds"])
            return decision
        reason = decision.get("reason", "")
        wait = decision.get("wait_seconds", 0)
        if reason in ("cross_domain_gap", "global_hard_gate") and wait > 0 and attempt < max_retries - 1:
            time.sleep(wait + 1)
            continue
        return decision
    return decision


def record_scheduler_result(outcome: str, blocked: bool = False, pages_increment: int = 1) -> dict[str, Any]:
    return scheduler.record_result(_config("taobao")["domain"], outcome, blocked=blocked, pages_increment=pages_increment)


def record_platform_scheduler_result(platform: str, outcome: str, blocked: bool = False, pages_increment: int = 1) -> dict[str, Any]:
    return scheduler.record_result(_config(platform)["domain"], outcome, blocked=blocked, pages_increment=pages_increment)


def _new_page() -> Page:
    page = pw.new_page()
    for other in list(page.context.pages):
        if other is page:
            continue
        try:
            other.close()
        except Exception:
            pass
    return page


def _debug_page_state(page: Page, label: str, platform: str = "taobao") -> dict[str, Any]:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    screenshot = DEBUG_DIR / f"{_config(platform)['debug_prefix']}_{label}_{stamp}.png"
    try:
        page.screenshot(path=str(screenshot), full_page=False)
    except Exception:
        screenshot = Path("")
    try:
        body_text = page.evaluate("document.body ? document.body.innerText.slice(0, 1500) : ''") or ""
    except Exception:
        body_text = ""
    return {
        "url": page.url,
        "title": _safe_title(page),
        "screenshot": str(screenshot) if screenshot else "",
        "body_text_sample": body_text,
    }


def _safe_title(page: Page) -> str:
    try:
        return page.title() or ""
    except Exception:
        return ""


def _current_host(page: Page) -> str:
    try:
        return urlparse(page.url).netloc.lower()
    except Exception:
        return ""


def _is_expected_search_host(page: Page, platform: str) -> bool:
    host = _current_host(page)
    allowed = [str(x).lower() for x in _config(platform).get("search_hosts", [])]
    return any(host == item or host.endswith("." + item) for item in allowed)


def _page_block_reason(page: Page, platform: str = "taobao") -> str:
    url = (page.url or "").lower()
    if any(host in url for host in _config(platform).get("login_hosts", [])):
        return "login_required"
    try:
        text = page.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''") or ""
    except Exception:
        text = ""
    markers = [
        "亲，请登录",
        "请登录",
        "登录后可查看",
        "安全验证",
        "验证码",
        "滑块",
        "访问受限",
        "访问频繁",
    ]
    if any(marker in text for marker in markers):
        if "登录" in text and ("验证码" not in text and "安全验证" not in text):
            return "login_required"
        return "challenge_or_rate_limited"
    return ""


def _close_extra_pages(page: Page, keep_pages: list[Page]) -> list[str]:
    urls: list[str] = []
    for other in list(page.context.pages):
        if other is page or any(other is keep for keep in keep_pages):
            continue
        try:
            urls.append(other.url)
        except Exception:
            pass
        try:
            other.close()
        except Exception:
            pass
    return urls


def _input_value(locator: Any) -> str:
    try:
        return str(locator.evaluate("(el) => el.value || el.textContent || ''") or "")
    except Exception:
        return ""


def _type_visible_query(page: Page, input_loc: Any, query: str) -> bool:
    try:
        input_loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    box = None
    try:
        box = input_loc.bounding_box(timeout=3000)
    except Exception:
        pass
    if box:
        try:
            pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        except Exception:
            page.mouse.move(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
    input_loc.click(timeout=5000)
    page.wait_for_timeout(int(250 + random.random() * 250))
    page.keyboard.press("Control+A")
    page.wait_for_timeout(int(120 + random.random() * 120))
    page.keyboard.press("Backspace")
    page.wait_for_timeout(int(250 + random.random() * 250))
    pw.human_type(page, query, min_delay=180, max_delay=320)
    page.wait_for_timeout(int(900 + random.random() * 600))
    if query in _input_value(input_loc):
        return True

    try:
        input_loc.click(timeout=3000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(250)
        for char in query:
            input_loc.evaluate(
                """(el, ch) => {
                  el.value = (el.value || '') + ch;
                  el.dispatchEvent(new Event('input', {bubbles: true}));
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                char,
            )
            page.wait_for_timeout(random.randint(160, 300))
        page.wait_for_timeout(700)
        return query in _input_value(input_loc)
    except Exception:
        return False


def _search_from_home(page: Page, query: str, platform: str) -> tuple[bool, str, str]:
    entry_url = str(_config(platform).get("home_url") or "")
    if not entry_url:
        return False, "", "missing_search_entry_url"
    try:
        page.goto(entry_url, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        try:
            page.goto(entry_url, wait_until="load", timeout=45000)
        except Exception:
            return False, entry_url, "search_entry_navigation_failed"
    time.sleep(2.5 + random.random() * 1.5)

    reason = _page_block_reason(page, platform)
    if reason and reason != "login_required":
        return False, entry_url, reason

    try:
        page.evaluate("""() => {
            document.querySelectorAll('a[target], form[target]').forEach(function(el) {
                el.removeAttribute('target');
            });
        }""")
    except Exception:
        pass

    popup_page: dict[str, Page | None] = {"page": None}

    def _handle_popup(popup: Page) -> None:
        popup_page["page"] = popup

    page.on("popup", _handle_popup)

    input_selectors = [
        'input#q',
        'input[name="q"]',
        'input#mq',
        '.search-combobox-input',
        'input[aria-label*="\u641c\u7d22"]',
        'input[placeholder*="\u641c\u7d22"]',
        'input[placeholder*="\u8bf7\u8f93\u5165"]',
        'input[type="search"]',
        'input[type="text"]',
    ]
    submit_selectors = [
        '.btn-search',
        '.search-button',
        '.search-btn',
        'button[type="submit"]',
        "button:has-text('\u641c\u7d22')",
        'input[type="submit"]',
        "a:has-text('\u641c\u7d22')",
    ]

    input_loc = None
    for sel in input_selectors:
        try:
            locs = page.locator(sel)
            for idx in range(min(locs.count(), 8)):
                loc = locs.nth(idx)
                try:
                    if loc.is_visible(timeout=1000) and loc.is_enabled(timeout=1000):
                        input_loc = loc
                        break
                except Exception:
                    continue
            if input_loc is not None:
                break
        except Exception:
            continue

    if input_loc is None:
        return False, entry_url, "search_input_not_found"

    if not _type_visible_query(page, input_loc, query):
        return False, entry_url, "search_input_fill_failed"

    pages_before = list(page.context.pages)
    clicked = False
    for sel in submit_selectors:
        try:
            buttons = page.locator(sel)
            for idx in range(min(buttons.count(), 6)):
                button = buttons.nth(idx)
                try:
                    if not button.is_visible(timeout=1000):
                        continue
                except Exception:
                    continue
                button.click(timeout=5000)
                clicked = True
                break
            if clicked:
                break
        except Exception:
            continue

    if not clicked:
        try:
            page.keyboard.press("Enter")
            clicked = True
        except Exception:
            pass

    if not clicked:
        return False, entry_url, "search_submit_not_found"

    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    time.sleep(2.0 + random.random() * 1.0)

    if _is_expected_search_host(page, platform):
        _close_extra_pages(page, pages_before)
        return True, page.url, ""

    popup_candidates = [p for p in list(page.context.pages) if p is not page and not any(p is old for old in pages_before)]
    if popup_page["page"] is not None and popup_page["page"] not in popup_candidates:
        popup_candidates.append(popup_page["page"])
    for popup in popup_candidates:
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        if _is_expected_search_host(popup, platform):
            popup_url = popup.url
            _close_extra_pages(page, pages_before)
            try:
                page.goto(popup_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                try:
                    page.goto(popup_url, wait_until="load", timeout=45000)
                except Exception:
                    pass
            time.sleep(2.0 + random.random() * 1.0)
            return True, page.url, ""
    _close_extra_pages(page, pages_before)

    reason = _page_block_reason(page, platform)
    if reason:
        return False, page.url, reason
    return False, page.url, f"search_submit_failed:{_current_host(page) or 'unknown'}"


def _open_search_page(page: Page, query: str, offset: int = 0, page_index: int = 0, platform: str = "taobao") -> tuple[bool, str, str]:
    if page_index == 0 and _config(platform).get("search_via_home"):
        opened, search_url, reason = _search_from_home(page, query, platform)
        if not opened:
            return False, search_url, reason
    else:
        search_url = _config(platform)["search_url"](query, offset, page_index)
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            try:
                page.goto(search_url, wait_until="load", timeout=45000)
            except Exception:
                return False, search_url, "navigation_failed"
        time.sleep(3.0 + random.random() * 2.0)

    captcha = pw.wait_for_captcha_or_proceed(page, wait_seconds=120.0)
    if captcha.get("blocked"):
        return False, search_url, "captcha_blocked"

    reason = _page_block_reason(page, platform)
    if reason:
        return False, search_url, reason
    if not _is_expected_search_host(page, platform):
        return False, search_url, f"unexpected_search_host:{_current_host(page) or 'unknown'}"

    for attempt in range(5):
        if _collect_candidates(page, query, limit=1, platform=platform):
            return True, search_url, ""
        if attempt in (1, 3):
            _scroll_search_results(page)
        time.sleep(1.0)

    return True, search_url, ""


def _query_terms(query: str) -> list[str]:
    terms = [part.strip() for part in query.replace("~", " ").replace("|", " ").split() if part.strip()]
    if not terms:
        terms = [query.strip()]
    if len(terms) == 1 and len(terms[0]) > 2:
        text = terms[0]
        terms = [text] + [text[i:i + 2] for i in range(len(text) - 1)]
    return terms


def _collect_candidates(page: Page, query: str, limit: int = 20, platform: str = "taobao") -> list[dict[str, Any]]:
    terms_json = json.dumps(_query_terms(query), ensure_ascii=False)
    link_selectors_json = json.dumps(_config(platform)["link_selectors"], ensure_ascii=False)
    js = f"""
    (() => {{
      const terms = {terms_json};
      const limit = {int(limit)};
      const linkSelectors = {link_selectors_json};
      const seen = new Set();
      const results = [];
      const blacklist = ['大家都在搜', '搜索历史', '猜你喜欢', '相关搜索', '掌柜热卖'];
      const priceRe = /[￥¥]\\s*\\d+(?:\\.\\d+)?/;
      const salesRe = /(\\d+(?:\\.\\d+)?\\s*万?\\+?\\s*)?(人付款|人收货|已售|付款|销量|评价)/;
      function normalizeSales(raw) {{
        if (!raw) return '';
        if (/^已售/.test(raw)) return raw;
        const m = raw.match(/([\\d.]+\\s*万?\\+?)/);
        if (!m) return raw;
        return '已售' + m[1] + '件';
      }}
      function clean(s) {{
        return String(s || '').replace(/\\s+/g, ' ').trim();
      }}
      function blacklisted(text) {{
        return blacklist.some(x => text.includes(x));
      }}
      function normalizeHref(href) {{
        if (!href) return '';
        if (href.startsWith('//')) return location.protocol + href;
        if (href.startsWith('/')) return location.origin + href;
        return href;
      }}
      function itemHrefFromValue(value) {{
        let val = String(value || '');
        if (!val) return '';
        val = val.replace(/\\\\u0026/g, '&').replace(/\\\\u003d/g, '=').replace(/\\\\u002F/g, '/').replace(/\\u0026/g, '&').replace(/\\u003d/g, '=').replace(/\\u002F/g, '/');
        try {{ val = decodeURIComponent(val); }} catch (e) {{}}
        const urlMatch = val.match(/https?:\\/\\/(?:item\\.taobao\\.com|detail\\.tmall\\.com|world\\.taobao\\.com)[^"'<>\\s\\\\]+/i);
        if (urlMatch) return normalizeHref(urlMatch[0].replace(/&amp;/g, '&'));
        const pathMatch = val.match(/(?:\\/\\/)?(?:item\\.taobao\\.com|detail\\.tmall\\.com)\\/item\\.htm\\?[^"'<>\\s\\\\]+/i);
        if (pathMatch) return normalizeHref(pathMatch[0].replace(/&amp;/g, '&'));
        const namedId = val.match(/(?:^|[^a-zA-Z])(?:itemId|item_id|itemIdStr|item_id_str|nid|data-nid|id)["'=:\\s%]+(\\d{{8,20}})/i);
        if (namedId) return 'https://item.taobao.com/item.htm?id=' + namedId[1];
        return '';
      }}
      function findHref(el, fallback) {{
        const fromFallback = itemHrefFromValue(fallback) || normalizeHref(fallback);
        if (fromFallback) return fromFallback;
        const link = el.querySelector && el.querySelector(linkSelectors.join(','));
        if (link) {{
          const direct = itemHrefFromValue(link.getAttribute('href') || link.href || '') || normalizeHref(link.getAttribute('href') || link.href || '');
          if (direct) return direct;
        }}
        let cur = el;
        for (let i = 0; cur && i < 5; i++, cur = cur.parentElement) {{
          if (cur.matches && cur.matches(linkSelectors.join(','))) {{
            const direct = itemHrefFromValue(cur.getAttribute('href') || cur.href || '') || normalizeHref(cur.getAttribute('href') || cur.href || '');
            if (direct) return direct;
          }}
        }}
        const attrNodes = [el, ...(el.querySelectorAll ? Array.from(el.querySelectorAll('*')).slice(0, 160) : [])];
        for (const node of attrNodes) {{
          for (const attr of Array.from(node.attributes || [])) {{
            const direct = itemHrefFromValue(attr.value || '');
            if (direct) return direct;
            if (/^(data-nid|nid|data-itemid|data-item-id|itemid)$/i.test(attr.name || '') && /^\\d{{8,20}}$/.test(attr.value || '')) {{
              return 'https://item.taobao.com/item.htm?id=' + attr.value;
            }}
          }}
        }}
        const html = String(el.outerHTML || '').slice(0, 20000);
        const fromHtml = itemHrefFromValue(html);
        if (fromHtml) return fromHtml;
        return '';
      }}
      function badTitleLine(x) {{
        if (!x) return true;
        if (/^[￥¥楼]?\\s*\\d+(?:\\.\\d+)?$/.test(x)) return true;
        if (/^\\d+(?:\\.\\d+)?$/.test(x)) return true;
        if (priceRe.test(x) || salesRe.test(x)) return true;
        if (/店铺粉丝|浏览|优惠券|满减|加入购物车|看相似|进店|自营店铺|广告|PLUS到手价|券后|到手价|补贴价|政府补贴|国补/.test(x)) return true;
        return false;
      }}
      function findCard(el) {{
        let cur = el;
        for (let i = 0; cur && i < 8; i++, cur = cur.parentElement) {{
          const text = cur.innerText || '';
          if (text.length > 20 && text.length < 1600 && priceRe.test(text)) return cur;
        }}
        return el;
      }}
      function parseCard(card, href) {{
        const rawText = clean(card.innerText || '');
        if (!rawText || rawText.length < 15 || blacklisted(rawText)) return null;
        href = findHref(card, href);
        const lines = (card.innerText || '').split('\\n').map(clean).filter(Boolean);
        const priceLine = lines.find(x => priceRe.test(x)) || '';
        const compactText = rawText.replace(/￥\\s+/g, '￥').replace(/¥\\s+/g, '¥');
        const priceMatch = compactText.match(/[￥¥]\\s*\\d+(?:\\.\\d+)?/) || rawText.match(/[￥¥]\\s*\\d+(?:\\.\\d+)?/);
        const salesLine = lines.find(x => salesRe.test(x)) || '';
        const shopLine = lines.find(x => /店|旗舰|专卖|淘宝|天猫/.test(x) && !priceRe.test(x) && !salesRe.test(x)) || '';
        const locationLine = lines.find(x => /北京|上海|广州|深圳|杭州|苏州|成都|重庆|武汉|南京|天津|义乌|金华|佛山|东莞|海外/.test(x)) || '';
        const titleLine = lines.find(x =>
          x.length >= 4 &&
          !badTitleLine(x) &&
          x !== shopLine &&
          x !== locationLine &&
          !/^广告$|^找同款$|^进店$/.test(x)
        ) || '';
        if (!titleLine) return null;
        const matched = terms.some(term => term && rawText.includes(term));
        if (!matched && !priceLine) return null;
        const price = priceMatch ? clean(priceMatch[0]) : priceLine;
        const key = (href ? href + '|' : '') + titleLine + '|' + price;
        if (seen.has(key)) return null;
        seen.add(key);
        return {{
          title: titleLine,
          price,
          sales: normalizeSales(salesLine),
          shop: shopLine,
          location: locationLine,
          href: href || '',
          text: rawText.slice(0, 1200),
        }};
      }}

      const links = Array.from(document.querySelectorAll(linkSelectors.join(',')));
      for (const a of links) {{
        const href = normalizeHref(a.getAttribute('href') || a.href || '');
        const card = findCard(a);
        const parsed = parseCard(card, href);
        if (parsed) results.push(parsed);
        if (results.length >= limit) return results;
      }}

      const blocks = Array.from(document.querySelectorAll('div, li, section, article'));
      for (const el of blocks) {{
        const text = el.innerText || '';
        if (!priceRe.test(text) || text.length < 20 || text.length > 1600) continue;
        const hrefEl = el.querySelector(linkSelectors.join(','));
        const parsed = parseCard(el, hrefEl ? normalizeHref(hrefEl.getAttribute('href') || hrefEl.href || '') : '');
        if (parsed) results.push(parsed);
        if (results.length >= limit) break;
      }}
      return results;
    }})()
    """
    return pw.evaluate_json_list(page, js)


def _scroll_search_results(page: Page) -> dict[str, Any]:
    before = page.evaluate("""(() => {
      const candidates = [document.scrollingElement, document.documentElement, document.body, ...document.querySelectorAll('main, section, div')].filter(Boolean);
      const visible = candidates.map((el, idx) => {
        const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {left: 0, top: 0, width: window.innerWidth, height: window.innerHeight};
        const scrollHeight = el.scrollHeight || 0;
        const clientHeight = el.clientHeight || 0;
        const scrollTop = el.scrollTop || 0;
        const canScroll = scrollHeight > clientHeight + 80;
        const visibleRect = r.width > 360 && r.height > 180 && r.bottom > 80 && r.top < window.innerHeight - 80;
        return {
          idx,
          canScroll,
          visibleRect,
          area: Math.max(0, Math.min(r.right, window.innerWidth) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0)),
          left: r.left,
          top: r.top,
          width: r.width,
          height: r.height,
          scrollTop,
          scrollHeight,
          clientHeight,
          tag: el.tagName || ''
        };
      }).filter(x => x.canScroll && x.visibleRect);
      visible.sort((a, b) => b.area - a.area);
      return visible[0] || {
        idx: -1,
        left: 0,
        top: 0,
        width: window.innerWidth,
        height: window.innerHeight,
        scrollTop: window.scrollY || 0,
        scrollHeight: Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0),
        clientHeight: window.innerHeight,
        tag: 'WINDOW'
      };
    })()""")
    viewport = page.viewport_size or {"width": 1440, "height": 900}
    x = int(max(20, min((before.get("left", 0) or 0) + (before.get("width", 0) or 0) * 0.55, viewport["width"] - 20)))
    y = int(max(90, min((before.get("top", 0) or 0) + (before.get("height", 0) or 0) * 0.62, viewport["height"] - 40)))
    try:
        pw.human_mouse_move(page, x, y)
    except Exception:
        page.mouse.move(x, y)
    ticks = random.randint(5, 8)
    total_delta = int((before.get("clientHeight", 0) or 700) * random.uniform(0.8, 1.25))
    for _ in range(ticks):
        delta = max(90, int((total_delta / ticks) * random.uniform(0.75, 1.35)))
        page.mouse.wheel(0, delta)
        time.sleep(random.uniform(0.06, 0.18))
    page.wait_for_timeout(int(900 + random.random() * 900))
    after = page.evaluate("""(() => ({
      y: Math.max(window.scrollY || 0, document.documentElement.scrollTop || 0, document.body.scrollTop || 0),
      h: Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0),
      activeScrollTop: (() => {
        const els = Array.from(document.querySelectorAll('main, section, div')).filter(el => el.scrollHeight > el.clientHeight + 80);
        els.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
        return els[0] ? els[0].scrollTop : 0;
      })()
    }))()""")
    return {"before": before, "after": after, "x": x, "y": y}


def _current_page_number(page: Page) -> int:
    try:
        page_param = page.evaluate("""() => {
          try { return Number(new URL(location.href).searchParams.get('page') || '0'); } catch (e) { return 0; }
        }""")
        if int(page_param or 0) > 0:
            return int(page_param)
    except Exception:
        pass
    try:
        return int(page.evaluate("""() => {
          const nodes = Array.from(document.querySelectorAll('[aria-current="page"], .active, .current, [class*="active"], [class*="current"]'));
          for (const el of nodes) {
            const txt = String(el.innerText || el.textContent || '').trim();
            if (/^\\d+$/.test(txt)) return Number(txt);
          }
          const body = document.body ? document.body.innerText : '';
          const m = body.match(/(?:^|\\s)(\\d+)\\s*\\/\\s*\\d+(?:\\s|$)/);
          return m ? Number(m[1]) : 0;
        }""") or 0)
    except Exception:
        return 0


def _jump_to_page(page: Page, target_page: int, before_url: str, before_page: int) -> tuple[bool, dict[str, Any]]:
    if target_page <= 1:
        return False, {"ok": False, "reason": "invalid_jump_page", "target_page": target_page}
    probe: dict[str, Any] = {}
    try:
        probe = page.evaluate("""() => {
          document.querySelectorAll('[data-search-fetch-page-jump-input], [data-search-fetch-page-jump-submit]').forEach(el => {
            el.removeAttribute('data-search-fetch-page-jump-input');
            el.removeAttribute('data-search-fetch-page-jump-submit');
          });
          const inputs = Array.from(document.querySelectorAll('input')).filter(el => {
            const r = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return r.width >= 20 && r.width <= 120 && r.height >= 18 && r.height <= 60 &&
              r.left >= 0 && r.right <= window.innerWidth && r.top >= 0 && r.bottom <= window.innerHeight &&
              style.display !== 'none' && style.visibility !== 'hidden';
          });
          const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], span, div')).filter(el => {
            const text = String(el.innerText || el.textContent || '').replace(/\s+/g, '').trim();
            const cls = String(el.className || '');
            const r = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (text === '确定' || text === '跳转' || /confirm|submit|jump/i.test(cls)) &&
              r.width >= 20 && r.width <= 120 && r.height >= 18 && r.height <= 60 &&
              r.left >= 0 && r.right <= window.innerWidth && r.top >= 0 && r.bottom <= window.innerHeight &&
              style.display !== 'none' && style.visibility !== 'hidden';
          });
          let best = null;
          for (const input of inputs) {
            const ir = input.getBoundingClientRect();
            let bestButton = null;
            let bestDist = 999999;
            for (const button of buttons) {
              const br = button.getBoundingClientRect();
              const dist = Math.abs((br.left + br.right) / 2 - (ir.left + ir.right) / 2) + Math.abs((br.top + br.bottom) / 2 - (ir.top + ir.bottom) / 2);
              if (dist < bestDist) {
                bestDist = dist;
                bestButton = button;
              }
            }
            if (bestButton && bestDist < 260) {
              best = {input, button: bestButton, dist: bestDist};
              break;
            }
          }
          if (!best) return {found: false, input_count: inputs.length, button_count: buttons.length};
          best.input.setAttribute('data-search-fetch-page-jump-input', '1');
          best.button.setAttribute('data-search-fetch-page-jump-submit', '1');
          const ir = best.input.getBoundingClientRect();
          const br = best.button.getBoundingClientRect();
          return {
            found: true,
            input: {x: ir.left + ir.width / 2, y: ir.top + ir.height / 2, width: ir.width, height: ir.height},
            button: {x: br.left + br.width / 2, y: br.top + br.height / 2, width: br.width, height: br.height, text: String(best.button.innerText || best.button.textContent || '').trim()},
            dist: best.dist,
          };
        }""") or {}
    except Exception as exc:
        return False, {"ok": False, "reason": f"page_jump_probe_failed:{type(exc).__name__}", "error": str(exc)[:500]}
    if not probe.get("found"):
        return False, {"ok": False, "reason": "page_jump_not_found", "probe": probe, "target_page": target_page}

    try:
        input_loc = page.locator('[data-search-fetch-page-jump-input="1"]').first
        box = input_loc.bounding_box(timeout=2000)
        if box:
            pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        input_loc.click(timeout=3000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        pw.human_type(page, str(target_page), min_delay=90, max_delay=150)
        page.wait_for_timeout(int(250 + random.random() * 250))
        button_loc = page.locator('[data-search-fetch-page-jump-submit="1"]').first
        box = button_loc.bounding_box(timeout=2000)
        if box:
            pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        button_loc.click(timeout=4000)
    except Exception as exc:
        return False, {"ok": False, "reason": f"page_jump_click_failed:{type(exc).__name__}", "probe": probe, "target_page": target_page}

    try:
        page.wait_for_load_state("domcontentloaded", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(int(2500 + random.random() * 1500))
    after_page = _current_page_number(page)
    if page.url == before_url and after_page <= before_page:
        return False, {
            "ok": False,
            "reason": "page_jump_no_page_change",
            "probe": probe,
            "target_page": target_page,
            "before_url": before_url,
            "after_url": page.url,
            "before_page": before_page,
            "after_page": after_page,
        }
    return True, {
        "ok": True,
        "reason": "jumped",
        "probe": probe,
        "target_page": target_page,
        "before_url": before_url,
        "after_url": page.url,
        "before_page": before_page,
        "after_page": after_page,
        "url_changed": page.url != before_url,
    }


def _click_next_page(page: Page) -> tuple[bool, dict[str, Any]]:
    before_url = page.url
    before_page = _current_page_number(page)
    try:
        for _ in range(3):
            page.evaluate("""() => {
              const bottom = Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0);
              window.scrollTo(0, bottom);
              const els = Array.from(document.querySelectorAll('main, section, div')).filter(el => el.scrollHeight > el.clientHeight + 120);
              els.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
              for (const el of els.slice(0, 4)) {
                try { el.scrollTop = el.scrollHeight; } catch (e) {}
              }
            }""")
            page.wait_for_timeout(int(450 + random.random() * 450))
    except Exception:
        pass

    probe: dict[str, Any] = {}
    try:
        probe = page.evaluate("""() => {
          document.querySelectorAll('[data-search-fetch-next-page]').forEach(el => el.removeAttribute('data-search-fetch-next-page'));
          const nodes = Array.from(document.querySelectorAll('a, button, [role="button"], li, span, div'));
          const candidates = [];
          function clean(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
          function visible(el, r) {
            const style = window.getComputedStyle(el);
            const cx = r.left + r.width / 2;
            const cy = r.top + r.height / 2;
            return r.width >= 16 && r.height >= 16 && r.width <= 260 && r.height <= 90 &&
              cx >= 0 && cx <= window.innerWidth && cy >= 0 && cy <= window.innerHeight &&
              style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || 1) > 0.05;
          }
          function disabled(el, text, cls) {
            return !!el.disabled || el.getAttribute('aria-disabled') === 'true' ||
              /disabled|disable|forbid|unavailable|next-disabled|pagination-disabled|prev|current/i.test(cls) ||
              /不可|禁用/.test(text);
          }
          for (const el of nodes) {
            const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
            const cls = clean(el.className || '');
            const aria = clean(el.getAttribute('aria-label') || '');
            const title = clean(el.getAttribute('title') || '');
            const role = clean(el.getAttribute('role') || '');
            const identity = [text, cls, aria, title, role].join(' ');
            const textLooksNext = /下一页|下页|后一页/.test(text) && text.length <= 30;
            const labelLooksNext = /下一页|下页|后一页/.test([aria, title].join(' '));
            const classLooksNext = !/tabs?|sort|filter|tab-|current|prev/i.test(cls) &&
              (/next-next|pagination-next|pager-next|page-next|next-page/i.test(cls));
            const tag = (el.tagName || '').toUpperCase();
            const clickable = tag === 'A' || tag === 'BUTTON' || role === 'button' || !!el.onclick || /button|btn|page|pagination|pager/i.test(cls);
            const looksNext =
              textLooksNext ||
              labelLooksNext ||
              (classLooksNext && clickable);
            if (!looksNext || !clickable) continue;
            const r = el.getBoundingClientRect();
            if (!visible(el, r)) continue;
            const isDisabled = disabled(el, text, cls);
            candidates.push({
              text, cls, aria, title, role,
              disabled: isDisabled,
              x: r.left + r.width / 2,
              y: r.top + r.height / 2,
              width: r.width,
              height: r.height,
              tag: el.tagName,
            });
            if (!isDisabled) {
              el.setAttribute('data-search-fetch-next-page', '1');
              return {found: true, candidate: candidates[candidates.length - 1], candidates};
            }
          }
          return {found: false, candidates};
        }""") or {}
    except Exception as exc:
        return False, {"ok": False, "reason": f"next_probe_failed:{type(exc).__name__}", "error": str(exc)[:500]}

    if not probe.get("found"):
        jumped, jump_meta = _jump_to_page(page, (before_page or 1) + 1, before_url, before_page or 1)
        return jumped, {"ok": jumped, "reason": "next_button_not_found_then_jump" if jumped else "next_button_not_found", "probe": probe, "jump": jump_meta}

    candidate = probe.get("candidate") or {}
    clicked = False
    try:
        x = int(candidate.get("x") or 0)
        y = int(candidate.get("y") or 0)
        if x > 0 and y > 0:
            pw.human_mouse_move(page, x, y)
            page.mouse.click(x + random.randint(-2, 2), y + random.randint(-2, 2))
            clicked = True
    except Exception:
        clicked = False

    if not clicked:
        try:
            loc = page.locator('[data-search-fetch-next-page="1"]').last
            if loc.count() <= 0:
                return False, {"ok": False, "reason": "next_marker_not_found", "probe": probe}
            loc.scroll_into_view_if_needed(timeout=3000)
            box = loc.bounding_box()
            if box:
                pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
            loc.click(timeout=5000)
            clicked = True
        except Exception as exc:
            return False, {"ok": False, "reason": f"next_click_failed:{type(exc).__name__}", "probe": probe}

    if clicked:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(int(2500 + random.random() * 1500))
        reason = _page_block_reason(page)
        if reason:
            return False, {"ok": False, "reason": reason, "probe": probe, "before_url": before_url, "after_url": page.url, "before_page": before_page, "after_page": _current_page_number(page)}
        after_page = _current_page_number(page)
        url_changed = page.url != before_url
        if not url_changed and before_page and after_page <= before_page:
            retry_meta: dict[str, Any] = {}
            try:
                page.evaluate("""() => {
                  const el = document.querySelector('[data-search-fetch-next-page="1"]');
                  if (el && el.scrollIntoView) el.scrollIntoView({block: 'center', inline: 'nearest'});
                }""")
                page.wait_for_timeout(int(350 + random.random() * 350))
                loc = page.locator('[data-search-fetch-next-page="1"]').last
                if loc.count() > 0:
                    box = loc.bounding_box(timeout=2000)
                    if box:
                        pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                        page.mouse.click(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                    else:
                        loc.click(timeout=3000)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=12000)
                    except Exception:
                        pass
                    page.wait_for_timeout(int(2500 + random.random() * 1200))
                    retry_meta = {"attempted": True, "after_url": page.url, "after_page": _current_page_number(page)}
                    if page.url != before_url or (retry_meta.get("after_page") or 0) > before_page:
                        try:
                            page.evaluate("window.scrollTo(0, 0)")
                        except Exception:
                            pass
                        return True, {
                            "ok": True,
                            "reason": "next_click_no_change_then_retry_click",
                            "probe": probe,
                            "retry": retry_meta,
                            "before_url": before_url,
                            "after_url": page.url,
                            "before_page": before_page,
                            "after_page": retry_meta.get("after_page"),
                            "url_changed": page.url != before_url,
                        }
                else:
                    retry_meta = {"attempted": False, "reason": "marker_missing"}
            except Exception as exc:
                retry_meta = {"attempted": True, "reason": f"retry_failed:{type(exc).__name__}", "error": str(exc)[:300]}
            jumped, jump_meta = _jump_to_page(page, before_page + 1, before_url, before_page)
            if jumped:
                return True, {"ok": True, "reason": "next_click_no_change_then_jump", "probe": probe, "retry": retry_meta, "jump": jump_meta}
            return False, {
                "ok": False,
                "reason": "next_click_no_page_change",
                "probe": probe,
                "retry": retry_meta,
                "jump": jump_meta,
                "before_url": before_url,
                "after_url": page.url,
                "before_page": before_page,
                "after_page": after_page,
            }
        try:
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        return True, {
            "ok": True,
            "reason": "clicked",
            "probe": probe,
            "before_url": before_url,
            "after_url": page.url,
            "before_page": before_page,
            "after_page": after_page,
            "url_changed": url_changed,
        }

    return False, {"ok": False, "reason": "next_click_not_attempted", "probe": probe}


def _href_key(href: str) -> str:
    if not href:
        return ""
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        item_id = (qs.get("id") or qs.get("itemId") or qs.get("item_id") or [""])[0]
        if item_id:
            return f"{parsed.netloc.lower()}{parsed.path}?id={item_id}"
        return f"{parsed.netloc.lower()}{parsed.path}?{parsed.query}".rstrip("?")
    except Exception:
        return href


def _item_key(item: dict[str, Any]) -> str:
    href = item.get("href") or ""
    if href:
        title = " ".join(str(item.get("title", "")).split())
        price = " ".join(str(item.get("price", "")).split())
        return f"href:{_href_key(href)}|title:{title}|price:{price}"
    semantic = "|".join([
        item.get("title", ""),
        item.get("price", ""),
        item.get("sales", ""),
        item.get("shop", ""),
    ])
    if semantic.strip("|"):
        return f"product:{semantic}"
    return ""


def _collect_items_incremental(page: Page, query: str, count: int, max_scrolls: int | None = None, platform: str = "taobao") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    target = max(count, 1)
    scroll_budget = max_scrolls if max_scrolls is not None else min(30, max(4, target // 8))
    if scroll_budget < 0:
        scroll_budget = 0
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    stagnant_rounds = 0
    scroll_rounds = 0
    raw_candidate_count = 0

    for round_index in range(scroll_budget + 1):
        candidates = _collect_candidates(page, query, limit=max(target * 2, target + 30), platform=platform)
        raw_candidate_count = max(raw_candidate_count, len(candidates))
        added = 0
        for item in candidates:
            key = _item_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(item)
            added += 1
            if len(items) >= target:
                break
        if len(items) >= target:
            break

        reason = _page_block_reason(page, platform)
        if pw._detect_captcha(page) or reason in ("challenge_or_rate_limited", "login_required"):
            return items, {
                "blocked": True,
                "reason": reason or "captcha_blocked",
                "scroll_rounds": scroll_rounds,
                "stagnant_rounds": stagnant_rounds,
                "raw_candidate_count": raw_candidate_count,
            }

        if round_index >= scroll_budget:
            break

        scroll_info = _scroll_search_results(page)
        before = scroll_info.get("before") or {}
        after = scroll_info.get("after") or {}
        scroll_rounds += 1
        moved = bool(after and before and (
            after.get("y", 0) > before.get("scrollTop", before.get("y", 0)) + 20
            or after.get("h", 0) > before.get("scrollHeight", before.get("h", 0)) + 20
            or after.get("activeScrollTop", 0) > before.get("scrollTop", 0) + 20
        ))
        if added == 0 and not moved:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        if stagnant_rounds >= 3:
            break

    result_items = items[:target]
    return result_items, {
        "blocked": False,
        "scroll_rounds": scroll_rounds,
        "stagnant_rounds": stagnant_rounds,
        "raw_candidate_count": raw_candidate_count,
        "scroll_budget": scroll_budget,
        "href_count": sum(1 for item in result_items if item.get("href")),
        "missing_href_count": sum(1 for item in result_items if not item.get("href")),
    }


def cards(query: str, count: int = 20, max_scrolls: int | None = None, max_pages: int | None = None, platform: str = "taobao") -> dict[str, Any]:
    cfg = _config(platform)
    decision = scheduler_gate("cards", platform=platform)
    if not decision.get("allowed"):
        return {
            "ok": False,
            "query": query,
            "platform": platform,
            "domain": cfg["domain"],
            "reason": decision.get("reason"),
            "scheduler": decision,
            "items": [],
        }

    page = _new_page()
    try:
        target = max(count, 1)
        # max_pages is a safety cap. When omitted, use enough pages to satisfy
        # larger targets while still keeping the run bounded and sequential.
        page_budget = max(1, int(max_pages)) if max_pages is not None else min(5, max(1, (target + 19) // 20))
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_metas: list[dict[str, Any]] = []
        next_page_attempts: list[dict[str, Any]] = []
        last_search_url = ""

        page_ready = False
        for page_index in range(page_budget):
            offset = page_index * int(cfg.get("offset_step", 44))
            if page_index == 0:
                opened, search_url, reason = _open_search_page(page, query, offset=offset, page_index=page_index, platform=platform)
                last_search_url = search_url
            else:
                if not page_ready:
                    break
                opened, search_url, reason = True, page.url, _page_block_reason(page, platform)
                last_search_url = search_url
                page_ready = False
            if not opened:
                blocked = reason in ("captcha_blocked", "challenge_or_rate_limited", "login_required")
                record_platform_scheduler_result(platform, reason or "search_failed", blocked=blocked, pages_increment=0)
                return {
                    "ok": False,
                    "query": query,
                    "platform": platform,
                    "domain": cfg["domain"],
                    "reason": reason or "search_failed",
                    "search_url": search_url,
                    "scheduler": decision,
                    "items": items,
                    "debug": _debug_page_state(page, reason or "search_failed", platform=platform),
                    "flow_evidence": {"search_opened": bool(items), "card_count": len(items), "pages_opened": page_index + 1, "page_budget": page_budget, "page_budget_mode": "explicit" if max_pages is not None else "dynamic_default"},
                }

            page_items, collect_meta = _collect_items_incremental(page, query, target - len(items), max_scrolls=max_scrolls, platform=platform)
            collect_meta = {**collect_meta, "page_index": page_index + 1, "offset": offset, "page_card_count": len(page_items)}
            page_metas.append(collect_meta)
            for item in page_items:
                key = _item_key(item)
                if not key or key in seen:
                    continue
                seen.add(key)
                items.append(item)
                if len(items) >= target:
                    break

            if collect_meta.get("blocked"):
                reason = collect_meta.get("reason", "blocked")
                record_platform_scheduler_result(platform, reason, blocked=True, pages_increment=0)
                return {
                    "ok": False,
                    "query": query,
                    "platform": platform,
                    "domain": cfg["domain"],
                    "reason": reason,
                    "search_url": search_url,
                    "target_count": count,
                    "card_count": len(items),
                    "items": items,
                    "scheduler": decision,
                    "flow_evidence": {"search_opened": True, "card_count": len(items), "pages_opened": page_index + 1, "page_budget": page_budget, "page_budget_mode": "explicit" if max_pages is not None else "dynamic_default", "pages": page_metas},
                }

            if len(items) >= target:
                break
            if len(page_items) == 0 and page_index > 0:
                break
            if page_index < page_budget - 1:
                page_ready, next_meta = _click_next_page(page)
                next_meta = {**next_meta, "after_page_index": page_index + 1}
                next_page_attempts.append(next_meta)
                if page_metas:
                    page_metas[-1]["next_page"] = next_meta

        record_platform_scheduler_result(platform, "ok" if len(items) >= count else "insufficient_results", blocked=False, pages_increment=0)
        return {
            "ok": len(items) >= count,
            "query": query,
            "platform": platform,
            "domain": cfg["domain"],
            "search_url": last_search_url,
            "target_count": count,
            "card_count": len(items),
            "items": items,
            "scheduler": decision,
            "flow_evidence": {"search_opened": True, "card_count": len(items), "pages_opened": len(page_metas), "page_budget": page_budget, "page_budget_mode": "explicit" if max_pages is not None else "dynamic_default", "pages": page_metas, "next_page_attempts": next_page_attempts},
        }
    finally:
        pw.close_page(page)
