#!/usr/bin/env python3
"""Playwright sampler for Xiaohongshu direct-entry -> site-search -> detail -> comments.

Uses Playwright with Edge (logged-in profile). Enters xiaohongshu.com directly
and uses the site's own search, avoiding unreliable search engine redirection.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import scheduler  # type: ignore
import _playwright_base as pw
from _config import TMP_DIR
from playwright.sync_api import Page

DOMAIN = "xiaohongshu.com"
BASE_URL = "https://www.xiaohongshu.com"
BLANK_URL = "about:blank"
BLOCK_MARKERS = ["captcha", "challenge", "access denied", "访问频繁", "验证", "登录后查看更多"]

# QR code / login wall markers — HARDCIRCUIT: instant circuit-break when detected.
# The script MUST freeze and wait for human login; never auto-dismiss or retry.
QR_LOGIN_MARKERS = [
    "扫码登录", "二维码登录", "微信扫码", "qq扫码",
    "登录后查看", "请先登录", "请登录后",
    "login-container", "qrcode-panel", "login-container",
    "使用APP扫码", "打开APP扫码",
]
SHELL_PREFIX_MARKERS = ["创作中心", "业务合作", "发现", "直播", "发布", "通知", "沪ICP备13030189号", "行吟信息科技（上海）有限公司", "全部", "图文", "视频", "用户", "筛选"]
COMMENT_SECTION_MARKERS = ["评论", "回复", "展开", "说点什么"]
MAX_CLICK_ATTEMPTS = 3
MIN_COMMENT_LINES = 5
MIN_ACTION_INTERVAL_SEC = 2.0


def human_pause(min_sec: float, max_sec: float, precision: int = 2) -> float:
    delay = round(random.uniform(min_sec, max_sec), precision)
    time.sleep(delay)
    return delay


def scheduler_gate(action: str) -> dict[str, Any]:
    max_retries = 3
    for attempt in range(max_retries):
        lock_id = os.environ.get("SEARCH_FETCH_LOCK_ID", "")
        if not lock_id:
            lock_id = str(uuid.uuid4())
            os.environ["SEARCH_FETCH_LOCK_ID"] = lock_id
        decision = scheduler.schedule(DOMAIN, action)
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


def record_result(outcome: str, blocked: bool = False, pages_increment: int = 1) -> dict[str, Any]:
    return scheduler.record_result(DOMAIN, outcome, blocked=blocked, pages_increment=pages_increment)


def pause_between_actions(seconds: float = MIN_ACTION_INTERVAL_SEC) -> float:
    time.sleep(seconds)
    return seconds


def build_xhs_entry_url() -> str:
    return BASE_URL


def normalize_xhs_url(raw_url: str) -> str | None:
    url = (raw_url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    domain = scheduler.normalize_domain(parsed.netloc or url)
    if domain != DOMAIN:
        return None
    if parsed.path in {"", "/"}:
        return None
    return parsed._replace(fragment="").geturl()


def looks_like_note_url(url: str) -> bool:
    parsed = urlparse(url)
    return any(marker in parsed.path for marker in ["/explore/", "/discovery/item", "/item/"])


def looks_like_search_result_detail_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.path.startswith("/search_result/")


def _extract_note_id(url: str) -> str:
    for part in url.split("/"):
        if len(part) >= 20 and all(c in "0123456789abcdef" for c in part.split("?")[0].split("#")[0]):
            return part.split("?")[0].split("#")[0]
    return ""


# --- Playwright-based operations ---

def _navigate_to_xhs(page: Page) -> dict[str, Any]:
    """Navigate directly to xiaohongshu.com using the logged-in Edge profile."""
    try:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        human_pause(3.0, 5.0)
        ctx = _read_page_context(page)
        current_domain = scheduler.normalize_domain(urlparse(ctx.get("url", "")).netloc)
        if current_domain == DOMAIN:
            return {"ok": True, "url": ctx.get("url", BASE_URL)}
        return {"ok": False, "reason": "navigation_failed", "url": ctx.get("url", "")}
    except Exception as exc:
        return {"ok": False, "reason": "navigation_exception", "error": str(exc)}


def _extract_search_links(page: Page) -> tuple[int, list[dict[str, str]], str]:
    """Extract links and like counts in a single DOM scan to reduce detection surface."""
    js = r"""
