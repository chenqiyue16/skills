#!/usr/bin/env python3
"""Playwright-based, humanized fetch with scheduler consultation and block detection.

Replaces safari_humanized_fetch.py — uses Playwright with Edge instead of AppleScript/Safari.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import scheduler  # type: ignore
import _playwright_base as pw
import _config as _cfg
from playwright.sync_api import Page, BrowserContext


def _save_bilibili_cookies_from_context(context: BrowserContext) -> None:
    """Extract bilibili cookies from Playwright browser context and write to cookie file.

    Called after a successful navigation to bilibili.com so that the comment API
    (which uses urllib, not Playwright) can pick up fresh cookies — especially the
    short-lived bili_ticket which expires every ~3 days.
    """
    try:
        all_cookies = context.cookies()
        bili_cookies = [c for c in all_cookies if ".bilibili.com" in c.get("domain", "")]
        if not bili_cookies:
            return
        cookie_str = "; ".join(f'{c["name"]}={c["value"]}' for c in bili_cookies)
        target = _cfg.BILIBILI_COOKIE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(cookie_str, encoding="utf-8")
        print(f"[search-fetch] 已从 Playwright 回写 {len(bili_cookies)} 条 B站 cookie → {target}", file=sys.stderr)
    except Exception as exc:
        print(f"[search-fetch] B站 cookie 回写失败: {exc}", file=sys.stderr)

BLOCK_MARKERS = [
    "captcha",
    "challenge",
    "access denied",
    "too many requests",
    "访问频繁",
    "verify you are human",
    "robot",
]


def human_pause(min_sec: float = 2.0, max_sec: float = 6.0, precision: int = 2) -> float:
    delay = round(random.uniform(min_sec, max_sec), precision)
    time.sleep(delay)
    return delay


def open_url(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)


def check_captcha(page: Page) -> bool:
    """Check for captcha, wait 60s for human to solve. Returns True if blocked (unsolved)."""
    import _playwright_base as pw
    result = pw.wait_for_captcha_or_proceed(page, wait_seconds=60.0)
    return result.get("blocked", False)


def wait_ready(page: Page, timeout_sec: int = 20) -> bool:
    try:
        page.wait_for_load_state("load", timeout=timeout_sec * 1000)
        return True
    except Exception:
        return False


def infer_page_mode(url: str) -> str:
    lower = url.lower()
    feed_hints = ["search", "topic", "forum", "feed", "moment", "weibo", "search_result", "hot", "new"]
    if any(hint in lower for hint in feed_hints):
        return "feed"
    return "detail"


def feed_scroll_pattern(max_loads: int = 10) -> list[float]:
    loads = random.randint(6, max_loads)
    return [round(random.uniform(0.55, 0.95), 3) for _ in range(loads)]


def detail_scroll_pattern(max_loads: int = 10) -> list[float]:
    roll = random.random()
    if roll < 0.35:
        return []
    if roll < 0.75:
        return [round(random.uniform(0.18, 0.38), 3)]
    loads = random.randint(2, min(max_loads, 4))
    return [round(random.uniform(0.12, 0.42), 3) for _ in range(loads)]


def page_metrics(page: Page) -> dict:
    return page.evaluate(
        "(() => ({"
        "scrollY: window.scrollY || 0, "
        "innerHeight: window.innerHeight || 0, "
        "scrollHeight: document.body.scrollHeight || 0, "
        "textLength: (document.body.innerText || '').length"
        "}))()"
    )


def gentle_scroll_once(page: Page, viewport_ratio: float) -> None:
    ratio = max(0.08, min(viewport_ratio, 0.98))
    delta = int(page.evaluate("window.innerHeight") * ratio)
    # Use mouse.wheel to generate real wheel events instead of window.scrollBy
    page.mouse.wheel(0, delta)


def scroll_and_wait(page: Page, pattern: list[float], min_pause: float, max_pause: float, precision: int = 1) -> list[dict]:
    actions = []
    previous_metrics = page_metrics(page)
    stagnant_rounds = 0

    for ratio in pattern:
        gentle_scroll_once(page, ratio)
        pause = human_pause(min_pause, max_pause, precision=precision)
        current_metrics = page_metrics(page)
        text_growth = current_metrics.get("textLength", 0) - previous_metrics.get("textLength", 0)
        height_growth = current_metrics.get("scrollHeight", 0) - previous_metrics.get("scrollHeight", 0)
        distance_to_bottom = current_metrics.get("scrollHeight", 0) - (current_metrics.get("scrollY", 0) + current_metrics.get("innerHeight", 0))
        near_bottom = distance_to_bottom <= max(300, current_metrics.get("innerHeight", 0) * 0.25)

        actions.append({
            "ratio": ratio,
            "pause": pause,
            "text_growth": text_growth,
            "height_growth": height_growth,
            "distance_to_bottom": distance_to_bottom,
            "near_bottom": near_bottom,
        })

        if text_growth <= 0 and height_growth <= 0:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0

        previous_metrics = current_metrics
        if near_bottom or stagnant_rounds >= 2:
            break

    return actions


def gentle_scroll(page: Page, url: str) -> tuple[str, list[dict]]:
    mode = infer_page_mode(url)
    if mode == "feed":
        return mode, scroll_and_wait(page, feed_scroll_pattern(), 1.0, 5.0, precision=1)
    return mode, scroll_and_wait(page, detail_scroll_pattern(), 1.0, 5.0, precision=1)


def extract_text(page: Page, limit: int = 8000) -> str:
    return page.evaluate(f"document.body.innerText.slice(0, {limit})")


def current_url(page: Page) -> str:
    return page.url


def current_title(page: Page) -> str:
    return page.title()


def extract_cookie_via_playwright(page: Page, url: str, names: list[str] | None = None) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    human_pause(2, 5, precision=1)
    wait_ready(page)
    cookies_js = r'''
(() => {
  const out = {};
  const parts = (document.cookie || '').split(/;\s*/).filter(Boolean);
  for (const item of parts) {
    const idx = item.indexOf('=');
    if (idx === -1) continue;
    const key = item.slice(0, idx);
    const value = item.slice(idx + 1);
    out[key] = value;
  }
  return { url: location.href, title: document.title || '', cookies: out };
})()
'''
    result = page.evaluate(cookies_js)
    cookies = result.get("cookies", {}) or {}
    if names:
        cookies = {k: v for k, v in cookies.items() if k in names}
    result["cookies"] = cookies
    return result


def write_cookie_file(path: str | Path, cookies: dict[str, str], order: list[str] | None = None) -> str:
    ordered_keys = order or list(cookies.keys())
    parts = []
    for key in ordered_keys:
        if key in cookies and cookies[key] != "":
            parts.append(f"{key}={cookies[key]}")
    cookie_str = "; ".join(parts)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(cookie_str)
    return cookie_str


def close_front_document(page: Page) -> None:
    pw.close_page(page)


# --- Platform-specific extractors (work on a given Page) ---

def extract_bilibili_search_samples(page: Page, limit: int = 5) -> list[dict]:
    js = r'''
(() => {
  return Array.from(document.querySelectorAll('.bili-video-card a[href*="/video/"]'))
    .map(a => {
      const title = (a.innerText || '').trim();
      const href = a.href || '';
      const parent = (a.parentElement && a.parentElement.innerText) ? a.parentElement.innerText.trim() : '';
      return { title, href, parent };
    })
    .filter(x => x.href.includes('bilibili.com/video/') && x.title && !/^\d+[\s\S]*\d+:\d+$/.test(x.title))
    .reduce((acc, item) => {
      if (!acc.some(existing => existing.href === item.href)) acc.push(item);
      return acc;
    }, [])
    .slice(0, %d)
})()
''' % limit
    return pw.evaluate_json_list(page, js)


def click_bilibili_video_result(page: Page, index: int = 0) -> dict:
    candidates = extract_bilibili_search_samples(page, limit=max(index + 3, 5))
    if not candidates:
        return {"ok": False, "reason": "no_video_candidate"}
    picked = candidates[min(index, len(candidates) - 1)]
    href = picked.get("href") or ""
    if not href:
        return {"ok": False, "reason": "candidate_missing_href", "candidate": picked}
    bvid = extract_bilibili_bvid(href)
    pages_before = page.context.pages[:]
    if bvid:
        pw.strip_target_blank(page, f'a[href*="/video/{bvid}"]')
        try:
            link = page.locator(f'a[href*="/video/{bvid}"]').first
            link.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            box = link.bounding_box()
            if box:
                pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
            link.click()
        except Exception:
            page.goto(href, wait_until="domcontentloaded", timeout=30000)
    else:
        page.goto(href, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    new_page = pw.pop_newly_opened_page(pages_before, page)
    if new_page:
        pw.close_page(page)
        return {"ok": True, "title": picked.get("title", ""), "href": href, "parent": picked.get("parent", ""), "_switched_page": True, "_new_page": new_page}
    return {"ok": True, "title": picked.get("title", ""), "href": href, "parent": picked.get("parent", "")}


def extract_bilibili_bvid(url: str) -> str:
    if "/video/" not in url:
        return ""
    tail = url.split("/video/", 1)[1]
    return tail.split("/", 1)[0].split("?", 1)[0]


def ensure_bilibili_video_context(search_url: str, index: int = 0, comment_wait_sec: int = 8) -> dict:
    page = pw.new_page()
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        pre_wait = human_pause(2, 6, precision=1)
        # Save cookies after landing on bilibili search page
        if "bilibili.com" in (page.url or ""):
            _save_bilibili_cookies_from_context(page.context)
        ready = wait_ready(page)
        human_pause(3, 6, precision=1)
        # Scroll at least 5 full pages before scraping search results
        pw.scroll_pages(page, min_pages=5)
        candidates = extract_bilibili_search_samples(page, limit=max(index + 5, 8))
        if not candidates:
            return {
                "ok": False,
                "reason": "no_video_candidate",
                "search_url": search_url,
                "ready": ready,
                "pre_wait": pre_wait,
                "candidates": [],
            }

        picked = candidates[min(index, len(candidates) - 1)]
        href = picked.get("href") or ""
        if not href:
            return {
                "ok": False,
                "reason": "candidate_missing_href",
                "search_url": search_url,
                "ready": ready,
                "pre_wait": pre_wait,
                "candidates": candidates,
                "candidate": picked,
            }

        page.goto(href, wait_until="domcontentloaded", timeout=30000)
        video_wait = human_pause(2, 5, precision=1)
        video_ready = wait_ready(page)
        # Scroll at least 5 full pages in video detail before scraping comments
        pw.scroll_pages(page, min_pages=5)
        human_pause(comment_wait_sec, comment_wait_sec + 2, precision=1)
        final_url = current_url(page)
        final_title = current_title(page)
        bvid = extract_bilibili_bvid(final_url or href)
        meta = extract_bilibili_video_meta(page)
        return {
            "ok": bool(video_ready and bvid and final_title and "出错" not in final_title),
            "search_url": search_url,
            "ready": ready,
            "pre_wait": pre_wait,
            "candidates": candidates,
            "candidate": picked,
            "video_ready": video_ready,
            "video_wait": video_wait,
            "final_url": final_url,
            "final_title": final_title,
            "bvid": bvid,
            "meta": meta,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"exception:{type(exc).__name__}",
            "error": str(exc),
            "search_url": search_url,
        }
    finally:
        pw.close_page(page)


def ensure_bilibili_multiple_video_contexts(search_url: str, min_results: int = 5, comment_wait_sec: int = 8) -> dict:
    page = pw.new_page()
    try:
        # Navigate to bilibili via Bing search instead of direct URL
        nav = pw.human_navigate_to_site(page, "B站 bilibili", "bilibili.com")
        if nav.get("ok"):
            captcha = pw.wait_for_captcha_or_proceed(page, wait_seconds=60.0)
            if captcha.get("blocked"):
                return {
                    "ok": False, "reason": "captcha_blocked",
                    "search_url": search_url, "nav": nav,
                }
            # Successfully landed on bilibili.com — save fresh cookies for the API layer
            _save_bilibili_cookies_from_context(page.context)
            # Use bilibili's own search box
            human_pause(2, 4, precision=1)
            query = urlparse(search_url).query
            from urllib.parse import parse_qs
            keyword = parse_qs(query).get("keyword", [""])[0]
            if keyword:
                try:
                    search_input = page.locator('input.nav-search-input, input[class*="search-input"]').first
                    search_input.click()
                    human_pause(0.3, 0.8, precision=2)
                    pw.human_type(page, keyword)
                    human_pause(0.3, 0.6, precision=2)
                    page.keyboard.press("Enter")
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    human_pause(4, 7, precision=1)
                except Exception:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    human_pause(3, 6, precision=1)
            else:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                human_pause(3, 6, precision=1)
        else:
            # Fallback to direct URL if Bing navigation fails
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            human_pause(3, 6, precision=1)
            # Also save cookies from direct navigation if we landed on bilibili
            if "bilibili.com" in (page.url or ""):
                _save_bilibili_cookies_from_context(page.context)

        pw.scroll_pages(page, min_pages=5)
        candidates = extract_bilibili_search_samples(page, limit=max(min_results + 3, 8))
        chosen = candidates[:max(min_results, 1)] if len(candidates) >= min_results else candidates
        contexts = []
        active_page = page
        for i, picked in enumerate(chosen):
            href = picked.get("href") or ""
            if not href:
                continue
            if i > 0:
                active_page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                human_pause(2, 4, precision=1)
                wait_ready(active_page)
                human_pause(1, 2, precision=1)
            bvid = extract_bilibili_bvid(href)
            pages_before = active_page.context.pages[:]
            if bvid:
                pw.strip_target_blank(active_page, f'a[href*="/video/{bvid}"]')
                try:
                    link = active_page.locator(f'a[href*="/video/{bvid}"]').first
                    link.scroll_into_view_if_needed()
                    active_page.wait_for_timeout(300)
                    # Use human mouse move before clicking
                    box = link.bounding_box()
                    if box:
                        pw.human_mouse_move(active_page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                    link.click()
                except Exception:
                    active_page.goto(href, wait_until="domcontentloaded", timeout=30000)
            else:
                active_page.goto(href, wait_until="domcontentloaded", timeout=30000)
            active_page.wait_for_timeout(2000)
            new_page = pw.pop_newly_opened_page(pages_before, active_page)
            detail_page = new_page if new_page else active_page
            video_wait = human_pause(2, 5, precision=1)
            video_ready = wait_ready(detail_page)
            pw.scroll_pages(detail_page, min_pages=5)
            human_pause(comment_wait_sec, comment_wait_sec + 2, precision=1)
            final_url = current_url(detail_page)
            final_title = current_title(detail_page)
            bvid = extract_bilibili_bvid(final_url or href)
            meta = extract_bilibili_video_meta(detail_page)
            contexts.append({
                "ok": bool(video_ready and bvid and final_title and "出错" not in final_title),
                "candidate": picked,
                "video_ready": video_ready,
                "video_wait": video_wait,
                "final_url": final_url,
                "final_title": final_title,
                "bvid": bvid,
                "meta": meta,
            })
            human_pause(1, 2, precision=1)
            if new_page:
                pw.close_page(new_page)
        return {
            "ok": bool(contexts),
            "search_url": search_url,
            "nav": nav if 'nav' in dir() else {},
            "candidates": candidates,
            "contexts": contexts,
            "opened_count": len(contexts),
            "min_results_requested": min_results,
        }
    finally:
        pw.close_all_pages()


def extract_bilibili_video_comments(page: Page, limit: int = 8) -> list[str]:
    js = r'''
(() => {
  return Array.from(document.querySelectorAll('.reply-content, .sub-reply-content, .comment-text, [class*="reply-content"], [class*="comment-content"]'))
    .map(el => (el.innerText || '').trim())
    .filter(Boolean)
    .reduce((acc, item) => {
      if (!acc.includes(item)) acc.push(item);
      return acc;
    }, [])
    .slice(0, %d)
})()
''' % limit
    return [x for x in pw.evaluate_json_list(page, js) if isinstance(x, str)]


def bilibili_comment_debug_snapshot(page: Page) -> dict:
    js = r'''
(() => {
  const selectors = [
    '.reply-content', '.sub-reply-content', '.comment-text',
    '[class*="reply-content"]', '[class*="comment-content"]',
    '#commentapp', '#comment', '.comment-container', '.bb-comment',
    '[class*="comment"]'
  ];
  const counts = {};
  for (const sel of selectors) {
    try { counts[sel] = document.querySelectorAll(sel).length; } catch (e) { counts[sel] = -1; }
  }
  const body = document.body?.innerText || '';
  const lines = body.split('\n').map(s => s.trim()).filter(Boolean);
  const commentHints = lines.filter(line => /(评论|回复|展开|热评|发布|条评论)/.test(line)).slice(0, 40);
  const buttons = Array.from(document.querySelectorAll('button, div, span, a'))
    .map(el => (el.innerText || '').trim())
    .filter(Boolean)
    .filter(text => /(评论|回复|展开|热评|按热度|按时间)/.test(text))
    .slice(0, 40);
  return {
    title: document.title || '', url: location.href, counts,
    commentHints, buttons, bodyPreview: body.slice(0, 3000)
  };
})()
'''
    result = pw.evaluate_json(page, js)
    if isinstance(result, dict):
        return result
    return {"title": "", "url": "", "counts": {}, "commentHints": [], "buttons": [], "bodyPreview": ""}


def scroll_to_bilibili_comment_region(page: Page) -> dict:
    js = r'''
(() => {
  const targets = [
    document.querySelector('#commentapp'),
    document.querySelector('[class*="comment"]'),
    document.querySelector('[data-anchor-id="comment"]')
  ].filter(Boolean);
  const target = targets[0];
  if (!target) {
    window.scrollBy(0, Math.floor(window.innerHeight * 1.6));
    return {ok:false, reason:'no_comment_target', y: window.scrollY || 0};
  }
  const rect = target.getBoundingClientRect();
  const absoluteTop = (window.scrollY || 0) + rect.top;
  window.scrollTo({top: Math.max(0, absoluteTop - 220), behavior: 'instant'});
  return {ok:true, y: window.scrollY || 0, targetTop: absoluteTop, rectTop: rect.top, id: target.id || '', className: target.className || ''};
})()
'''
    result = pw.evaluate_json(page, js)
    if isinstance(result, dict):
        return result
    return {"ok": False, "reason": "scroll_probe_failed"}


def dismiss_bilibili_overlay_and_scroll(page: Page) -> dict:
    js = r'''
(() => {
  const blockers = Array.from(document.querySelectorAll('button, .ad-report, .adcard, .bili-video-card, [class*="close"], [class*="skip"], [class*="ad"]'));
  let clicked = 0;
  for (const el of blockers) {
    const text = (el.innerText || '').trim();
    if (/关闭|跳过|关闭广告|稍后再说|我知道了/.test(text) || /close|skip/i.test(el.className || '')) {
      try { el.click(); clicked += 1; } catch (e) {}
    }
  }
  window.scrollBy(0, Math.floor(window.innerHeight * 0.9));
  return {clicked, y: window.scrollY || 0, title: document.title || ''};
})()
'''
    result = pw.evaluate_json(page, js)
    if isinstance(result, dict):
        return result
    return {"clicked": 0, "y": 0, "title": ""}


def extract_bilibili_video_meta(page: Page) -> dict:
    js = r'''
(() => {
  const title = (document.querySelector('h1')?.innerText || document.title || '').trim();
  const body = (document.body?.innerText || '');
  const lines = body.split('\n').map(s => s.trim()).filter(Boolean);
  const stats = lines.filter(line => /(播放|弹幕|点赞|投币|收藏|转发|评论)/.test(line)).slice(0, 12);
  return { title, stats, bodyPreview: body.slice(0, 2000) };
})()
'''
    result = pw.evaluate_json(page, js)
    if isinstance(result, dict):
        return result
    return {"title": "", "stats": [], "bodyPreview": ""}


def extract_taptap_search_samples(page: Page, limit: int = 5) -> list[dict]:
    js = r'''
(() => {
  return Array.from(document.querySelectorAll('a[href*="/moment/"]'))
    .map(a => {
      const title = (a.innerText || '').trim();
      const href = a.href || '';
      const parent = (a.parentElement && a.parentElement.innerText) ? a.parentElement.innerText.trim() : '';
      return { title, href, parent };
    })
    .filter(x => x.href.includes('taptap.cn/moment/') && x.title)
    .reduce((acc, item) => {
      if (!acc.some(existing => existing.href === item.href)) acc.push(item);
      return acc;
    }, [])
    .slice(0, %d)
})()
''' % limit
    return pw.evaluate_json_list(page, js)


def extract_tieba_search_samples(page: Page, limit: int = 5) -> list[dict]:
    js = r'''
(() => {
  return Array.from(document.querySelectorAll('a[href*="tieba.baidu.com/p/"]'))
    .map(a => {
      const title = (a.innerText || '').trim();
      const href = a.href || '';
      const parent = (a.parentElement && a.parentElement.innerText) ? a.parentElement.innerText.trim() : '';
      return { title, href, parent, className: a.className || '' };
    })
    .filter(x => x.href.includes('tieba.baidu.com/p/') && x.title && x.className && !/^\d+$/.test(x.title) && x.title !== '分享')
    .reduce((acc, item) => {
      if (!acc.some(existing => existing.href === item.href)) acc.push(item);
      return acc;
    }, [])
    .slice(0, %d)
})()
''' % limit
    return pw.evaluate_json_list(page, js)


def extract_douyin_search_samples(page: Page, limit: int = 5, keyword: str = '') -> list[dict]:
    kw_json = json.dumps(keyword)
    js = r'''
(() => {
  return Array.from(document.querySelectorAll('.search-result-card, [data-e2e="search-result-video"], a[href*="/video/"]'))
    .map((el, idx) => {
      const text = (el.innerText || '').trim();
      const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
      const link = el.tagName === 'A' ? el : (el.querySelector && el.querySelector('a[href*="/video/"]'));
      const href = link && link.href ? link.href : '';
      const titleLine = lines[2] || lines[1] || lines[0] || '';
      return { index: idx, title: titleLine, text: text.slice(0, 1000), href };
    })
    .filter(x => x.title && (!%s || x.text.includes(%s) || x.title.includes(%s)))
    .reduce((acc, item) => {
      if (!acc.some(existing => existing.href === item.href && item.href)) acc.push(item);
      return acc;
    }, [])
    .slice(0, %d)
})()
''' % (kw_json, kw_json, kw_json, limit)
    return pw.evaluate_json_list(page, js)


def detect_block_signal(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in BLOCK_MARKERS)


def fetch(url: str) -> dict:
    domain = scheduler.normalize_domain(urlparse(url).netloc)
    lock_id = str(uuid.uuid4())
    previous_lock_id = os.environ.get("SEARCH_FETCH_LOCK_ID")
    os.environ["SEARCH_FETCH_LOCK_ID"] = lock_id
    decision = scheduler.schedule(domain, "fetch")
    if not decision["allowed"]:
        if previous_lock_id is None:
            os.environ.pop("SEARCH_FETCH_LOCK_ID", None)
        else:
            os.environ["SEARCH_FETCH_LOCK_ID"] = previous_lock_id
        return {
            "url": url,
            "domain": domain,
            "allowed": False,
            "reason": decision["reason"],
            "wait_seconds": decision["wait_seconds"],
            "scheduler": decision,
        }

    page = pw.new_page()
    record = None
    final_url = ""
    final_title = ""
    try:
        if decision["wait_seconds"] > 0:
            time.sleep(decision["wait_seconds"])

        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        pre_wait = human_pause(2, 6)
        ready = wait_ready(page)
        page_mode, scroll_actions = gentle_scroll(page, url)
        post_scroll_wait = human_pause(1, 3)
        text = extract_text(page)
        blocked = detect_block_signal(text)
        try:
            final_url = current_url(page)
            final_title = current_title(page)
        except Exception:
            pass
        record = scheduler.record_result(domain, "blocked" if blocked else "ok", blocked=blocked, pages_increment=1)
        return {
            "url": url,
            "domain": domain,
            "allowed": True,
            "ready": ready,
            "pre_wait": pre_wait,
            "post_scroll_wait": post_scroll_wait,
            "page_mode": page_mode,
            "scroll_actions": scroll_actions,
            "blocked": blocked,
            "scheduler": decision,
            "record": record,
            "lock_id": lock_id,
            "text": text,
            "final_url": final_url,
            "final_title": final_title,
            "closed_after_fetch": True,
        }
    except Exception as exc:
        if record is None:
            record = scheduler.record_result(domain, f"error:{type(exc).__name__}", blocked=True, pages_increment=0)
        return {
            "url": url,
            "domain": domain,
            "allowed": True,
            "blocked": True,
            "reason": f"exception:{type(exc).__name__}",
            "error": str(exc),
            "scheduler": decision,
            "record": record,
            "lock_id": lock_id,
            "final_url": final_url,
            "final_title": final_title,
            "closed_after_fetch": True,
        }
    finally:
        pw.close_page(page)
        if previous_lock_id is None:
            os.environ.pop("SEARCH_FETCH_LOCK_ID", None)
        else:
            os.environ["SEARCH_FETCH_LOCK_ID"] = previous_lock_id


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "cookie":
        if len(sys.argv) < 3:
            raise SystemExit("usage: playwright_fetch.py cookie <url> [cookie_name ...]")
        url = sys.argv[2]
        names = sys.argv[3:] if len(sys.argv) > 3 else None
        page = pw.new_page()
        try:
            print(json.dumps(extract_cookie_via_playwright(page, url, names=names), ensure_ascii=False, indent=2))
        finally:
            pw.close_page(page)
    else:
        if len(sys.argv) != 2:
            raise SystemExit("usage: playwright_fetch.py <url>")
        print(json.dumps(fetch(sys.argv[1]), ensure_ascii=False, indent=2))
