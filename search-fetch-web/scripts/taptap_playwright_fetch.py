#!/usr/bin/env python3
"""Fetch TapTap game detail reviews via Playwright with Edge.

Replaces taptap_review_fetch.py — uses Playwright instead of AppleScript/Safari.
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import scheduler  # type: ignore
import _playwright_base as pw
from playwright.sync_api import Page

DOMAIN = "taptap.cn"
SEARCH_URL = "https://www.taptap.cn/search/{query}"


def _open_url_fallback(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)


def _site_search(page: Page, query: str) -> dict:
    try:
        selectors = [
            'input[data-e2e="search-input"]',
            'input[placeholder*="搜索"]',
            'input[type="text"][class*="search"]',
            'input.search-input',
            '#search-input',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    loc.click()
                    time.sleep(0.3 + random.random() * 0.5)
                    pw.human_type(page, query)
                    time.sleep(0.3 + random.random() * 0.3)
                    page.keyboard.press("Enter")
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    time.sleep(4)
                    return {"ok": True, "method": "site_search", "selector": sel}
            except Exception:
                continue
        return {"ok": False, "reason": "search_box_not_found"}
    except Exception as exc:
        return {"ok": False, "reason": "site_search_failed", "error": str(exc)}


def _wait_ready(page: Page, timeout: int = 20) -> bool:
    try:
        page.wait_for_load_state("load", timeout=timeout * 1000)
        return True
    except Exception:
        return False


def _evaluate_js(page: Page, js: str) -> dict:
    result = pw.evaluate_json(page, js)
    if result and isinstance(result, dict):
        return result
    return {}


def _click_first_game_view(page: Page, query: str) -> dict:
    # Build a core keyword for fuzzy matching (strip common suffixes like 手游/游戏/online etc.)
    core = query
    for suffix in ("手游", "游戏", "online", "移动版", "手机版", "手机游戏"):
        core = core.replace(suffix, "")
    core = core.strip()
    js = f"""
(() => {{
  const queryText = {json.dumps(query, ensure_ascii=False)};
  const coreText = {json.dumps(core, ensure_ascii=False)};
  const candidates = Array.from(document.querySelectorAll('a,button,div,span'));

  // Strategy 1: exact query match
  let hit = candidates.find(el => {{
    const txt = (el.innerText || '').trim();
    const parent = (el.parentElement?.innerText || '').trim();
    return txt === '查看' && parent.includes(queryText);
  }});

  // Strategy 2: core keyword match (handles query="洛克王国手游" vs display="洛克王国：世界")
  if (!hit && coreText.length >= 2) {{
    hit = candidates.find(el => {{
      const txt = (el.innerText || '').trim();
      const parent = (el.parentElement?.innerText || '').trim();
      return txt === '查看' && parent.includes(coreText);
    }});
  }}

  // Strategy 3: fallback — click first "查看" button in search results
  if (!hit) {{
    hit = candidates.find(el => {{
      const txt = (el.innerText || '').trim();
      return txt === '查看';
    }});
  }}

  if (!hit) return {{ok:false, reason:'no_view_button'}};
  const anchor = hit.closest('a') || hit;
  hit.scrollIntoView({{block:'center'}});
  const r = hit.getBoundingClientRect();
  return {{
    ok: true,
    href: anchor.tagName === 'A' ? (anchor.href || '') : '',
    text: (hit.innerText || '').trim(),
    matched_by: hit ? 'exact' : (candidates.find(el => (el.innerText||'').trim()==='查看' && (el.parentElement?.innerText||'').includes(coreText)) ? 'core' : 'fallback'),
    left: Math.round(r.left),
    top: Math.round(r.top),
    width: Math.round(r.width),
    height: Math.round(r.height)
  }};
}})()
"""
    result = _evaluate_js(page, js)
    if not result.get("ok"):
        return result
    try:
        x = result["left"] + result["width"] // 2
        y = result["top"] + result["height"] // 2
        pw.human_mouse_move(page, x, y)
        page.mouse.click(x, y)
    except Exception as exc:
        result["click_error"] = str(exc)
    return result


def _click_exact_text(page: Page, text: str) -> dict:
    js = f"""
(() => {{
  const target = {json.dumps(text, ensure_ascii=False)};
  const nodes = Array.from(document.querySelectorAll('a,button,div,span'));
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const attrs = el => [norm(el.innerText), norm(el.textContent), norm(el.getAttribute?.('aria-label'))].filter(Boolean);
  const hit = nodes.find(el => attrs(el).some(v => v === target || v.startsWith(target + ' ') || v.startsWith(target + '　') || v.startsWith(target + ':') || v.startsWith(target + '：') || /^评价\\s*\\d+/.test(v)));
  if (!hit) return {{ok:false, reason:'no_target:' + target, title: document.title || '', url: location.href}};
  hit.scrollIntoView({{block:'center'}});
  const r = hit.getBoundingClientRect();
  return {{
    ok: true,
    text: target,
    href: (hit.closest('a') || hit).href || '',
    left: Math.round(r.left),
    top: Math.round(r.top),
    width: Math.round(r.width),
    height: Math.round(r.height)
  }};
}})()
"""
    result = _evaluate_js(page, js)
    if not result.get("ok"):
        return result
    try:
        x = result["left"] + result["width"] // 2
        y = result["top"] + result["height"] // 2
        pw.human_mouse_move(page, x, y)
        page.mouse.click(x, y)
    except Exception as exc:
        result["click_error"] = str(exc)
    return result


def _collect_reviews(page: Page, limit: int = 10, sort_tab: str = "") -> dict:
    js = f"""