(() => {
  return Array.from(document.querySelectorAll('a[href]')).map(a => {
    const href = a.href || '';
    const text = (a.innerText || a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 240);
    const lines = text.split(/\n/).map(l => l.trim()).filter(Boolean);
    let likes = 0;
    for (const line of lines) {
      if (/^\d{1,7}$/.test(line)) {
        const n = parseInt(line, 10);
        if (n > likes) likes = n;
      }
      const m = line.match(/^([\d.]+)万$/);
      if (m) { const n = Math.floor(parseFloat(m[1]) * 10000); if (n > likes) likes = n; }
    }
    return { href, text, likes };
  }).filter(item => item.href || item.text).slice(0, 200)
})()
"""
    try:
        links = pw.evaluate_json_list(page, js)
        return 0, links, ""
    except Exception as exc:
        return 1, [], str(exc)


def _read_page_context(page: Page) -> dict[str, Any]:
    try:
        result = page.evaluate("({title: document.title || '', url: location.href || ''})")
        result["ok"] = True
        return result
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def _click_search_trigger(page: Page) -> dict[str, Any]:
    """Find and click XHS search bar trigger via Playwright mouse (isTrusted=true).

    Returns rect info so _activate_search_box can decide next steps.
    Never uses JS n.click() which produces isTrusted=false events.
    """
    js = r"""
(() => {
  const triggers = [
    'div[class*="search"]',
    'div[class*="Search"]',
    '[class*="search-bar"]',
    '[class*="SearchBar"]',
    'div[placeholder]',
  ];
  for (const sel of triggers) {
    const nodes = Array.from(document.querySelectorAll(sel));
    for (const n of nodes) {
      const text = (n.innerText || n.textContent || '').trim();
      const r = n.getBoundingClientRect();
      if (r.width > 50 && r.height > 10 && r.top < 200) {
        return {ok: true, selector: sel, text: text.slice(0, 60),
                left: Math.round(r.left), top: Math.round(r.top),
                width: Math.round(r.width), height: Math.round(r.height)};
      }
    }
  }
  return {ok: false, reason: 'no_search_trigger_found'};
})()
"""
    result = pw.evaluate_json(page, js)
    if not (result and isinstance(result, dict) and result.get("ok")):
        return {"ok": False, "reason": result.get("reason", "no_search_trigger_found") if result else "evaluate_failed"}
    # Click via Playwright mouse for isTrusted=true events
    x = result["left"] + result["width"] // 2
    y = result["top"] + result["height"] // 2
    pw.human_mouse_move(page, x, y)
    page.mouse.click(x, y)
    return result


def _find_search_box(page: Page) -> dict[str, Any]:
    js = r"""
(() => {
  const selectors = [
    'input[placeholder*="搜索"]',
    'input.search-input',
    'input[type="text"]',
    'div[class*="search"] input',
    '[placeholder*="搜索小红书"]',
    'input[class*="input"]',
    'input[class*="search"]',
    '#search-input',
    '.search-input',
  ];
  const hits = [];
  for (const sel of selectors) {
    const nodes = Array.from(document.querySelectorAll(sel)).slice(0, 10);
    for (const n of nodes) {
      const r = n.getBoundingClientRect();
      hits.push({
        selector: sel,
        placeholder: n.placeholder || '',
        visible: !!(r.width > 0 && r.height > 0),
        left: Math.round(r.left),
        top: Math.round(r.top),
        width: Math.round(r.width),
        height: Math.round(r.height)
      });
    }
  }
  return {candidates: hits};
})()
"""
    result = pw.evaluate_json(page, js)
    if result and isinstance(result, dict):
        result["ok"] = True
        return result
    return {"ok": False, "reason": "search_box_probe_failed", "candidates": []}


def _activate_search_box(page: Page, max_rounds: int = 3) -> dict[str, Any]:
    for round_no in range(max_rounds):
        # First round: try clicking search trigger to open overlay
        if round_no < 1:
            trigger = _click_search_trigger(page)
            if trigger.get("ok"):
                human_pause(1.2, 2.5)

        probe = _find_search_box(page)
        candidates = [c for c in (probe.get("candidates") or []) if c.get("visible")]
        if not candidates:
            human_pause(0.8, 1.5)
            continue
        prioritized = sorted(
            candidates,
            key=lambda c: (
                0 if str(c.get("placeholder", "")) == "搜索小红书" else 1,
                int(c.get("top", 9999)),
                -int(c.get("width", 0)),
            ),
        )
        for candidate in prioritized[:2]:
            try:
                # Use mouse click with trajectory instead of locator.click()
                # to avoid "click without mousemove" bot detection
                loc = page.locator(candidate["selector"]).first
                box = loc.bounding_box()
                if box:
                    cx = int(box["x"] + box["width"] / 2)
                    cy = int(box["y"] + box["height"] / 2)
                    pw.human_mouse_move(page, cx, cy)
                    page.mouse.click(cx, cy)
                else:
                    loc.click()
                human_pause(0.3, 0.8)
                state = _read_search_state(page)
                if state.get("ok") and state.get("search_ready"):
                    return {"ok": True, "matched_candidate": candidate, "search_state": state}
            except Exception:
                continue
    return {"ok": False, "reason": "search_box_not_activated"}


def _read_search_state(page: Page) -> dict[str, Any]:
    js = r"""
