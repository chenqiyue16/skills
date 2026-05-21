#!/usr/bin/env python3
"""Fetch Tieba forum samples via Playwright with Edge.

Replaces tieba_bar_fetch.py — uses Playwright instead of AppleScript/Safari.
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _playwright_base as pw
import scheduler  # type: ignore
from playwright.sync_api import Page

DOMAIN = "tieba.baidu.com"

BAR_URL = "https://tieba.baidu.com/f?kw={query}"
THREAD_API = "https://tieba.baidu.com/f/good?kw={query}&pn={pn}&ie=utf-8"
SEARXNG_DISCOVERY_HINT = "贴吧 + 目标游戏名吧"
BAD_THREAD_KEYWORDS = ["置顶", "吧规", "导航", "公告", "招新", "水楼", "水贴", "黄牌", "升级", "签到", "自建水楼", "欢迎一起水"]
LOW_VALUE_THREAD_KEYWORDS = ["水楼", "水贴", "黄牌", "升级", "签到", "自建水楼", "欢迎一起水", "氵"]


def _open_url(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)


def _open_bar(page: Page, query: str) -> None:
    # Navigate to tieba via Bing search instead of direct URL
    nav = pw.human_navigate_to_site(page, "百度贴吧", "tieba.baidu.com")
    if not nav.get("ok"):
        page.goto("https://tieba.baidu.com", wait_until="domcontentloaded", timeout=30000)
    time.sleep(1.5)
    captcha = pw.wait_for_captcha_or_proceed(page, wait_seconds=60.0)
    if captcha.get("blocked"):
        return
    time.sleep(1)
    try:
        search_input = page.locator('input[name="kw1"], input[placeholder*="搜索"], #wd1, input[name="kw"]').first
        search_input.click()
        time.sleep(0.3 + random.random() * 0.4)
        pw.human_type(page, query)
        time.sleep(0.3 + random.random() * 0.3)
        pw.strip_target_blank(page, 'a[target="_blank"]')
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        time.sleep(3)
    except Exception:
        pass
    # Verify we landed on the bar page (/f?kw=...), not the search results page (/f/search/res)
    if '/f/search/' in page.url or '/f?kw=' not in page.url:
        _open_url(page, BAR_URL.format(query=quote_plus(query)))


def _wait_ready(page: Page, seconds: int = 20) -> bool:
    try:
        page.wait_for_load_state("load", timeout=seconds * 1000)
        return True
    except Exception:
        return False


def _scroll_bar(page: Page) -> None:
    """Scroll the thread content area only. Never touch the left sidebar."""
    page.evaluate("""
    (() => {
        const scroller = document.querySelector('.frs-page-wrap');
        if (scroller) scroller.scrollTop += scroller.clientHeight * 0.9;
    })()
    """)
    time.sleep(0.3)


def _collect_visible_threads(page: Page, limit: int = 120) -> list[dict[str, Any]]:
    js = f"""