(() => {{
  const body = document.body?.innerText || '';
  const lines = body.split('\\n').map(s => s.trim()).filter(Boolean);
  const blocks = [];
  for (let i = 0; i < lines.length; i++) {{
    const line = lines[i];
    if (/天前|小时前|分钟前|月前/.test(line)) {{
      const block = lines.slice(i, i + 18).join('\\n');
      if (block.length > 40) blocks.push(block);
    }}
  }}
  const uniq = [];
  for (const b of blocks) {{
    if (!uniq.includes(b)) uniq.push(b);
    if (uniq.length >= {limit}) break;
  }}
  return {{ title: document.title || '', url: location.href, reviews: uniq, bodyPreview: body.slice(0, 7000), sortTab: {json.dumps(sort_tab, ensure_ascii=False)} }};
}})()
"""
    return _evaluate_js(page, js)


def fetch(query: str = "", review_limit: int = 10) -> dict:
    decision = scheduler.schedule(DOMAIN, "fetch")
    if not decision.get("allowed"):
        return {"ok": False, "items": [], "reason": decision.get("reason"), "scheduler": decision, "checklist": {"search_opened": False, "official_game_detail_opened": False, "review_tab_opened": False, "detail_comment_captured": False, "review_content_captured": False}}
    if decision.get("wait_seconds", 0) > 0:
        time.sleep(decision["wait_seconds"])

    page = pw.new_page()
    try:
        # Navigate to TapTap via Bing search instead of direct URL
        nav = pw.human_navigate_to_site(page, "TapTap", "taptap.cn")
        if not nav.get("ok"):
            page.goto("https://www.taptap.cn", wait_until="domcontentloaded", timeout=30000)
        _wait_ready(page)
        time.sleep(3)
        captcha = pw.wait_for_captcha_or_proceed(page, wait_seconds=60.0)
        if captcha.get("blocked"):
            scheduler.record_result(DOMAIN, "blocked", blocked=True, pages_increment=0)
            return {
                'query': query,
                'checklist': {
                    'search_opened': False,
                    'official_game_detail_opened': False,
                    'review_tab_opened': False,
                    'detail_comment_captured': False,
                    'review_content_captured': False,
                },
                'captcha_blocked': True,
            }

        search_result = _site_search(page, query)
        if not search_result.get("ok"):
            _open_url_fallback(page, SEARCH_URL.format(query=quote_plus(query)))
            _wait_ready(page)
            time.sleep(5)
        pw.scroll_pages(page, min_pages=5)
        step1 = _click_first_game_view(page, query)
        _wait_ready(page)
        time.sleep(6)
        pw.scroll_pages(page, min_pages=5)
        detail_comments = _collect_reviews(page, limit=max(review_limit, 1), sort_tab='detail_fallback')
        step2 = _click_exact_text(page, '评价')
        _wait_ready(page)
        time.sleep(6)
        if not step2.get('ok'):
            checklist = {
                'search_opened': True,
                'official_game_detail_opened': bool(step1.get('ok')),
                'review_tab_opened': False,
                'detail_comment_captured': bool(detail_comments.get('reviews')),
                'review_content_captured': bool(detail_comments.get('reviews')),
            }
            scheduler.record_result(DOMAIN, "ok_partial", blocked=False, pages_increment=1)
            return {
                'query': query,
                'step1': step1,
                'step2': step2,
                'step3': {'ok': False, 'reason': 'skipped'},
                'step4': {'ok': False, 'reason': 'skipped'},
                'detail_comments': detail_comments,
                'comprehensive': {'title': '', 'url': '', 'reviews': [], 'bodyPreview': '', 'sortTab': 'comprehensive'},
                'comprehensive_sorted': {'title': '', 'url': '', 'reviews': [], 'bodyPreview': '', 'sortTab': 'comprehensive'},
                'latest': {'title': '', 'url': '', 'reviews': [], 'bodyPreview': '', 'sortTab': 'latest'},
                'checklist': checklist,
            }
        pw.scroll_pages(page, min_pages=5)
        comprehensive = _collect_reviews(page, limit=max(review_limit, 1), sort_tab='review_default')
        step3 = _click_exact_text(page, '综合')
        _wait_ready(page)
        time.sleep(4)
        pw.scroll_pages(page, min_pages=5)
        comprehensive_sorted = _collect_reviews(page, limit=max(review_limit, 1), sort_tab='comprehensive')
        step4 = _click_exact_text(page, '最新')
        _wait_ready(page)
        time.sleep(4)
        pw.scroll_pages(page, min_pages=5)
        latest = _collect_reviews(page, limit=max(review_limit, 1), sort_tab='latest')
        checklist = {
            'search_opened': True,
            'official_game_detail_opened': bool(step1.get('ok')),
            'review_tab_opened': bool(step2.get('ok')),
            'detail_comment_captured': bool(detail_comments.get('reviews')),
            'review_content_captured': bool((detail_comments.get('reviews') or []) or (comprehensive.get('reviews') or []) or (comprehensive_sorted.get('reviews') or []) or (latest.get('reviews') or [])),
        }
        scheduler.record_result(DOMAIN, "ok", blocked=False, pages_increment=4)
        return {
            'query': query,
            'step1': step1,
            'step2': step2,
            'step3': step3,
            'step4': step4,
            'detail_comments': detail_comments,
            'comprehensive': comprehensive,
            'comprehensive_sorted': comprehensive_sorted,
            'latest': latest,
            'checklist': checklist,
        }
    finally:
        pw.close_all_pages()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: taptap_playwright_fetch.py <query>')
    q = sys.argv[1]
    print(json.dumps(fetch(q), ensure_ascii=False, indent=2))