(() => {
  const active = document.activeElement;
  const bodyText = document.body ? document.body.innerText.slice(0, 1200) : '';
  const placeholder = active && active.placeholder ? active.placeholder : '';
  const cls = active && typeof active.className === 'string' ? active.className : '';
  const exact_active_match = !!(active && active.tagName === 'INPUT' && placeholder === '搜索小红书' && cls.includes('search-input'));
  const search_hint_visible = bodyText.includes('搜索小红书') || bodyText.includes('历史搜索') || bodyText.includes('大家都在搜');
  return {
    search_ready: exact_active_match,
    exact_active_match,
    active_tag: active ? active.tagName : '',
    active_placeholder: placeholder,
    active_class: cls,
    search_hint_visible
  };
})()
"""
    result = pw.evaluate_json(page, js)
    if result and isinstance(result, dict):
        result["ok"] = True
        return result
    return {"ok": False, "reason": "search_state_probe_failed"}


def _enter_search_query(page: Page, query: str) -> dict[str, Any]:
    activation = _activate_search_box(page)
    if not activation.get("ok"):
        return {"ok": False, "reason": activation.get("reason", "search_box_not_activated"), "activation": activation}
    try:
        # Strip target="_blank" from all existing forms and anchors (non-invasive).
        # DO NOT monkey-patch native APIs (window.open, HTMLAnchorElement.prototype.click,
        # EventTarget.prototype.dispatchEvent, HTMLFormElement.prototype.submit) — XHS can
        # detect prototype tampering via toString() checks for "[native code]".
        page.evaluate("""(() => {
          document.querySelectorAll('form[target="_blank"]').forEach(f => f.removeAttribute('target'));
          document.querySelectorAll('a[target="_blank"]').forEach(a => a.removeAttribute('target'));
        })()""")
        page.keyboard.press("Control+a")
        page.keyboard.type(query, delay=random.randint(35, 130))
        # Occasional longer pause to simulate thinking/reading
        if random.random() < 0.3:
            human_pause(0.3, 0.8)
        ctx_pages_before = page.context.pages[:]
        page.keyboard.press("Enter")
        human_pause(2.0, 3.5)
        # Check if a new tab was opened (XHS may still open one despite strip)
        new_pages = [p for p in page.context.pages if p not in ctx_pages_before]
        if new_pages:
            new_page = new_pages[0]
            try:
                new_page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            new_url = new_page.url
            new_page.close()
            if new_url and DOMAIN in new_url:
                page.goto(new_url, wait_until="domcontentloaded", timeout=30000)
                human_pause(3.0, 5.0)
                return {"ok": True, "query": query, "activation": activation, "new_tab_redirected": True, "new_tab_url": new_url}
            return {"ok": False, "reason": "new_tab_url_not_ready_or_wrong_domain", "new_tab_url": new_url, "activation": activation}
        return {"ok": True, "query": query, "activation": activation}
    except Exception as exc:
        return {"ok": False, "reason": "type_failed", "error": str(exc)}


def _site_search_discovery(page: Page, query: str, count: int = 5) -> dict[str, Any]:
    pause_between_actions()
    search_step = _enter_search_query(page, query)
    search_wait = human_pause(8.0, 12.0, precision=1) if search_step.get("ok") else 0.0

    if search_step.get("ok"):
        links_rc, links, links_err = _extract_search_links(page)
    else:
        links_rc, links, links_err = 1, [], ""

    direct_urls: list[str] = []
    seen: set[str] = set()
    for link in links:
        href = str(link.get("href", ""))
        url = normalize_xhs_url(href)
        if not url or url in seen:
            continue
        if looks_like_note_url(url) or looks_like_search_result_detail_url(url):
            direct_urls.append(url)
            seen.add(url)

    candidates = direct_urls[:count]
    return {
        "ok": bool(candidates),
        "entry_url": build_xhs_entry_url(),
        "query": query,
        "search_step": search_step,
        "search_wait": search_wait,
        "discovery_layer_complete": bool(search_step.get("ok")) and links_rc == 0,
        "candidate_urls": candidates,
        "candidate_count": len(candidates),
    }


def _extract_cards_with_likes(page: Page) -> list[dict[str, Any]]:
    """Extract note card URLs with their like counts from the search results page.

    Parses each card anchor's innerText to find the like count.
    The like count is the largest standalone number in the card text
    (timestamps like "3天前" are embedded strings, not standalone numbers).
    """
    js = r"""
