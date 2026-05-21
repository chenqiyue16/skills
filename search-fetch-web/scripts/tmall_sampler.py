#!/usr/bin/env python3
"""Lightweight Tmall search card sampler.

Collects product cards from the search result page without entering item
details. The goal is to get enough list-level product evidence in one page
session by scrolling the result container and de-duplicating cards.
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse, parse_qs

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import scheduler  # type: ignore
import _playwright_base as pw  # type: ignore
from playwright.sync_api import Page

DEBUG_DIR = SCRIPT_DIR.parent / ".data" / "debug"

PLATFORM_CONFIGS: dict[str, dict[str, Any]] = {
    "tmall": {
        "domain": "tmall.com",
        "label": "天猫",
        "debug_prefix": "tmall",
        "login_url": "https://login.tmall.com",
        "home_url": "https://www.tmall.com/",
        "search_via_home": True,
        "search_url": lambda query, offset, page_index: f"https://list.tmall.com/search_product.htm?q={quote_plus(query)}" + (f"&s={offset}" if offset > 0 else ""),
        "offset_step": 60,
        "link_selectors": [
            'a[href*="detail.tmall.com/item"]',
            'a[href*="detail.tmall.com"]',
        ],
        "login_hosts": ["login.tmall.com", "login.taobao.com"],
        "search_hosts": ["list.tmall.com", "search.tmall.com"],
        "mall_tab_hosts": ["s.taobao.com"],
    },
}


def _config(platform: str = "tmall") -> dict[str, Any]:
    if platform != "tmall":
        raise ValueError(f"tmall_sampler only supports platform='tmall', got {platform!r}")
    return PLATFORM_CONFIGS["tmall"]


def login_url(platform: str = "tmall") -> str:
    return str(_config(platform)["login_url"])


def verify_query(platform: str = "tmall") -> str:
    return str(_config(platform).get("verify_query") or "手机")


def scheduler_gate(action: str, platform: str = "tmall") -> dict[str, Any]:
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
    return scheduler.record_result(_config("tmall")["domain"], outcome, blocked=blocked, pages_increment=pages_increment)


def record_platform_scheduler_result(platform: str, outcome: str, blocked: bool = False, pages_increment: int = 1) -> dict[str, Any]:
    return scheduler.record_result(_config(platform)["domain"], outcome, blocked=blocked, pages_increment=pages_increment)


def _new_page() -> Page:
    return pw.new_page()


def _debug_page_state(page: Page, label: str, platform: str = "tmall") -> dict[str, Any]:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", label)[:80]
    screenshot = DEBUG_DIR / f"{_config(platform)['debug_prefix']}_{safe_label}_{stamp}.png"
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
    if any(host == item or host.endswith("." + item) for item in allowed):
        return True
    mall_tab_hosts = [str(x).lower() for x in _config(platform).get("mall_tab_hosts", [])]
    if any(host == item or host.endswith("." + item) for item in mall_tab_hosts):
        try:
            query = parse_qs(urlparse(page.url).query)
            return query.get("tab", [""])[0] == "mall" or query.get("fromTmallRedirect", [""])[0].lower() == "true"
        except Exception:
            return False
    return False


def _is_taobao_search_host(page: Page) -> bool:
    host = _current_host(page)
    return host == "s.taobao.com" or host.endswith(".s.taobao.com")


def _force_same_tab_search_targets(page: Page) -> None:
    try:
        page.evaluate("""(() => {
          for (const el of document.querySelectorAll('a[target], form[target]')) {
            el.removeAttribute('target');
          }
          for (const form of document.querySelectorAll('form')) {
            form.target = '_self';
          }
        })()""")
    except Exception:
        pass


def _first_visible_locator(page: Page, selectors: list[str]) -> tuple[Any | None, str]:
    for selector in selectors:
        try:
            locs = page.locator(selector)
            count = min(locs.count(), 8)
        except Exception:
            continue
        for index in range(count):
            try:
                loc = locs.nth(index)
                if loc.is_visible(timeout=800):
                    return loc, selector
            except Exception:
                continue
    return None, ""


def _set_search_input(page: Page, loc: Any, query: str) -> bool:
    try:
        loc.click(timeout=5000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(query)
        page.wait_for_timeout(250)
        value = loc.input_value(timeout=1000)
        if value == query:
            return True
    except Exception:
        pass
    try:
        loc.evaluate("""(el, value) => {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(el, value);
          else el.value = value;
          el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        }""", query)
        page.wait_for_timeout(250)
        return loc.input_value(timeout=1000) == query
    except Exception:
        return False


def _close_extra_pages(page: Page, pages_before: list[Page]) -> str:
    opened_url = ""
    try:
        pages_after = page.context.pages[:]
    except Exception:
        return ""
    for candidate in pages_after:
        if candidate in pages_before or candidate == page:
            continue
        try:
            candidate.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        try:
            opened_url = candidate.url or opened_url
        except Exception:
            pass
        try:
            candidate.close()
        except Exception:
            pass
    return opened_url


def _submit_home_search_same_tab(page: Page, query: str, platform: str) -> tuple[bool, str, str]:
    input_selectors = [
        'input[name="q"]',
        'input#mq',
        'input[aria-label*="\u641c\u7d22"]',
        'input[placeholder*="\u641c\u7d22"]',
        'input[type="search"]',
        'input[type="text"]',
    ]
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        "button:has-text('\u641c\u7d22')",
        'button.search-btn',
        '.search-btn',
        '#mallSearch button',
        '.mallSearch button',
    ]

    input_loc, input_selector = _first_visible_locator(page, input_selectors)
    if input_loc is None:
        return False, page.url, "search_input_not_found"
    if not _set_search_input(page, input_loc, query):
        return False, page.url, f"search_input_failed:{input_selector or 'unknown'}"

    _force_same_tab_search_targets(page)
    pages_before = page.context.pages[:]
    before_url = page.url
    button_loc, _ = _first_visible_locator(page, submit_selectors)
    try:
        if button_loc is not None:
            box = button_loc.bounding_box()
            if box:
                x = int(box["x"] + box["width"] / 2 + random.uniform(-3, 3))
                y = int(box["y"] + box["height"] / 2 + random.uniform(-3, 3))
                pw.human_mouse_move(page, x, y)
                page.wait_for_timeout(int(150 + random.random() * 250))
            button_loc.click(timeout=5000)
        else:
            input_loc.click(timeout=3000)
            page.keyboard.press("Enter")
    except Exception:
        try:
            input_loc.click(timeout=3000)
            page.keyboard.press("Enter")
        except Exception:
            return False, page.url, "search_submit_failed"

    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(int(2000 + random.random() * 1200))

    popup_url = _close_extra_pages(page, pages_before)
    if popup_url and page.url == before_url:
        try:
            page.goto(popup_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(int(1600 + random.random() * 900))
        except Exception:
            pass

    if _is_expected_search_host(page, platform):
        return True, page.url, ""
    return False, page.url, f"unexpected_search_host:{_current_host(page) or 'unknown'}"


def _page_block_reason(page: Page, platform: str = "tmall") -> str:
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


def _search_from_home(page: Page, query: str, platform: str) -> tuple[bool, str, str]:
    home_url = str(_config(platform).get("home_url") or "")
    if not home_url:
        return False, "", "missing_home_url"
    fallback_url = _config(platform)["search_url"](query, 0, 0)
    try:
        page.goto(home_url, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        try:
            page.goto(home_url, wait_until="load", timeout=45000)
        except Exception:
            return False, home_url, "home_navigation_failed"
    time.sleep(2.5 + random.random() * 1.5)

    reason = _page_block_reason(page, platform)
    if reason and reason != "login_required":
        return False, home_url, reason

    opened, url, submit_reason = _submit_home_search_same_tab(page, query, platform)
    if opened:
        return True, url, ""
    if submit_reason not in ("search_input_not_found",) and not submit_reason.startswith("unexpected_search_host:"):
        return False, url, submit_reason

    try:
        page.goto(fallback_url, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        try:
            page.goto(fallback_url, wait_until="load", timeout=45000)
        except Exception:
            return False, fallback_url, "navigation_failed"
    time.sleep(3.0 + random.random() * 2.0)
    if _is_expected_search_host(page, platform):
        return True, page.url, ""
    return False, page.url, f"unexpected_search_host:{_current_host(page) or 'unknown'}"


def _open_search_page(page: Page, query: str, offset: int = 0, page_index: int = 0, platform: str = "tmall") -> tuple[bool, str, str]:
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

    captcha = pw.wait_for_captcha_or_proceed(page, wait_seconds=30.0)
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
        terms = [text]
        if len(text) >= 4:
            terms.extend([text[:2], text[2:]])
    return terms


def _collect_candidates(page: Page, query: str, limit: int = 20, platform: str = "tmall") -> list[dict[str, Any]]:
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
      function relevant(text) {{
        const full = terms[0] || '';
        if (full && text.includes(full)) return true;
        if (terms.length >= 3) return text.includes(terms[1]);
        return terms.some(term => term && text.includes(term));
      }}
      function normalizeHref(href) {{
        if (!href) return '';
        if (href.startsWith('//')) return location.protocol + href;
        if (href.startsWith('/')) return location.origin + href;
        return href;
      }}
      function findHref(el, fallback) {{
        if (fallback) return normalizeHref(fallback);
        const link = el.querySelector && el.querySelector(linkSelectors.join(','));
        if (link) return normalizeHref(link.getAttribute('href') || link.href || '');
        let cur = el;
        for (let i = 0; cur && i < 5; i++, cur = cur.parentElement) {{
          if (cur.matches && cur.matches(linkSelectors.join(','))) return normalizeHref(cur.getAttribute('href') || cur.href || '');
        }}
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
        const matched = relevant(rawText);
        if (!matched || !href) return null;
        const price = priceMatch ? clean(priceMatch[0]) : priceLine;
        const key = titleLine + '|' + price + '|' + salesLine + '|' + shopLine;
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


def _click_next_page(page: Page) -> bool:
    try:
        page.evaluate("window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))")
        page.wait_for_timeout(int(600 + random.random() * 700))
    except Exception:
        pass
    selectors = [
        'a:has-text("下一页")',
        'button:has-text("下一页")',
        '[aria-label*="下一页"]',
        '[title*="下一页"]',
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).last
            if loc.count() <= 0:
                continue
            text = ""
            try:
                text = (loc.inner_text(timeout=1000) or "") + " " + (loc.get_attribute("class", timeout=1000) or "")
            except Exception:
                pass
            if "disabled" in text.lower() or "不可" in text:
                continue
            loc.scroll_into_view_if_needed(timeout=3000)
            box = loc.bounding_box()
            if box:
                pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
            loc.click(timeout=5000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(int(2500 + random.random() * 1500))
            return True
        except Exception:
            continue
    return False


def _item_key(item: dict[str, Any]) -> str:
    href = item.get("href") or ""
    if href:
        try:
            parsed = urlparse(href)
            item_id = parse_qs(parsed.query).get("id", [""])[0]
            if item_id:
                return f"id:{item_id}"
            match = re.search(r"/(\d{5,})(?:\.html)?", parsed.path)
            if match:
                return f"id:{match.group(1)}"
            return f"href:{href.split('?')[0]}"
        except Exception:
            return f"href:{href.split('?')[0]}"
    semantic = "|".join([
        item.get("title", ""),
        item.get("price", ""),
        item.get("sales", ""),
        item.get("shop", ""),
    ])
    if semantic.strip("|"):
        return f"product:{semantic}"
    return ""


def _collect_items_incremental(page: Page, query: str, count: int, max_scrolls: int | None = None, platform: str = "tmall") -> tuple[list[dict[str, Any]], dict[str, Any]]:
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

    return items[:target], {
        "blocked": False,
        "scroll_rounds": scroll_rounds,
        "stagnant_rounds": stagnant_rounds,
        "raw_candidate_count": raw_candidate_count,
        "scroll_budget": scroll_budget,
    }


def cards(query: str, count: int = 20, max_scrolls: int | None = None, max_pages: int | None = None, platform: str = "tmall") -> dict[str, Any]:
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
    pages_at_start = page.context.pages[:]
    try:
        target = max(count, 1)
        # Tmall search currently redirects to the Taobao mall tab. Opening
        # multiple result pages in one run is noisy and tends to trip risk
        # controls, so this sampler is intentionally single-page + scroll.
        page_budget = 1
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_metas: list[dict[str, Any]] = []
        last_search_url = ""

        page_ready = False
        for page_index in range(page_budget):
            offset = page_index * int(cfg.get("offset_step", 44))
            if page_index == 0 or not page_ready:
                opened, search_url, reason = _open_search_page(page, query, offset=offset, page_index=page_index, platform=platform)
                last_search_url = search_url
            else:
                opened, search_url, reason = True, page.url, _page_block_reason(page, platform)
                last_search_url = search_url
                page_ready = False
            if not opened:
                blocked = reason in ("captcha_blocked", "challenge_or_rate_limited")
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
                    "flow_evidence": {"search_opened": bool(items), "card_count": len(items), "pages_opened": page_index + 1},
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
                blocked = reason in ("captcha_blocked", "challenge_or_rate_limited")
                record_platform_scheduler_result(platform, reason, blocked=blocked, pages_increment=0)
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
                    "flow_evidence": {"search_opened": True, "card_count": len(items), "pages_opened": page_index + 1, "pages": page_metas},
                }

            if len(items) >= target:
                break
            if len(page_items) == 0 and page_index > 0:
                break
            if page_index < page_budget - 1:
                page_ready = _click_next_page(page)

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
            "flow_evidence": {"search_opened": True, "card_count": len(items), "pages_opened": len(page_metas), "pages": page_metas},
        }
    finally:
        _close_extra_pages(page, pages_at_start)
        pw.close_page(page)