(() => {{
  // Exclude sidebar / recommendation areas
  const excludeSel = ['aside',' .aside','#aside','.sidebar','.side-bar',
    '.card_right','.recommend','[class*="recommend"]','[class*="sidebar"]',
    '[class*="aside"]','footer','.foot',
    '.left-content','.left-nav-drawer','div.left-content'];
  const links = Array.from(document.querySelectorAll('a[href*="/p/"]'));
  const filtered = links.filter(a => {{
    for (const s of excludeSel) {{
      if (a.closest(s)) return false;
    }}
    return true;
  }});
  return filtered.map((a) => {{
    const text = (a.innerText || '').trim();
    const href = a.href || '';
    const parent = (a.parentElement?.innerText || '').trim();
    return {{ text, href, parent }};
  }}).filter(x => x.text && x.href && !x.href.includes('showComment=1')).slice(0, {limit})
}})()
"""
    return pw.evaluate_json_list(page, js)


def _thread_score(item: dict[str, Any]) -> int:
    text = item.get('text', '') or ''
    parent = item.get('parent', '') or ''
    if any(k in text or k in parent for k in BAD_THREAD_KEYWORDS):
        return -999
    score = 0
    if '回复' in parent:
        score += 15
    if '分享' in parent:
        score += 5
    nums = [int(x) for x in re.findall(r'\b(\d{1,5})\b', parent)]
    if nums:
        score += sum(nums[-2:])
    return score


def _low_value(text: str, body: str = '') -> bool:
    # Only check title — body includes sidebar recommendations from other threads
    return any(k in text for k in LOW_VALUE_THREAD_KEYWORDS)


def _is_wrong_bar(main_text: str, query: str) -> bool:
    """Return True if the thread is from a different bar than the target."""
    if not main_text or not query:
        return False
    return query not in main_text


def _try_next_page(page: Page, query: str = "") -> bool:
    # Strategy 1: scroll the thread content container to load more (virtual scroll)
    try:
        scrolled = page.evaluate("""
        (() => {
            const scroller = document.querySelector('.frs-page-wrap');
            if (scroller && scroller.scrollHeight > scroller.clientHeight) {
                scroller.scrollTop = scroller.scrollHeight;
                return true;
            }
            return false;
        })()
        """)
        if scrolled:
            time.sleep(3)
            return True
    except Exception:
        pass
    # Strategy 2: traditional pagination links (for older tieba layouts)
    try:
        next_btn = page.locator('#frs-list-pager a.next, .thread_list_pager a.next, a.next_pagination').first
        if next_btn.count() > 0:
            in_sidebar = next_btn.evaluate('el => !!el.closest("aside,.aside,.sidebar,.recommend,.card_right,[class*=sidebar],[class*=aside],.left-content,.left-nav-drawer")')
            if not in_sidebar:
                next_btn.scroll_into_view_if_needed()
                time.sleep(0.5)
                box = next_btn.bounding_box()
                if box:
                    pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                next_btn.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                time.sleep(3)
                return True
    except Exception:
        pass
    if query:
        try:
            cur_pn = 0
            cur_url = page.url
            import re as _re
            m = _re.search(r'[?&]pn=(\d+)', cur_url)
            if m:
                cur_pn = int(m.group(1))
            next_pn = cur_pn + 50
            next_url = THREAD_API.format(query=quote_plus(query), pn=next_pn)
            page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            new_links = page.evaluate("""(() => {
              return document.querySelectorAll('a[href*="/p/"]').length;
            })()""")
            if new_links > 0:
                return True
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
        except Exception:
            pass
    return False


def _try_expand_all(page: Page) -> bool:
    try:
        expand = page.locator('.list-load-more').first
        if expand.count() == 0:
            return False
        expand.scroll_into_view_if_needed()
        time.sleep(0.3)
        box = expand.bounding_box()
        if box:
            pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        expand.click()
        time.sleep(3)
        pw.scroll_pages(page, min_pages=3)
        time.sleep(2)
        return True
    except Exception:
        return False


def _try_expand_replies(page: Page) -> None:
    """Click all visible expand/show-off links to reveal hidden replies."""
    try:
        expand_btns = page.locator('span.show-off, a.show-off, button.show-off')
        count = expand_btns.count()
        for i in range(min(count, 10)):
            try:
                btn = expand_btns.nth(i)
                if btn.is_visible(timeout=1000):
                    btn.scroll_into_view_if_needed()
                    time.sleep(0.2)
                    box = btn.bounding_box()
                    if box:
                        pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                    btn.click()
                    time.sleep(1)
            except Exception:
                continue
    except Exception:
        pass


def _try_thread_next_page(page: Page) -> bool:
    """Try to click next page in thread reply pagination (NOT sidebar)."""
    try:
        # Reply-area specific selectors, ordered by specificity
        for sel in [
            'div.l_pager a.p_next',
            'div.pb_footer a.next',
            'a.l_pager.p_next',
            '.p_postlist_area a.next',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    # Verify not inside sidebar
                    in_sidebar = loc.evaluate('el => !!el.closest("aside,.aside,.sidebar,.recommend,.card_right,[class*=sidebar],[class*=aside]")')
                    if in_sidebar:
                        continue
                    loc.scroll_into_view_if_needed()
                    time.sleep(0.3)
                    box = loc.bounding_box()
                    if box:
                        pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                    loc.click()
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    time.sleep(3)
                    return True
            except Exception:
                continue
        try:
            next_link = page.locator('a:has-text("下一页")').first
            if next_link.is_visible(timeout=1500):
                in_sidebar = next_link.evaluate('el => !!el.closest("aside,.aside,.sidebar,.recommend,.card_right,[class*=sidebar],[class*=aside]")')
                if in_sidebar:
                    return False
                next_link.scroll_into_view_if_needed()
                time.sleep(0.3)
                box = next_link.bounding_box()
                if box:
                    pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                next_link.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                time.sleep(3)
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _collect_page_candidates(page: Page, scroll_rounds: int = 12) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    no_new_count = 0
    for _ in range(scroll_rounds):
        before = len(seen)
        for item in _collect_visible_threads(page, limit=150):
            href = item.get('href') or ''
            if href and href not in seen:
                seen[href] = {**item, 'score': _thread_score(item)}
        _scroll_bar(page)
        time.sleep(2.5)
        if len(seen) == before:
            no_new_count += 1
            if no_new_count >= 3:
                break
        else:
            no_new_count = 0
    ranked = sorted(seen.values(), key=lambda x: x.get('score', 0), reverse=True)
    return [r for r in ranked if r.get('score', 0) > 0 and not _low_value(r.get('text', ''), r.get('parent', ''))]


def _read_detail(page: Page) -> dict[str, Any]:
    js = r"""({title: document.title, url: location.href, text: (document.body.innerText || '').slice(0, 12000)})"""
    result = pw.evaluate_json(page, js)
    if result and isinstance(result, dict):
        return result
    return {}


def sample(query: str, count: int = 5, max_attempts: int | None = None, max_pages: int = 5) -> dict[str, Any]:
    decision = scheduler.schedule(DOMAIN, "fetch")
    if not decision.get("allowed"):
        return {"ok": False, "discovery_hint": SEARXNG_DISCOVERY_HINT, "items": [], "reason": decision.get("reason"), "scheduler": decision, "checklist": {"real_bar_opened": False, "at_least_5_threads_clicked": False, "entered_real_thread_detail": False, "not_low_value_thread": False, "thread_text_captured": False}}
    if decision.get("wait_seconds", 0) > 0:
        time.sleep(decision["wait_seconds"])

    page = pw.new_page()
    try:
        items: list[dict[str, Any]] = []
        _open_bar(page, query)
        _wait_ready(page)
        time.sleep(2)
        all_candidates: list[dict[str, Any]] = []
        seen_hrefs: set[str] = set()
        pages_scanned = 0
        expanded = _try_expand_all(page)
        for page_num in range(max_pages):
            pages_scanned = page_num + 1
            page_candidates = _collect_page_candidates(page, scroll_rounds=12)
            for cand in page_candidates:
                href = cand.get('href') or ''
                if href and href not in seen_hrefs:
                    seen_hrefs.add(href)
                    all_candidates.append(cand)
            if len(all_candidates) >= count:
                break
            if not _try_next_page(page, query=query):
                break
        attempts = 0
        for cand in all_candidates:
            valid_count = sum(1 for i in items if i.get('parse_ok') and not i.get('wrong_bar'))
            if valid_count >= count:
                break
            if max_attempts is not None and attempts >= max_attempts:
                break
            attempts += 1
            href = cand.get('href') or ''
            if not href:
                continue
            _open_url(page, href)
            _wait_ready(page)
            time.sleep(5)
            # Scroll in the thread detail page - move mouse to center to avoid sidebar
            page.mouse.move(600, 400)
            pw.scroll_pages(page, min_pages=5)
            _try_expand_replies(page)
            detail = _read_detail(page)
            url = detail.get('url') or href
            title = detail.get('title') or cand.get('text')
            main_text = detail.get('text', '')
            if _is_wrong_bar(main_text, query):
                items.append({
                    'candidate': cand,
                    'detail_url': url,
                    'title': title,
                    'main_text': main_text,
                    'low_value_thread': False,
                    'parse_ok': False,
                    'wrong_bar': True,
                    'failure_reason': 'wrong_bar',
                })
                continue
            for _ in range(2):
                if not _try_thread_next_page(page):
                    break
                page.mouse.move(600, 400)
                pw.scroll_pages(page, min_pages=3)
                _try_expand_replies(page)
                more = _read_detail(page)
                if more.get('text'):
                    main_text += '\n' + more['text']
            low_value = _low_value(title, main_text)
            item = {
                'candidate': cand,
                'detail_url': url,
                'title': title,
                'main_text': main_text,
                'low_value_thread': low_value,
                'parse_ok': bool(main_text) and not low_value and '/p/' in url,
            }
            if not item['parse_ok']:
                item['failure_reason'] = 'direct_open_failed_or_low_value'
            items.append(item)
        success_items = [i for i in items if i.get('parse_ok')]
        scheduler.record_result(DOMAIN, "ok" if success_items else "no_results", blocked=False, pages_increment=pages_scanned)
        return {
            'ok': len(success_items) >= count,
            'discovery_hint': SEARXNG_DISCOVERY_HINT,
            'bar_url': BAR_URL.format(query=quote_plus(query)),
            'candidate_pool_size': len(all_candidates),
            'pages_scanned': pages_scanned,
            'items': items,
            'checklist': {
                'real_bar_opened': True,
                'at_least_5_threads_clicked': len(items) >= 5,
                'entered_real_thread_detail': len(success_items) >= 5,
                'not_low_value_thread': len(success_items) >= 5 and all(not i.get('low_value_thread') for i in success_items),
                'thread_text_captured': len(success_items) >= 5 and all(bool(i.get('main_text')) for i in success_items),
            },
        }
    finally:
        pw.close_page(page)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: tieba_playwright_fetch.py <bar-query>')
    q = sys.argv[1]
    print(json.dumps(sample(q), ensure_ascii=False, indent=2))