(() => {
  const results = [];
  const seen = new Set();
  const anchors = Array.from(document.querySelectorAll('a[href]')).filter(a => {
    const h = a.href || '';
    return (h.includes('/explore/') || h.includes('/search_result/') || h.includes('/discovery/item/'))
      && h.includes('xiaohongshu.com');
  });
  for (const a of anchors) {
    const url = a.href.split('#')[0];
    if (seen.has(url)) continue;
    seen.add(url);
    const text = (a.innerText || '').trim();
    const lines = text.split(/\n/).map(l => l.trim()).filter(Boolean);
    let likes = 0;
    for (const line of lines) {
      if (/^\d{1,7}$/.test(line)) {
        const n = parseInt(line, 10);
        if (n > likes) likes = n;
      }
      const m = line.match(/^([\d.]+)万$/);
      if (m) {
        const n = Math.floor(parseFloat(m[1]) * 10000);
        if (n > likes) likes = n;
      }
    }
    results.push({ url, likes });
  }
  return results;
})()
"""
    cards = pw.evaluate_json_list(page, js)
    return cards if cards else []


def _scroll_and_collect(page: Page, seen: dict[str, int], scroll_rounds: int = 3) -> None:
    """Scroll down to trigger lazy loading and collect note URLs with like counts.

    Updates `seen` dict (url -> likes) in-place.
    """
    for _ in range(scroll_rounds):
        factor = random.uniform(0.5, 1.2)
        page.evaluate(f"window.scrollBy(0, Math.floor(window.innerHeight * {factor}))")
        human_pause(1.5, 3.0)
        for card in _extract_cards_with_likes(page):
            url = card.get("url", "")
            likes = card.get("likes", 0)
            if url and (url not in seen or likes > seen.get(url, 0)):
                seen[url] = likes


def _direct_entry_discovery(page: Page, query: str, count: int = 5, visited_urls: set[str] | None = None) -> dict[str, Any]:
    """Navigate to XHS via Bing, use site search, scroll to load more, collect note URLs sorted by likes.

    Flow: Bing搜索"小红书" → 点击链接进入小红书 → 站内搜索关键词 → 滚动加载 → 按赞数排序选详情页.
    Direct URL navigation triggers bot detection on high-risk sites.
    """
    from urllib.parse import quote
    skip = visited_urls or set()

    # Step 1: Navigate to xiaohongshu.com via Bing (simulates human behavior)
    search_url = f"{BASE_URL}/search_result?keyword={quote(query)}&source=web_search_result_notes"
    nav = pw.human_navigate_to_site(page, "小红书", "xiaohongshu.com")
    nav_method = "bing_search_click"
    search_step_ok = False

    if nav.get("ok"):
        nav_step = {"ok": True, "url": nav.get("url", BASE_URL), "method": nav_method}
        captcha = pw.wait_for_captcha_or_proceed(page, wait_seconds=60.0)
        if captcha.get("blocked"):
            return {
                "ok": False, "entry_url": nav.get("url", BASE_URL), "query": query,
                "nav_step": nav_step,
                "captcha": captcha,
                "site_search_discovery": {"ok": False, "reason": "captcha_blocked"},
                "candidate_urls": [], "candidate_count": 0,
            }
        human_pause(2.0, 4.0)
        # HARD CONSTRAINT: QR login circuit-break — wait indefinitely for human
        qr = _detect_qr_login(page)
        if qr.get("abandoned"):
            return {
                "ok": False, "entry_url": nav.get("url", BASE_URL), "query": query,
                "nav_step": nav_step,
                "site_search_discovery": {"ok": False, "reason": "qr_login_abandoned"},
                "candidate_urls": [], "candidate_count": 0,
            }
        if qr.get("detected"):
            human_pause(2.0, 4.0)  # brief pause after human logs in
        search_step = _enter_search_query(page, query)
        search_step_ok = search_step.get("ok", False)

    if not nav.get("ok") or not search_step_ok:
        # Fallback: navigate directly to XHS search results page
        nav_method = "direct_search_url"
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        human_pause(5.0, 8.0)
        # QR login detection on fallback path (same HARD CONSTRAINT as Bing path)
        qr = _detect_qr_login(page, timeout_sec=600)
        if qr.get("abandoned"):
            return {
                "ok": False, "entry_url": search_url, "query": query,
                "nav_step": {"ok": False, "reason": "qr_login_abandoned_on_fallback", "method": nav_method},
                "site_search_discovery": {"ok": False, "reason": "qr_login_abandoned"},
                "candidate_urls": [], "candidate_count": 0,
            }
        if qr.get("detected"):
            human_pause(2.0, 4.0)
        current = _read_page_context(page)
        current_domain = scheduler.normalize_domain(urlparse(current.get("url", "")).netloc)
        if current_domain != DOMAIN:
            return {
                "ok": False, "entry_url": search_url, "query": query,
                "nav_step": {"ok": False, "reason": "bing_nav_and_direct_url_both_failed", "details": nav, "direct_url_domain": current_domain},
                "site_search_discovery": {"ok": False, "entry_url": search_url, "query": query, "search_step": {"ok": False, "reason": "direct_url_wrong_domain"}, "discovery_layer_complete": False, "candidate_urls": [], "candidate_count": 0},
                "candidate_urls": [], "candidate_count": 0,
            }
        nav_step = {"ok": True, "url": current.get("url", search_url), "method": nav_method}

    search_url = page.url

    # Scroll at least 5 full pages before scraping (trigger lazy loading)
    pw.scroll_pages(page, min_pages=5)

    # Extract note cards with like counts from initial page load
    card_likes: dict[str, int] = {}
    for card in _extract_cards_with_likes(page):
        url = card.get("url", "")
        likes = card.get("likes", 0)
        if url:
            card_likes[url] = likes

    # Scroll down to load more notes
    _scroll_and_collect(page, card_likes, scroll_rounds=3)

    # Deduplicate by note ID — same note can appear as /explore/{id} and /search_result/{id}
    deduped_likes: dict[str, int] = {}
    seen_note_ids: dict[str, str] = {}  # note_id -> canonical URL (prefer /explore/)
    for url, likes in card_likes.items():
        note_id = _extract_note_id(url)
        if not note_id:
            continue
        if note_id in seen_note_ids:
            # Keep /explore/ as canonical, skip /search_result/ duplicate
            existing = seen_note_ids[note_id]
            if '/explore/' in url and '/search_result/' not in existing:
                # New URL is better canonical, replace
                deduped_likes[url] = max(likes, deduped_likes.get(existing, 0))
                del deduped_likes[existing]
                seen_note_ids[note_id] = url
            elif likes > deduped_likes.get(existing, 0):
                deduped_likes[existing] = likes
            continue
        seen_note_ids[note_id] = url
        deduped_likes[url] = likes

    # Filter out already-visited URLs, then sort by likes descending
    candidates = [(url, likes) for url, likes in deduped_likes.items() if url not in skip]
    candidates.sort(key=lambda x: x[1], reverse=True)
    top_urls = [url for url, _ in candidates[:count]]
    top_likes = {url: likes for url, likes in candidates[:count]}

    site_discovery = {
        "ok": bool(top_urls),
        "entry_url": search_url,
        "query": query,
        "search_step": {"ok": bool(top_urls), "method": "direct_search_url_with_scroll_sorted_by_likes"},
        "discovery_layer_complete": bool(top_urls),
        "candidate_urls": top_urls,
        "candidate_count": len(top_urls),
        "total_discovered": len(deduped_likes),
        "skipped_already_visited": len(deduped_likes) - len(candidates) if skip else 0,
        "top_candidate_likes": top_likes,
    }

    return {
        "ok": bool(top_urls),
        "entry_url": search_url,
        "query": query,
        "nav_step": nav_step,
        "site_search_discovery": site_discovery,
        "candidate_urls": top_urls,
        "candidate_count": len(top_urls),
    }


def _find_note_card_rect(page: Page, url: str) -> dict[str, Any] | None:
    note_id = _extract_note_id(url)
    js = f"""
(() => {{
  const target = {json.dumps(url)};
  const noteId = {json.dumps(note_id)};

  function tryMatch(el) {{
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return null;
    el.scrollIntoView({{block: 'center'}});
    const r2 = el.getBoundingClientRect();
    return {{left: Math.round(r2.left), top: Math.round(r2.top), width: Math.round(r2.width), height: Math.round(r2.height), href: el.href || '', tag: el.tagName}};
  }}

  const anchors = Array.from(document.querySelectorAll('a[href]'));
  let hit = anchors.find(a => (a.href || '') === target);
  if (hit) {{ const r = tryMatch(hit); if (r) return r; hit = null; }}

  if (noteId) {{
    for (const a of anchors) {{
      const h = a.href || '';
      if (h.includes(noteId) && h.includes('xiaohongshu.com')) {{
        const r = tryMatch(a);
        if (r) return r;
      }}
    }}
  }}

  if (noteId) {{
    const sections = Array.from(document.querySelectorAll('section, div[class*="note"], div[class*="card"]'));
    for (const sec of sections) {{
      const links = sec.querySelectorAll('a[href]');
      for (const a of links) {{
        if ((a.href || '').includes(noteId)) {{
          const r = tryMatch(sec);
          if (r) {{ r.href = a.href || r.href; return r; }}
        }}
      }}
    }}
  }}

  return null;
}})()
"""
    result = pw.evaluate_json(page, js)
    if result and isinstance(result, dict):
        return result

    page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
    human_pause(1.5, 2.5)
    for _ in range(4):
        dist = random.randint(400, 800)
        page.evaluate(f"window.scrollBy(0, {dist})")
        human_pause(0.8, 1.5)
        result = pw.evaluate_json(page, js)
        if result and isinstance(result, dict):
            return result
    return None


def _click_note_card(page: Page, url: str) -> dict[str, Any]:
    rect = _find_note_card_rect(page, url)
    if not rect:
        return {"ok": False, "reason": "note_card_not_found", "url": url}
    x = rect["left"] + rect["width"] // 2
    y = rect["top"] + rect["height"] // 2
    try:
        # Move mouse through intermediate waypoints before clicking,
        # avoiding the teleport-click bot detection pattern.
        pw.human_mouse_move(page, x, y)
        page.mouse.click(x, y)
        return {"ok": True, "url": url, "rect": rect, "click_method": "mouse"}
    except Exception as exc:
        try:
            note_id = _extract_note_id(url)
            if note_id:
                loc = page.locator(f'a[href*="{note_id}"]').first
                loc.click()
                return {"ok": True, "url": url, "rect": rect, "click_method": "locator"}
        except Exception:
            pass
        return {"ok": False, "reason": "click_failed", "url": url, "error": str(exc)}


def _go_back(page: Page) -> dict[str, Any]:
    try:
        page.go_back(wait_until="domcontentloaded", timeout=15000)
        return {"ok": True, "did_go_back": True}
    except Exception:
        return {"ok": False, "did_go_back": False}


def _escape(page: Page) -> dict[str, Any]:
    try:
        page.keyboard.press("Escape")
        human_pause(0.3, 0.8)
        return {"ok": True}
    except Exception:
        return {"ok": False}


def _parse_probe(page: Page) -> dict[str, Any]:
    try:
        result = page.evaluate("""({
  title: document.title || '',
  url: location.href || '',
  text: (document.body ? document.body.innerText.slice(0, 2500) : '')
})""")
        body = result.get("text", "")
        url = result.get("url", "")
        title = result.get("title", "")
        domain = scheduler.normalize_domain(urlparse(url).netloc or url)
        return {
            "parse_ok": True,
            "title": title,
            "url": url,
            "probe_domain": domain,
            "text_head": body[:1200],
            "has_comment_word": any(marker in body for marker in COMMENT_SECTION_MARKERS),
            "looks_like_search_page": "搜索" in body and "图文" in body and "用户" in body,
            "is_expected_domain": domain == DOMAIN,
        }
    except Exception:
        return {"parse_ok": False}


def _detect_block(text: str, title: str = "") -> bool:
    hay = f"{title}\n{text}".lower()
    return any(marker.lower() in hay for marker in BLOCK_MARKERS)

def _detect_qr_login(page: Page, timeout_sec: float = 600) -> dict[str, Any]:
    """Detect QR code login wall on XHS. HARD CONSTRAINT: instant circuit-break.

    If QR login is detected, freeze and wait up to timeout_sec for human to scan.
    If human closes the page/browser, or timeout expires, return abandoned=True
    so caller gives up and terminates all tasks for this site.
    Never auto-dismiss, never retry past it, never treat as soft block.
    """
    text = ''
    try:
        text = page.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''") or ''
    except Exception:
        return {'detected': True, 'abandoned': True}

    hay = text.lower()
    has_qr_img = False
    try:
        has_qr_img = bool(page.evaluate("""(() => {
            const imgs = document.querySelectorAll('img[src*="qrcode"], img[src*="qr_code"], canvas');
            return imgs.length > 0;
        })()"""))
    except Exception:
        return {'detected': True, 'abandoned': True}

    triggered = any(marker.lower() in hay for marker in QR_LOGIN_MARKERS)
    if not triggered and not has_qr_img:
        return {'detected': False}

    # HARD BREAK: print warning and wait for human (up to timeout_sec)
    deadline = time.time() + timeout_sec
    print(f'[search-fetch] XHS QR登录墙检测到，已熔断。等待人类扫码（超时 {int(timeout_sec)}s）。', file=sys.stderr)
    matched = [m for m in QR_LOGIN_MARKERS if m.lower() in hay]
    if matched:
        print(f'[search-fetch]    matched: {matched}', file=sys.stderr)

    while True:
        if time.time() >= deadline:
            print(f'[search-fetch] XHS QR登录等待超时（{int(timeout_sec)}s），终止小红书所有任务。', file=sys.stderr)
            return {'detected': True, 'abandoned': True, 'reason': 'timeout'}
        time.sleep(5.0)
        remaining = int(deadline - time.time())
        if remaining > 0 and remaining % 30 == 0:
            print(f'[search-fetch]    仍在等待扫码登录... 剩余 {remaining}s', file=sys.stderr)
        # Check if page still open — closed page = human abandoned
        try:
            _ = page.url
        except Exception:
            print('[search-fetch] XHS 页面已关闭，放弃小红书抓取。', file=sys.stderr)
            return {'detected': True, 'abandoned': True}
        try:
            text = page.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''") or ''
        except Exception:
            print('[search-fetch] XHS 页面已关闭，放弃小红书抓取。', file=sys.stderr)
            return {'detected': True, 'abandoned': True}
        hay = text.lower()
        still_blocked = any(marker.lower() in hay for marker in QR_LOGIN_MARKERS)
        if not still_blocked:
            has_shell = any(marker in text for marker in SHELL_PREFIX_MARKERS[:3])
            if has_shell:
                print('[search-fetch] QR login complete, resuming...', file=sys.stderr)
                return {'detected': True, 'waited': True, 'human_logged_in': True}
        url = page.url or ''
        if DOMAIN in url and '/login' not in url.lower():
            try:
                body = page.evaluate("document.body ? document.body.innerText.slice(0, 1000) : ''") or ''
                if any(marker in body for marker in SHELL_PREFIX_MARKERS[:3]):
                    print('[search-fetch] QR login complete (nav detected), resuming...', file=sys.stderr)
                    return {'detected': True, 'waited': True, 'human_logged_in': True}
            except Exception:
                print('[search-fetch] XHS 页面已关闭，放弃小红书抓取。', file=sys.stderr)
                return {'detected': True, 'abandoned': True}


def _extract_main_text(text: str, title: str = "") -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    filtered = [line for line in lines if not any(prefix in line for prefix in SHELL_PREFIX_MARKERS)]
    if title:
        filtered = [line for line in filtered if line != title.strip()]
    return "\n".join(filtered[:120])


def _find_comment_snippet(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if any(marker in line for marker in COMMENT_SECTION_MARKERS):
            return "\n".join(lines[idx:idx + 12])
    return ""


def _extract_comments(text: str) -> list[str]:
    snippet = _find_comment_snippet(text)
    if not snippet:
        return []
    lines = [line.strip() for line in snippet.splitlines() if line.strip()]
    return lines[1:8] if len(lines) > 1 else []


def _has_minimum_comments(comments: list[str], minimum: int = MIN_COMMENT_LINES) -> bool:
    return len([line for line in comments if line.strip()]) >= minimum


def _is_valid_detail_probe(item: dict[str, Any]) -> bool:
    if not item.get("parse_ok"):
        return False
    if item.get("looks_like_search_page"):
        return False
    if not item.get("is_expected_domain"):
        return False
    url = item.get("url", "")
    if not looks_like_note_url(url):
        return False
    return True


def _full_text_probe(page: Page) -> str:
    try:
        return page.evaluate("document.body ? document.body.innerText.slice(0, 8000) : ''") or ""
    except Exception:
        return ""


def sample(query: str, count: int = 5) -> dict[str, Any]:
    decision = scheduler_gate("fetch")
    if not decision.get("allowed"):
        return {"ok": False, "query": query, "domain": DOMAIN, "reason": decision.get("reason"), "scheduler": decision, "items": [], "flow_evidence": {"site_search_complete": False}}

    page = pw.new_page()
    try:
        discovery = _direct_entry_discovery(page, query, count=count, visited_urls=set())
        candidate_urls = discovery.get("candidate_urls", [])[:count]

        results: list[dict[str, Any]] = []
        blocked = False

        if not candidate_urls:
            record = record_result("discovery_failed", blocked=True, pages_increment=0)
            return {
                "ok": False,
                "query": query,
                "domain": DOMAIN,
                "reason": "no_xhs_note_links_from_direct_entry",
                "scheduler": decision,
                "record": record,
                "direct_entry_discovery": discovery,
                "items": [],
                "flow_evidence": {"site_search_complete": bool(discovery.get("site_search_discovery", {}).get("discovery_layer_complete")), "candidate_count": discovery.get("candidate_count", 0), "opened_note_count": 0, "opened_note_urls": []},
                "summary": {"requested": count, "visited": 0, "detail_successes": 0, "comment_successes": 0, "extracted_comment_lines": 0, "entry_source": "direct_xhs_entry_then_site_search"},
            }

        for idx, detail_url in enumerate(candidate_urls, 1):
            pre = human_pause(1.0, 3.0, precision=1)
            attempts: list[dict[str, Any]] = []
            item: dict[str, Any] = {"index": idx, "discovered_url": detail_url, "waits": {"pre": pre}, "open_attempts": attempts}

            for attempt_no in range(1, MAX_CLICK_ATTEMPTS + 1):
                pause_between_actions()
                click_payload = _click_note_card(page, detail_url)
                open_rc = 0 if click_payload.get("ok") else 1
                load = human_pause(3.0, 6.0, precision=1)
                attempt: dict[str, Any] = {"attempt": attempt_no, "open_rc": open_rc, "load": load, "click_payload": click_payload}

                if open_rc != 0:
                    attempt["status"] = "open_failed"
                    attempts.append(attempt)
                    if attempt_no < MAX_CLICK_ATTEMPTS:
                        human_pause(2.0, 5.0, precision=1)
                    continue

                probe = _parse_probe(page)
                attempt["probe_url"] = probe.get("url")
                attempt["probe_domain"] = probe.get("probe_domain")
                attempt["looks_like_search_page"] = probe.get("looks_like_search_page")
                attempt["is_expected_domain"] = probe.get("is_expected_domain")

                # HARD CONSTRAINT: QR login circuit-break on note detail pages
                qr = _detect_qr_login(page)
                if qr.get("abandoned"):
                    record = record_result("blocked", blocked=True, pages_increment=0)
                    flow_evidence = {"site_search_complete": bool(discovery.get("site_search_discovery", {}).get("discovery_layer_complete")), "candidate_count": discovery.get("candidate_count", 0), "opened_note_count": 0, "opened_note_urls": []}
                    return {
                        "ok": False, "query": query, "domain": DOMAIN,
                        "reason": "qr_login_abandoned", "scheduler": decision, "record": record,
                        "direct_entry_discovery": discovery, "items": results,
                        "flow_evidence": flow_evidence,
                        "summary": {"requested": count, "visited": len(results), "detail_successes": 0, "comment_successes": 0, "extracted_comment_lines": 0, "entry_source": "direct_xhs_entry_then_site_search"},
                    }
                if qr.get("detected"):
                    human_pause(2.0, 4.0)
                    # Re-probe after human login
                    probe = _parse_probe(page)

                if _is_valid_detail_probe(probe):
                    attempt["status"] = "detail_ok"
                    attempts.append(attempt)
                    item.update(probe)
                    item["open_rc"] = open_rc
                    item["load"] = load
                    break
                attempt["status"] = "invalid_detail"
                attempt["text_head"] = probe.get("text_head", "")[:240]
                attempts.append(attempt)
                if attempt_no < MAX_CLICK_ATTEMPTS:
                    human_pause(2.0, 5.0, precision=1)

            if not item.get("parse_ok"):
                item["parse_ok"] = False
                item["main_text"] = ""
                item["comment_snippet"] = ""
                item["comments"] = []
                item["comment_text_length"] = 0
                item["comments_requirement_met"] = False
                item["blocked_signal"] = False
                item["failure_reason"] = attempts[-1]["status"] if attempts else "no_attempts"
            elif _is_valid_detail_probe(item):
                full_text = _full_text_probe(page)
                if full_text:
                    item["full_text"] = full_text[:8000]
                    item["main_text"] = _extract_main_text(full_text, item.get("title", ""))
                    item["comment_snippet"] = _find_comment_snippet(full_text)
                    item["comments"] = _extract_comments(full_text)
                    item["comment_text_length"] = len(item["comment_snippet"])
                    item["comments_requirement_met"] = _has_minimum_comments(item["comments"])
                    item["blocked_signal"] = _detect_block(full_text, item.get("title", ""))
                else:
                    item["main_text"] = ""
                    item["comment_snippet"] = ""
                    item["comments"] = []
                    item["comment_text_length"] = 0
                    item["comments_requirement_met"] = False
                    item["blocked_signal"] = False
            else:
                item["main_text"] = ""
                item["comment_snippet"] = ""
                item["comments"] = []
                item["comment_text_length"] = 0
                item["comments_requirement_met"] = False
                item["blocked_signal"] = False

            if item.get("parse_ok") and not item.get("comments_requirement_met", False):
                item["failure_reason"] = "insufficient_comments"

            results.append(item)

            if item.get("blocked_signal"):
                blocked = True
                break

            if idx < len(candidate_urls):
                item["escape_to_results"] = _escape(page)
                item["between_detail_wait"] = human_pause(1.0, 3.0, precision=1)

        record = record_result("blocked" if blocked else "ok", blocked=blocked, pages_increment=len(results))
        success_count = sum(1 for item in results if _is_valid_detail_probe(item))
        comment_count = sum(1 for item in results if item.get("comments_requirement_met", False))
        extracted_comment_lines = sum(len(item.get("comments", [])) for item in results)
        opened_urls = [item.get("url") for item in results if _is_valid_detail_probe(item) and item.get("url")]
        overall_ok = (not blocked) and bool(results) and all(item.get("comments_requirement_met", False) for item in results if _is_valid_detail_probe(item))
        flow_evidence = {"site_search_complete": bool(discovery.get("site_search_discovery", {}).get("discovery_layer_complete")), "candidate_count": discovery.get("candidate_count", 0), "opened_note_count": success_count, "opened_note_urls": opened_urls}
        return {
            "ok": overall_ok,
            "query": query,
            "domain": DOMAIN,
            "scheduler": decision,
            "record": record,
            "direct_entry_discovery": discovery,
            "items": results,
            "flow_evidence": flow_evidence,
            "summary": {"requested": count, "visited": len(results), "detail_successes": success_count, "comment_successes": comment_count, "extracted_comment_lines": extracted_comment_lines, "entry_source": "direct_xhs_entry_then_site_search"},
        }
    finally:
        pw.close_page(page)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: xhs_sampler.py <query> [count]")
    query = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    print(json.dumps(sample(query, count=count), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
