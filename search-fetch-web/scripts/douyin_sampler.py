#!/usr/bin/env python3
"""Playwright sampler for Douyin search -> click video -> open comments -> close detail -> repeat.

Flow per video:
  1. On search page, click video card by href -> modal/detail opens (URL gains modal_id)
  2. Open comments by clicking visible comment affordance
  3. Extract comments
  4. Close detail and return to search page
  5. Re-navigate to search URL to refresh DOM, scroll, click next video
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import scheduler  # type: ignore
import _playwright_base as pw
from playwright.sync_api import Page

DOMAIN = "douyin.com"
DEBUG_DIR = SCRIPT_DIR.parent / ".data" / "debug"
VIDEO_ID_RE = re.compile(r"(?:/video/|modal_id=|aweme_id[\"'=:\s%]+|awemeId[\"'=:\s%]+|group_id[\"'=:\s%]+|groupId[\"'=:\s%]+|item_id[\"'=:\s%]+|itemId[\"'=:\s%]+|video_id[\"'=:\s%]+|videoId[\"'=:\s%]+)(\d{16,22})")


def scheduler_gate(action: str) -> dict[str, Any]:
    max_retries = 3
    for attempt in range(max_retries):
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


def record_scheduler_result(outcome: str, blocked: bool = False, pages_increment: int = 1) -> dict[str, Any]:
    return scheduler.record_result(DOMAIN, outcome, blocked=blocked, pages_increment=pages_increment)


def _debug_page_state(page: Page, label: str) -> dict[str, Any]:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    screenshot = DEBUG_DIR / f"douyin_{label}_{stamp}.png"
    try:
        page.screenshot(path=str(screenshot), full_page=False)
    except Exception:
        screenshot = Path("")
    try:
        body_text = page.evaluate("document.body ? document.body.innerText.slice(0, 1200) : ''") or ""
    except Exception:
        body_text = ""
    try:
        video_link_count = page.evaluate("document.querySelectorAll('a[href*=\\\"/video/\\\"]').length")
    except Exception:
        video_link_count = None
    try:
        title = page.title()
    except Exception:
        title = ""
    return {
        "url": page.url,
        "title": title,
        "video_link_count": video_link_count,
        "body_text_sample": body_text,
        "screenshot": str(screenshot) if screenshot else "",
    }


def _new_page() -> Page:
    # Close shared context first so isolated context can use the Edge profile
    pw.shutdown()
    return pw.new_isolated_page()


def _video_url(video_id: str) -> str:
    return f"https://www.douyin.com/video/{video_id}" if video_id else ""


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


def _query_terms(query: str) -> list[str]:
    terms = [part.strip() for part in query.replace('｜', ' ').replace('|', ' ').split() if part.strip()]
    if not terms and query.strip():
        terms = [query.strip()]
    if len(terms) == 1 and len(terms[0]) > 2:
        term = terms[0]
        terms = [term] + [term[i:i + 2] for i in range(len(term) - 1)]
    return terms


def _video_ids_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for match in VIDEO_ID_RE.finditer(text or ""):
        video_id = match.group(1)
        if video_id and video_id not in seen:
            seen.add(video_id)
            ids.append(video_id)
    return ids


def _format_count(value: Any) -> str:
    try:
        count = int(value)
    except Exception:
        return str(value or "")
    if count >= 10000:
        text = f"{count / 10000:.1f}".rstrip("0").rstrip(".")
        return f"{text}万"
    return str(count)


def _format_duration(value: Any) -> str:
    try:
        total = int(value)
    except Exception:
        return ""
    if total > 10000:
        total = int(round(total / 1000))
    if total <= 0:
        return ""
    minutes, seconds = divmod(total, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _format_create_time(value: Any) -> str:
    try:
        ts = int(value)
    except Exception:
        return ""
    if ts <= 0:
        return ""
    try:
        dt = datetime.fromtimestamp(ts)
    except Exception:
        return ""
    return f"{dt.year}年{dt.month}月{dt.day}日"


def _upsert_network_record(records: list[dict[str, Any]], seen: set[str], record: dict[str, Any]) -> None:
    video_id = str(record.get("id") or "")
    if not video_id:
        return
    for existing in records:
        if str(existing.get("id") or "") != video_id:
            continue
        for key, value in record.items():
            if value and not existing.get(key):
                existing[key] = value
        seen.add(video_id)
        return
    records.append(record)
    seen.add(video_id)


def _walk_json_records(obj: Any, records: list[dict[str, Any]], seen: set[str], budget: list[int]) -> None:
    if budget[0] <= 0:
        return
    budget[0] -= 1
    if isinstance(obj, dict):
        raw_id = obj.get("aweme_id") or obj.get("awemeId") or obj.get("group_id") or obj.get("groupId") or obj.get("item_id") or obj.get("itemId") or obj.get("video_id") or obj.get("videoId") or obj.get("id")
        video_id = str(raw_id or "")
        if video_id.isdigit() and 16 <= len(video_id) <= 22:
            author_obj = obj.get("author") if isinstance(obj.get("author"), dict) else {}
            stats_obj = obj.get("statistics") if isinstance(obj.get("statistics"), dict) else {}
            video_obj = obj.get("video") if isinstance(obj.get("video"), dict) else {}
            share_obj = obj.get("share_info") if isinstance(obj.get("share_info"), dict) else {}
            title = str(obj.get("desc") or obj.get("title") or obj.get("caption") or obj.get("text") or "")
            if not title:
                title = str(share_obj.get("share_title") or share_obj.get("share_desc") or "")
            author = str((author_obj or {}).get("nickname") or (author_obj or {}).get("unique_id") or obj.get("nickname") or "")
            _upsert_network_record(records, seen, {
                "id": video_id,
                "href": _video_url(video_id),
                "title": title,
                "author": author,
                "interaction": _format_count(stats_obj.get("digg_count") or stats_obj.get("diggCount") or ""),
                "duration": _format_duration(video_obj.get("duration") or obj.get("duration") or ""),
                "date": _format_create_time(obj.get("create_time") or obj.get("createTime") or ""),
            })
        for value in obj.values():
            _walk_json_records(value, records, seen, budget)
    elif isinstance(obj, list):
        for value in obj:
            _walk_json_records(value, records, seen, budget)


def _attach_network_link_collector(page: Page) -> dict[str, Any]:
    store: dict[str, Any] = {
        "records": [],
        "seen": set(),
        "responses": 0,
        "json_responses": 0,
        "regex_ids": 0,
        "errors": [],
    }

    def _handle_response(response: Any) -> None:
        try:
            url = response.url
            if "douyin.com" not in url and "douyinpic.com" not in url:
                return
            if not any(token in url for token in ("aweme", "search", "general", "video", "stream", "feed")):
                return
            headers = response.headers or {}
            content_type = str(headers.get("content-type", "")).lower()
            if "json" not in content_type and "text" not in content_type and "javascript" not in content_type:
                return
            text = response.text()
            if not text:
                return
            store["responses"] += 1
            records: list[dict[str, Any]] = store["records"]
            seen: set[str] = store["seen"]
            try:
                payload = json.loads(text)
                before = len(records)
                _walk_json_records(payload, records, seen, [2500])
                if len(records) > before:
                    store["json_responses"] += 1
            except Exception:
                pass
            for video_id in _video_ids_from_text(text):
                if video_id in seen:
                    continue
                _upsert_network_record(records, seen, {"id": video_id, "href": _video_url(video_id), "title": "", "author": ""})
                store["regex_ids"] += 1
        except Exception as exc:
            errors = store["errors"]
            if isinstance(errors, list) and len(errors) < 5:
                errors.append(f"{type(exc).__name__}:{str(exc)[:120]}")

    page.on("response", _handle_response)
    return store


# ---------------------------------------------------------------------------
def _alert_captcha(page) -> None:
    """弹窗+提示音+桌面通知，提醒用户处理验证码。"""
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass
    try:
        from subprocess import Popen
        Popen(['powershell', '-Command',
               "[Windows.UI.Notifications.ToastNotificationManager, "
               "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
               "$t=[Windows.UI.Notifications.ToastNotification,"
               "Windows.UI.Notifications,ContentType=WindowsRuntime];"
               "$xml=[Windows.UI.Notifications.ToastNotificationManager]"
               "::GetTemplateContent(0);"
               "$xml.GetElementsByTagName('text')[0].AppendChild("
               "$xml.CreateTextNode('抖音验证码 - 请在Edge窗口中手动完成验证')) | Out-Null;"
               "[Windows.UI.Notifications.ToastNotificationManager]"
               "::CreateToastNotifier('Claude Code').Show($t::new($xml))"],
              creationflags=0x08000000)
    except Exception:
        pass
    print("[douyin] ⚠️  检测到验证码！请在 Edge 窗口中手动完成验证（等待最多 10 分钟）",
          file=sys.stderr, flush=True)
    print("[CAPTCHA] 请在 Edge 窗口中手动完成抖音验证码", flush=True)


# Search page helpers
# ---------------------------------------------------------------------------

def _open_search_page(page: Page, query: str) -> str:
    search_url = f"https://www.douyin.com/search/{quote_plus(query)}?type=video"

    # 先访问首页建立真人访问痕迹（cookie + referrer），再导航到搜索页
    # 直接跳搜索页会触发风控验证码
    page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=30000)
    time.sleep(2 + random.random() * 2)

    if pw._detect_captcha(page):
        _alert_captcha(page)
        captcha = pw.wait_for_captcha_or_proceed(page, wait_seconds=600.0)
        if captcha.get("blocked"):
            return ""
        print("[douyin] 验证码已通过", file=sys.stderr)

    # 导航到搜索页 — 此时有首页 referrer，不易触发风控
    search_selectors = [
        'input[data-e2e="search-input"]',
        'input[placeholder*="搜索"]',
        'input[type="search"]',
        'input',
    ]
    search_input = None
    for selector in search_selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible(timeout=2000):
                search_input = loc
                break
        except Exception:
            continue
    if search_input is None:
        return ""

    try:
        search_input.click()
        time.sleep(0.5 + random.random() * 0.8)
        search_input.fill("")
        pw.human_type(page, query, min_delay=80, max_delay=180)
        time.sleep(0.5 + random.random() * 1.0)
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        try:
            button = page.locator('button:has-text("搜索"), [data-e2e*="search"]').first
            button.click(timeout=3000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            return ""

    time.sleep(4 + random.random() * 3)

    # 搜索页验证码检测 — 给用户充足时间手动处理
    if pw._detect_captcha(page):
        _alert_captcha(page)

    captcha = pw.wait_for_captcha_or_proceed(page, wait_seconds=300.0)
    if captcha.get("blocked"):
        print("[douyin] ⚠️  验证码未在 10 分钟内解决，放弃本次搜索", file=sys.stderr)
        return ""
    if captcha.get("solved"):
        print("[douyin] 验证码已手动通过，重新导航到搜索页...", file=sys.stderr)
        time.sleep(3)
        # 验证码通过后页面通常会跳转到首页，必须重新导航到搜索页
        try:
            search_input = page.locator('input[data-e2e="search-input"], input[placeholder*="搜索"], input[type="search"], input').first
            search_input.click(timeout=5000)
            search_input.fill("")
            pw.human_type(page, query, min_delay=80, max_delay=180)
            page.keyboard.press("Enter")
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            time.sleep(6 + random.random() * 4)
        except Exception:
            return ""

    # 确认 page 仍然存活
    try:
        page.evaluate("1+1")
    except Exception:
        return ""

    # Do not blindly scroll the page after submitting search. If the submit
    # lands in a feed-like page, fixed multi-page scrolling looks stuck and
    # can waste the run before candidate extraction. Wait for video anchors
    # first, then use at most two small scroll probes.
    for attempt in range(6):
        if _collect_candidates(page, query, limit=1):
            break
        if attempt in (2, 4):
            pw.scroll_pages(page, min_pages=1, pause_min=0.5, pause_max=1.0)
        time.sleep(1.0)
    _ensure_video_tab(page, query)
    return search_url


def _ensure_video_tab(page: Page, query: str) -> bool:
    """After human-like homepage search, switch the result page to the video tab once."""
    try:
        if "type=video" in page.url or "source=video" in page.url:
            return True
    except Exception:
        return False
    try:
        marked = page.evaluate("""() => {
          const visible = (el) => {
            const r = el.getBoundingClientRect && el.getBoundingClientRect();
            if (!r || r.width < 8 || r.height < 8) return false;
            if (r.bottom < 0 || r.top > window.innerHeight) return false;
            const style = getComputedStyle(el);
            return style.visibility !== 'hidden' && style.display !== 'none';
          };
          const candidates = Array.from(document.querySelectorAll('a, button, [role="tab"], [data-e2e], div, span'))
            .filter(el => visible(el) && (el.innerText || el.textContent || '').trim() === '视频');
          candidates.sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (ar.top - br.top) || (ar.left - br.left);
          });
          for (const el of candidates.slice(0, 8)) {
            const target = el.closest('a, button, [role="tab"]') || el;
            const r = target.getBoundingClientRect();
            if (r.top > 0 && r.top < Math.max(260, window.innerHeight * 0.45)) {
              target.setAttribute('data-search-fetch-video-tab', '1');
              return true;
            }
          }
          const href = Array.from(document.querySelectorAll('a[href*="type=video"], a[href*="source=video"]'))
            .find(el => visible(el));
          if (href) {
            href.setAttribute('data-search-fetch-video-tab', '1');
            return true;
          }
          return false;
        }""")
    except Exception:
        marked = False
    if not marked:
        return False
    try:
        loc = page.locator('[data-search-fetch-video-tab="1"]').first
        box = loc.bounding_box(timeout=1500)
        if box:
            pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2), steps=3)
        loc.click(timeout=3000)
        page.wait_for_load_state("domcontentloaded", timeout=12000)
        time.sleep(3 + random.random() * 2)
        if pw._detect_captcha(page):
            return False
        for attempt in range(6):
            if _collect_candidates(page, query, limit=1):
                return True
            if attempt in (2, 4):
                pw.scroll_pages(page, min_pages=1, pause_min=0.5, pause_max=1.0)
            time.sleep(0.8)
    except Exception:
        return False
    return "type=video" in page.url


def _collect_candidates(page: Page, query: str, limit: int = 10) -> list[dict[str, Any]]:
    query_terms = [part.strip() for part in query.replace('｜', ' ').replace('|', ' ').split() if part.strip()]
    if not query_terms:
        query_terms = [query.strip()]
    if len(query_terms) == 1 and len(query_terms[0]) > 2:
        t = query_terms[0]
        bigrams = [t[i:i+2] for i in range(len(t) - 1)]
        query_terms = [t] + bigrams
    terms_json = json.dumps(query_terms, ensure_ascii=False)
    js = f'''
    (() => {{
      const terms = {terms_json};
      const blacklist = ['相关搜索', '大家都在搜', '搜索历史', '猜你想搜'];
      function isBlacklisted(text) {{
        return blacklist.some(b => text.includes(b));
      }}
      function normalizeHref(href) {{
        if (!href) return '';
        if (href.startsWith('//')) return location.protocol + href;
        if (href.startsWith('/')) return location.origin + href;
        return href;
      }}
      function findHref(el) {{
        const direct = el.closest && el.closest('a[href]');
        if (direct) {{
          const val = direct.getAttribute('href') || direct.href || '';
          if (val.includes('/video/') || val.includes('modal_id=')) return normalizeHref(val);
        }}
        const child = el.querySelector && el.querySelector('a[href*="/video/"], a[href*="modal_id="]');
        if (child) return normalizeHref(child.getAttribute('href') || child.href || '');
        function videoUrlFromValue(val) {{
          val = String(val || '');
          const direct = val.match(/(?:\\/video\\/|modal_id=)(\\d{{16,22}})/);
          if (direct) return 'https://www.douyin.com/video/' + direct[1];
          const named = val.match(/(?:aweme_id|awemeId|group_id|groupId|item_id|itemId|video_id|videoId|modal_id)["'=:\\s%]+(\\d{{16,22}})/);
          if (named) return 'https://www.douyin.com/video/' + named[1];
          return '';
        }}
        const hrefLike = el.querySelector && el.querySelector('[href],[data-href],[data-url],[data-share-url]');
        if (hrefLike) {{
          for (const name of ['href', 'data-href', 'data-url', 'data-share-url']) {{
            const val = hrefLike.getAttribute(name) || '';
            if (val.includes('douyin.com') || val.includes('/video/') || val.includes('modal_id=')) return normalizeHref(val);
            const fromVal = videoUrlFromValue(val);
            if (fromVal) return fromVal;
          }}
        }}
        const attrNodes = [el, ...(el.querySelectorAll ? Array.from(el.querySelectorAll('*')).slice(0, 80) : [])];
        for (const node of attrNodes) {{
          for (const attr of Array.from(node.attributes || [])) {{
            const val = attr.value || '';
            if (val.includes('douyin.com/video/') || val.includes('/video/') || val.includes('modal_id=')) return normalizeHref(val);
            const fromVal = videoUrlFromValue(val);
            if (fromVal) return fromVal;
          }}
        }}
        return '';
      }}
      const pageLooksSearch = location.href.includes('/search/') || document.body.innerText.includes(terms[0] || '');
      const links = Array.from(document.querySelectorAll('a[href*="/video/"]'));
      const seen = new Set();
      const results = [];
      let idx = 0;
      for (const a of links) {{
        const href = a.href || '';
        if (!href || seen.has(href)) continue;
        seen.add(href);
        const card = a.closest('[data-e2e], li, div') || a;
        const text = ((a.innerText || card.innerText || a.getAttribute('aria-label') || a.title || '')).trim();
        if (isBlacklisted(text)) continue;
        const matched = text && terms.some(term => term && text.includes(term));
        if (!matched) continue;
        const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
        const matchedLine = lines.find(line => terms.some(term => term && line.includes(term)));
        const title = matchedLine || lines[1] || lines[0] || href.split('/video/').pop().split('?')[0] || '';
        const cardIndex = String(idx++);
        card.setAttribute('data-search-fetch-card-index', cardIndex);
        results.push({{ title, text: text.slice(0, 1200), href, card_index: cardIndex }});
        if (results.length >= {limit}) break;
      }}
      if (results.length < {limit}) {{
        const blocks = Array.from(document.querySelectorAll('div, li, section, article'));
        for (const el of blocks) {{
          const text = (el.innerText || '').trim();
          if (!text || text.length < 12 || text.length > 900) continue;
          if (isBlacklisted(text)) continue;
          const hasMedia = !!el.querySelector('img, video, canvas');
          const hasDuration = /\\b\\d{{1,2}}:\\d{{2}}\\b/.test(text);
          const hrefEl = el.querySelector('a[href*="/video/"]');
          const href = hrefEl ? normalizeHref(hrefEl.getAttribute('href') || hrefEl.href) : findHref(el);
          const matched = terms.some(term => term && text.includes(term));
          if (!matched) continue;
          if (!href && !hasDuration) continue;
          if (!hasMedia && !hasDuration && !href) continue;
          const key = href || text.slice(0, 80);
          if (seen.has(key)) continue;
          seen.add(key);
          const cardIndex = String(idx++);
          el.setAttribute('data-search-fetch-card-index', cardIndex);
          const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
          const matchedLine = lines.find(line => terms.some(term => term && line.includes(term)));
          const title = matchedLine || lines.find(line => line.length >= 8) || key;
          results.push({{ title, text: text.slice(0, 1200), href, card_index: cardIndex }});
          if (results.length >= {limit}) break;
        }}
      }}
      return results;
    }})()
    '''
    return pw.evaluate_json_list(page, js)


def _parse_condensed_card_text(text: str) -> dict[str, str]:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return {}
    match = re.match(r"^(?P<duration>\d{1,2}:\d{2})(?P<interaction>(?:\d+(?:\.\d+)?万|\d{1,4}))(?P<rest>.+)$", value)
    if not match:
        return {}
    rest = match.group("rest").strip()
    author_match = re.search(r"(?P<author>@[^@·]{1,80})(?:\s*·\s*(?P<date>.+))?$", rest)
    if not author_match:
        return {}
    title = rest[:author_match.start()].strip()
    author = author_match.group("author").strip()
    if not title or not author:
        return {}
    return {
        "duration": match.group("duration").strip(),
        "interaction": match.group("interaction").strip(),
        "title": title,
        "author": author,
        "date": (author_match.group("date") or "").strip(),
    }


def _normalize_card(candidate: dict[str, Any]) -> dict[str, Any]:
    raw_text = candidate.get("text") or ""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    duration = ""
    interaction = ""
    title = candidate.get("title") or ""
    author = ""
    date = ""
    if lines and re.fullmatch(r"\d{1,2}:\d{2}", lines[0]):
        duration = lines[0]
    if len(lines) > 1 and not lines[1].startswith("@"):
        interaction = lines[1]
    if len(lines) > 2:
        title = title or lines[2]
    for idx, line in enumerate(lines):
        if line.startswith("@"):
            author = line
            if idx + 1 < len(lines):
                date = lines[idx + 1].lstrip("·").strip()
            break
    condensed = _parse_condensed_card_text(raw_text or title)
    if condensed and (not author or not duration or not re.fullmatch(r"\d{1,2}:\d{2}", duration)):
        duration = condensed.get("duration", duration)
        interaction = condensed.get("interaction", interaction)
        title = condensed.get("title", title)
        author = condensed.get("author", author)
        date = condensed.get("date", date)
    return {
        "title": title,
        "author": author,
        "date": date,
        "interaction": interaction,
        "duration": duration,
        "text": candidate.get("text") or "",
        "href": candidate.get("href") or "",
        "url": candidate.get("href") or "",
        "card_index": candidate.get("card_index"),
    }


def _card_key(item: dict[str, Any]) -> str:
    href = item.get("href") or ""
    if href:
        return f"href:{href.split('?')[0]}"
    title = (item.get("title") or "").strip()
    author = (item.get("author") or "").strip()
    duration = (item.get("duration") or "").strip()
    text = (item.get("text") or "").strip()
    if title or author or duration:
        return f"card:{title}|{author}|{duration}"
    return f"text:{text[:120]}"


def _dedupe_items_by_key(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = _card_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _network_record_matches_query(record: dict[str, Any], query: str) -> bool:
    title = str(record.get("title") or "")
    if not title:
        return False
    compact_title = _compact_text(title)
    terms = [_compact_text(term) for term in _query_terms(query) if _compact_text(term)]
    if not terms:
        return True
    if terms[0] and terms[0] in compact_title:
        return True
    return any(len(term) >= 2 and term in compact_title for term in terms[1:])


def _network_records_to_items(network_store: dict[str, Any] | None, query: str, seen_keys: set[str], limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta = {
        "available": 0,
        "eligible": 0,
        "added": 0,
        "skipped_no_title": 0,
        "skipped_unmatched": 0,
        "skipped_duplicate": 0,
    }
    if not network_store or limit <= 0:
        return [], meta
    added: list[dict[str, Any]] = []
    records = list(network_store.get("records") or [])
    meta["available"] = len(records)
    for record in records:
        title = str(record.get("title") or "").strip()
        href = str(record.get("href") or "").strip()
        if not title:
            meta["skipped_no_title"] += 1
            continue
        if not _network_record_matches_query(record, query):
            meta["skipped_unmatched"] += 1
            continue
        item = {
            "title": title,
            "author": f"@{record.get('author')}" if record.get("author") and not str(record.get("author")).startswith("@") else str(record.get("author") or ""),
            "date": str(record.get("date") or ""),
            "interaction": str(record.get("interaction") or ""),
            "duration": str(record.get("duration") or ""),
            "text": title,
            "href": href,
            "url": href,
            "card_index": "",
            "source": "network",
        }
        meta["eligible"] += 1
        key = _card_key(item)
        if not key or key in seen_keys:
            meta["skipped_duplicate"] += 1
            continue
        seen_keys.add(key)
        added.append(item)
        if len(added) >= limit:
            break
    meta["added"] = len(added)
    return added, meta


def _scroll_search_results(page: Page) -> dict[str, Any]:
    before = page.evaluate("""(() => {
      const candidates = [document.scrollingElement, document.documentElement, document.body, ...document.querySelectorAll('main, section, div')].filter(Boolean);
      const cardNodes = Array.from(document.querySelectorAll('[data-search-fetch-card-index], a[href*="/video/"]'))
        .map(el => el.closest('[data-search-fetch-card-index], [data-e2e], li, section, article, div') || el)
        .filter(Boolean);
      const visibleCards = cardNodes.filter(el => {
        const r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
        return r && r.width > 80 && r.height > 80 && r.bottom > 0 && r.top < window.innerHeight + 400;
      });
      const lastCard = visibleCards[visibleCards.length - 1] || cardNodes[cardNodes.length - 1] || null;
      if (lastCard) lastCard.setAttribute('data-search-fetch-scroll-anchor', '1');
      const visible = candidates.map((el, idx) => {
        const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {left: 0, top: 0, width: window.innerWidth, height: window.innerHeight};
        const scrollHeight = el.scrollHeight || 0;
        const clientHeight = el.clientHeight || 0;
        const scrollTop = el.scrollTop || 0;
        const canScroll = scrollHeight > clientHeight + 80;
        const visibleRect = r.width > 260 && r.height > 180 && r.bottom > 80 && r.top < window.innerHeight - 80;
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
          tag: el.tagName || '',
          cardCount: cardNodes.length
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
        tag: 'WINDOW',
        cardCount: cardNodes.length
      };
    })()""")
    anchor_box = None
    try:
        anchor = page.locator('[data-search-fetch-scroll-anchor="1"]').last
        if anchor.count() > 0:
            anchor_box = anchor.bounding_box(timeout=1000)
    except Exception:
        anchor_box = None
    x = int(max(20, min((before.get("left", 0) or 0) + (before.get("width", 0) or 0) * 0.55, page.viewport_size["width"] - 20 if page.viewport_size else 1200)))
    y = int(max(90, min((before.get("top", 0) or 0) + (before.get("height", 0) or 0) * 0.62, page.viewport_size["height"] - 40 if page.viewport_size else 760)))
    if anchor_box:
        x = int(max(20, min(anchor_box["x"] + anchor_box["width"] * 0.5, page.viewport_size["width"] - 20 if page.viewport_size else 1200)))
        y = int(max(90, min(anchor_box["y"] + anchor_box["height"] * 0.85, page.viewport_size["height"] - 40 if page.viewport_size else 760)))
    try:
        pw.human_mouse_move(page, x, y)
    except Exception:
        page.mouse.move(x, y)
    try:
        page.evaluate("""() => {
          document.querySelectorAll('[data-search-fetch-scroll-container="1"]').forEach(el => el.removeAttribute('data-search-fetch-scroll-container'));
          const cards = Array.from(document.querySelectorAll('[data-search-fetch-card-index], a[href*="/video/"]'))
            .map(el => el.closest('[data-search-fetch-card-index], [data-e2e], li, section, article, div') || el)
            .filter(Boolean);
          const anchor = document.querySelector('[data-search-fetch-scroll-anchor="1"]') || cards[cards.length - 1];
          const scrollables = [document.scrollingElement, document.documentElement, document.body, ...document.querySelectorAll('main, section, div')]
            .filter(Boolean)
            .filter(el => (el.scrollHeight || 0) > (el.clientHeight || 0) + 80)
            .map(el => {
              const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight, width: window.innerWidth, height: window.innerHeight};
              const visible = r.width > 220 && r.height > 120 && r.bottom > 40 && r.top < window.innerHeight - 40;
              const contained = cards.filter(card => {
                try { return el === document.scrollingElement || el === document.documentElement || el === document.body || el.contains(card); } catch (e) { return false; }
              }).length;
              const hasAnchor = !!(anchor && (el === document.scrollingElement || el === document.documentElement || el === document.body || el.contains(anchor)));
              return {el, visible, contained, hasAnchor, area: Math.max(0, Math.min(r.right, window.innerWidth) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0)), room: (el.scrollHeight || 0) - (el.clientHeight || 0) - (el.scrollTop || 0)};
            })
            .filter(x => x.visible && x.room > 40);
          scrollables.sort((a, b) => (Number(b.hasAnchor) - Number(a.hasAnchor)) || (b.contained - a.contained) || (b.area - a.area) || (b.room - a.room));
          const target = scrollables[0] ? scrollables[0].el : null;
          if (target) {
            target.setAttribute('data-search-fetch-scroll-container', '1');
            const delta = Math.max(520, Math.floor((target.clientHeight || window.innerHeight || 700) * 0.92));
            target.scrollTop = (target.scrollTop || 0) + delta;
          } else if (anchor && anchor.scrollIntoView) {
            anchor.scrollIntoView({block: 'end', inline: 'nearest', behavior: 'instant'});
          }
        }""")
        page.wait_for_timeout(int(250 + random.random() * 250))
    except Exception:
        pass
    ticks = random.randint(7, 11)
    total_delta = int((before.get("clientHeight", 0) or 700) * random.uniform(1.0, 1.55))
    for _ in range(ticks):
        delta = max(80, int((total_delta / ticks) * random.uniform(0.75, 1.35)))
        page.mouse.wheel(0, delta)
        time.sleep(random.uniform(0.06, 0.18))
    page.wait_for_timeout(int(1100 + random.random() * 1000))
    after = page.evaluate("""(() => ({
      y: Math.max(window.scrollY || 0, document.documentElement.scrollTop || 0, document.body.scrollTop || 0),
      h: Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0),
      activeScrollTop: (() => {
        const marked = document.querySelector('[data-search-fetch-scroll-container="1"]');
        if (marked) return marked.scrollTop || 0;
        const els = Array.from(document.querySelectorAll('main, section, div')).filter(el => el.scrollHeight > el.clientHeight + 80);
        els.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
        return els[0] ? els[0].scrollTop : 0;
      })(),
      cardCount: document.querySelectorAll('[data-search-fetch-card-index], a[href*="/video/"]').length
    }))()""")
    moved = bool(after and before and (
        after.get("y", 0) > before.get("scrollTop", before.get("y", 0)) + 20
        or after.get("h", 0) > before.get("scrollHeight", before.get("h", 0)) + 20
        or after.get("activeScrollTop", 0) > before.get("scrollTop", 0) + 20
        or after.get("cardCount", 0) > before.get("cardCount", 0)
    ))
    if not moved:
        try:
            page.evaluate("""() => {
              const delta = Math.max(700, Math.floor(window.innerHeight * 1.25));
              const marked = document.querySelector('[data-search-fetch-scroll-container="1"]');
              const els = [marked, document.scrollingElement, document.documentElement, document.body, ...document.querySelectorAll('main, section, div')]
                .filter(Boolean)
                .filter(el => (el.scrollHeight || 0) > (el.clientHeight || 0) + 80);
              for (const el of els.slice(0, 4)) {
                try { el.scrollTop = (el.scrollTop || 0) + delta; } catch (e) {}
              }
              window.scrollBy(0, delta);
            }""")
            page.wait_for_timeout(int(1200 + random.random() * 1000))
            after = page.evaluate("""(() => ({
              y: Math.max(window.scrollY || 0, document.documentElement.scrollTop || 0, document.body.scrollTop || 0),
              h: Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0),
              activeScrollTop: (() => {
                const marked = document.querySelector('[data-search-fetch-scroll-container="1"]');
                if (marked) return marked.scrollTop || 0;
                const els = Array.from(document.querySelectorAll('main, section, div')).filter(el => el.scrollHeight > el.clientHeight + 80);
                els.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                return els[0] ? els[0].scrollTop : 0;
              })(),
              cardCount: document.querySelectorAll('[data-search-fetch-card-index], a[href*="/video/"]').length
            }))()""")
        except Exception:
            pass
    moved = bool(after and before and (
        after.get("y", 0) > before.get("scrollTop", before.get("y", 0)) + 20
        or after.get("h", 0) > before.get("scrollHeight", before.get("h", 0)) + 20
        or after.get("activeScrollTop", 0) > before.get("scrollTop", 0) + 20
        or after.get("cardCount", 0) > before.get("cardCount", 0)
    ))
    return {"before": before, "after": after, "x": x, "y": y, "moved": moved}


def _douyin_end_marker(page: Page) -> str:
    try:
        text = page.evaluate("document.body ? document.body.innerText.slice(-5000) : ''") or ""
    except Exception:
        return ""
    for marker in ("没有更多", "暂无更多", "已加载全部", "已经到底", "到底了", "没有找到相关结果"):
        if marker in text:
            return marker
    return ""


def _resolve_candidate_url_light(page: Page, candidate: dict[str, Any]) -> str:
    href = candidate.get("href") or ""
    if href and ("modal_id=" in href or "/video/" in href):
        return href
    before_url = page.url
    card_index = candidate.get("card_index")
    if card_index is None:
        return ""
    try:
        loc = page.locator(f'[data-search-fetch-card-index="{card_index}"]').first
        loc.scroll_into_view_if_needed(timeout=2500)
        page.wait_for_timeout(200)
        box = loc.bounding_box()
        if box:
            target_x = int(box["x"] + box["width"] / 2)
            target_y = int(box["y"] + min(box["height"] * 0.45, box["height"] - 18))
            pw.human_mouse_move(page, target_x, target_y, steps=3)
            page.mouse.click(target_x + random.randint(-3, 3), target_y + random.randint(-3, 3))
        else:
            loc.click(timeout=2500)
        try:
            page.wait_for_function(
                """(before) => location.href !== before && (location.href.includes('modal_id=') || location.href.includes('/video/'))""",
                arg=before_url,
                timeout=1800,
            )
        except Exception:
            page.wait_for_timeout(350)
        resolved = page.url if ("modal_id=" in page.url or "/video/" in page.url) else ""
    except Exception:
        return ""
    for _ in range(2):
        if "modal_id=" not in page.url and "/video/" not in page.url:
            break
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(350)
        except Exception:
            pass
    if "modal_id=" in page.url or "/video/" in page.url:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=5000)
            page.wait_for_timeout(450)
        except Exception:
            pass
    if not resolved or resolved == before_url:
        return ""
    return resolved


def _resolve_candidate_url_quiet(page: Page, candidate: dict[str, Any]) -> str:
    href = candidate.get("href") or ""
    if href and ("modal_id=" in href or "/video/" in href):
        return href
    card_index = candidate.get("card_index")
    if card_index is None:
        return ""
    try:
        return str(page.evaluate(
            """async (cardIndex) => {
              const el = document.querySelector(`[data-search-fetch-card-index="${cardIndex}"]`);
              if (!el) return '';
              const link = el.closest('a[href]') || el.querySelector('a[href]');
              const target = link || el;
              let captured = '';
              const oldPush = history.pushState;
              const oldReplace = history.replaceState;
              const oldOpen = window.open;
              function normalize(raw) {
                if (!raw) return '';
                const url = new URL(String(raw), location.href).href;
                if (url.includes('modal_id=') || url.includes('/video/')) return url;
                return '';
              }
              history.pushState = function(state, title, url) {
                captured = captured || normalize(url);
                return undefined;
              };
              history.replaceState = function(state, title, url) {
                captured = captured || normalize(url);
                return undefined;
              };
              window.open = function(url) {
                captured = captured || normalize(url);
                return null;
              };
              const evOpts = {bubbles: true, cancelable: true, view: window};
              try {
                target.dispatchEvent(new MouseEvent('mouseover', evOpts));
                target.dispatchEvent(new MouseEvent('mousedown', evOpts));
                target.dispatchEvent(new MouseEvent('mouseup', evOpts));
                target.dispatchEvent(new MouseEvent('click', evOpts));
                await new Promise(resolve => setTimeout(resolve, 450));
              } catch (e) {
              } finally {
                history.pushState = oldPush;
                history.replaceState = oldReplace;
                window.open = oldOpen;
              }
              if (captured) return captured;
              const attrs = [target, el, ...(el.querySelectorAll ? Array.from(el.querySelectorAll('*')).slice(0, 80) : [])];
              for (const node of attrs) {
                for (const attr of Array.from(node.attributes || [])) {
                  const value = String(attr.value || '');
                  const direct = value.match(/(?:\\/video\\/|modal_id=)(\\d{16,22})/);
                  if (direct) return 'https://www.douyin.com/video/' + direct[1];
                  const named = value.match(/(?:aweme_id|awemeId|group_id|groupId|item_id|itemId|video_id|videoId|modal_id)["'=:\\s%]+(\\d{16,22})/);
                  if (named) return 'https://www.douyin.com/video/' + named[1];
                }
              }
              return '';
            }""",
            str(card_index),
        ) or "")
    except Exception:
        return ""


def _show_link_resolve_overlay(page: Page) -> None:
    try:
        page.evaluate("""() => {
          let el = document.getElementById('search-fetch-link-resolve-overlay');
          if (!el) {
            el = document.createElement('div');
            el.id = 'search-fetch-link-resolve-overlay';
            document.documentElement.appendChild(el);
          }
          Object.assign(el.style, {
            position: 'fixed',
            inset: '0',
            zIndex: '2147483647',
            background: 'rgba(18, 18, 18, 0.86)',
            color: '#fff',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            font: '16px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif',
            letterSpacing: '0',
            pointerEvents: 'none'
          });
          el.textContent = '正在补齐抖音视频链接...';
        }""")
    except Exception:
        pass


def _hide_link_resolve_overlay(page: Page) -> None:
    try:
        page.evaluate("""() => {
          const el = document.getElementById('search-fetch-link-resolve-overlay');
          if (el) el.remove();
        }""")
    except Exception:
        pass


def _network_href_for_item(item: dict[str, Any], network_store: dict[str, Any] | None, used_ids: set[str]) -> str:
    if not network_store:
        return ""
    item_title = _compact_text(item.get("title") or "")
    item_text = _compact_text(item.get("text") or "")
    item_author = _compact_text(str(item.get("author") or "").lstrip("@"))
    for record in list(network_store.get("records") or []):
        video_id = str(record.get("id") or "")
        if not video_id or video_id in used_ids:
            continue
        record_title = _compact_text(record.get("title") or "")
        record_author = _compact_text(str(record.get("author") or "").lstrip("@"))
        title_match = bool(
            item_title and record_title and (
                item_title in record_title
                or record_title in item_title
                or item_title[:24] in record_title
                or record_title[:24] in item_text
            )
        )
        author_match = bool(item_author and record_author and (item_author in record_author or record_author in item_author))
        if title_match and (not item_author or not record_author or author_match):
            used_ids.add(video_id)
            return str(record.get("href") or _video_url(video_id))
    return ""


def _dom_href_for_candidate(page: Page, candidate: dict[str, Any]) -> str:
    card_index = candidate.get("card_index")
    if card_index is None:
        return ""
    try:
        return str(page.evaluate(
            """(cardIndex) => {
              const root = document.querySelector(`[data-search-fetch-card-index="${cardIndex}"]`);
              if (!root) return '';
              function makeUrl(id) { return id ? 'https://www.douyin.com/video/' + id : ''; }
              function scanValue(value) {
                value = String(value || '');
                const direct = value.match(/(?:\\/video\\/|modal_id=)(\\d{16,22})/);
                if (direct) return makeUrl(direct[1]);
                const named = value.match(/(?:aweme_id|awemeId|group_id|groupId|item_id|itemId|video_id|videoId|modal_id)["'=:\\s%]+(\\d{16,22})/);
                if (named) return makeUrl(named[1]);
                return '';
              }
              const nodes = [root, ...(root.querySelectorAll ? Array.from(root.querySelectorAll('*')).slice(0, 240) : [])];
              for (const node of nodes) {
                if (node.href) {
                  const href = String(node.href || '');
                  if (href.includes('/video/') || href.includes('modal_id=')) return href;
                }
                for (const attr of Array.from(node.attributes || [])) {
                  const found = scanValue(attr.value);
                  if (found) return found;
                }
                for (const key of Object.keys(node)) {
                  if (!key.startsWith('__reactProps') && !key.startsWith('__reactFiber')) continue;
                  try {
                    const seen = new Set();
                    const stack = [node[key]];
                    let budget = 500;
                    while (stack.length && budget-- > 0) {
                      const cur = stack.pop();
                      if (!cur || seen.has(cur)) continue;
                      if (typeof cur === 'string' || typeof cur === 'number') {
                        const found = scanValue(cur);
                        if (found) return found;
                        continue;
                      }
                      if (typeof cur !== 'object') continue;
                      seen.add(cur);
                      for (const [k, v] of Object.entries(cur)) {
                        if (/aweme|group|item|video|modal/i.test(k)) {
                          const found = scanValue(String(v));
                          if (found) return found;
                        }
                        if (v && (typeof v === 'object' || typeof v === 'string' || typeof v === 'number')) stack.push(v);
                      }
                    }
                  } catch (e) {}
                }
              }
              return '';
            }""",
            str(card_index),
        ) or "")
    except Exception:
        return ""


def _resolve_missing_links(page: Page, items: list[dict[str, Any]], query: str, network_store: dict[str, Any] | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "enabled": True,
        "attempted": 0,
        "resolved": sum(1 for item in items if item.get("href")),
        "unresolved": 0,
        "network_records": len(network_store.get("records") or []) if network_store else 0,
        "network_responses": int(network_store.get("responses", 0) or 0) if network_store else 0,
        "network_resolved": 0,
        "network_sample": [
            {"id": rec.get("id"), "title": str(rec.get("title") or "")[:80], "author": rec.get("author")}
            for rec in (list(network_store.get("records") or [])[:5] if network_store else [])
        ],
        "clicked": 0,
        "quiet": 0,
        "dom_resolved": 0,
        "react_resolved": 0,
        "visible_click_fallback": 0,
        "overlay_used": False,
        "errors": [],
    }
    if not items:
        return meta

    try:
        fresh_candidates = _collect_candidates(page, query, limit=max(len(items) * 2, len(items) + 20))
    except Exception:
        fresh_candidates = []
    fresh_by_key: dict[str, dict[str, Any]] = {}
    for candidate in fresh_candidates:
        fresh_item = _normalize_card(candidate)
        key = _card_key(fresh_item)
        if key and key not in fresh_by_key:
            fresh_by_key[key] = candidate

    used_network_ids: set[str] = set()
    for item in items:
        if item.get("href"):
            continue
        href = _network_href_for_item(item, network_store, used_ids=used_network_ids)
        if href:
            item["href"] = href
            item["url"] = href
            meta["network_resolved"] += 1
            meta["resolved"] += 1
            continue
        key = _card_key(item)
        candidate = fresh_by_key.get(key) or item.get("_resolve_candidate") or {}
        href = candidate.get("href") or ""
        if not href:
            href = _dom_href_for_candidate(page, candidate)
        if href:
            item["href"] = href
            item["url"] = href
            meta["dom_resolved"] += 1
            meta["resolved"] += 1

    try:
        _show_link_resolve_overlay(page)
        meta["overlay_used"] = True
        for item in items:
            if item.get("href"):
                continue
            candidate = item.get("_resolve_candidate") or fresh_by_key.get(_card_key(item)) or {}
            if not candidate:
                continue
            meta["attempted"] += 1
            try:
                resolved = _resolve_candidate_url_quiet(page, candidate)
                if resolved:
                    meta["quiet"] += 1
                else:
                    meta["visible_click_fallback"] += 1
                    resolved = _resolve_candidate_url_light(page, candidate)
            except Exception as exc:
                if len(meta["errors"]) < 5:
                    meta["errors"].append(f"{type(exc).__name__}:{str(exc)[:120]}")
                resolved = ""
            if resolved:
                item["href"] = resolved
                item["url"] = resolved
                meta["clicked"] += 1
                meta["resolved"] += 1
            if pw._detect_captcha(page):
                meta["blocked"] = True
                meta["reason"] = "captcha_blocked_during_link_resolve"
                break
    finally:
        _hide_link_resolve_overlay(page)

    meta["unresolved"] = sum(1 for item in items if not item.get("href"))
    return meta


def _collect_cards_incremental(page: Page, query: str, count: int, max_scrolls: int | None = None, resolve_links: bool = False, network_store: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    target = max(count, 1)
    scroll_budget_mode = "explicit_cap" if max_scrolls is not None else "dynamic_default"
    # This is an upper bound, not a promise to run every round. The loop below
    # stops early when recent rounds show no item or DOM growth.
    scroll_budget = max_scrolls if max_scrolls is not None else min(24, max(3, (target + 7) // 8))
    if scroll_budget < 0:
        scroll_budget = 0

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    stagnant_rounds = 0
    no_progress_rounds = 0
    scroll_rounds = 0
    raw_candidate_count = 0
    last_candidate_count = 0
    stop_reason = ""
    end_marker = ""
    round_trace: list[dict[str, Any]] = []
    min_probe_rounds = min(scroll_budget, 3 if target <= 40 else 5)
    adaptive_stop_after = 3 if target <= 40 else 4

    for round_index in range(scroll_budget + 1):
        candidates = _collect_candidates(page, query, limit=max(target * 2, target + 20))
        current_candidate_count = len(candidates)
        candidate_pool_grew = current_candidate_count > last_candidate_count
        last_candidate_count = max(last_candidate_count, current_candidate_count)
        raw_candidate_count = max(raw_candidate_count, len(candidates))
        added = 0
        for candidate in candidates:
            item = _normalize_card(candidate)
            key = _card_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            item["_resolve_candidate"] = candidate
            item["rank"] = len(items) + 1
            item["source"] = "dom"
            items.append(item)
            added += 1
            if len(items) >= target:
                break
        if len(items) >= target:
            break

        if pw._detect_captcha(page):
            return items, {
                "blocked": True,
                "reason": "captcha_blocked",
                "scroll_rounds": scroll_rounds,
                "stagnant_rounds": stagnant_rounds,
                "raw_candidate_count": raw_candidate_count,
            }

        end_marker = _douyin_end_marker(page)
        if end_marker:
            stop_reason = "candidate_pool_exhausted"
            round_trace.append({
                "round": round_index,
                "candidate_count": current_candidate_count,
                "added": added,
                "item_count": len(items),
                "moved": False,
                "candidate_pool_grew": candidate_pool_grew,
                "end_marker": end_marker,
            })
            break

        if round_index >= scroll_budget:
            if len(items) < target:
                stop_reason = "scroll_budget_exhausted"
            break

        scroll_info = _scroll_search_results(page)
        before = scroll_info.get("before") or {}
        after = scroll_info.get("after") or {}
        scroll_rounds += 1

        moved = bool(scroll_info.get("moved")) or bool(after and before and (
            after.get("y", 0) > before.get("scrollTop", before.get("y", 0)) + 20
            or after.get("h", 0) > before.get("scrollHeight", before.get("h", 0)) + 20
            or after.get("activeScrollTop", 0) > before.get("scrollTop", 0) + 20
        ))
        before_card_count = int(before.get("cardCount", 0) or 0)
        after_card_count = int(after.get("cardCount", 0) or 0)
        dom_grew = after_card_count > before_card_count
        if added == 0:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        if added == 0 and not candidate_pool_grew and not dom_grew:
            no_progress_rounds += 1
        else:
            no_progress_rounds = 0
        round_trace.append({
            "round": round_index + 1,
            "candidate_count": current_candidate_count,
            "added": added,
            "item_count": len(items),
            "moved": moved,
            "candidate_pool_grew": candidate_pool_grew,
            "dom_grew": dom_grew,
            "no_progress_rounds": no_progress_rounds,
            "before_scroll_top": before.get("scrollTop", before.get("y", 0)),
            "after_scroll_y": after.get("y", 0),
            "after_active_scroll_top": after.get("activeScrollTop", 0),
            "before_card_count": before_card_count,
            "after_card_count": after_card_count,
        })
        if (
            len(items) < target
            and scroll_rounds >= min_probe_rounds
            and no_progress_rounds >= adaptive_stop_after
        ):
            stop_reason = "adaptive_no_progress"
            break

    result_items = items[:target]
    link_meta = {"enabled": resolve_links, "attempted": 0, "resolved": sum(1 for item in result_items if item.get("href")), "unresolved": 0}
    if resolve_links:
        link_meta = _resolve_missing_links(page, result_items, query, network_store=network_store)
    before_dedupe_count = len(result_items)
    result_items = _dedupe_items_by_key(result_items)
    deduped_after_link_resolve = before_dedupe_count - len(result_items)
    if deduped_after_link_resolve:
        link_meta["deduped_after_link_resolve"] = deduped_after_link_resolve
        link_meta["resolved"] = sum(1 for item in result_items if item.get("href"))
        link_meta["unresolved"] = sum(1 for item in result_items if not item.get("href"))
    seen_after_resolve = {_card_key(item) for item in result_items if _card_key(item)}
    network_candidates, network_items_meta = _network_records_to_items(network_store, query, set(seen_after_resolve), target - len(result_items))
    network_items_meta["used_for_order"] = False
    network_items_meta["would_add_if_unordered"] = len(network_candidates)
    network_items_meta["added"] = 0
    network_items_meta["reason"] = "strict_first_n_requires_dom_order"
    for item in result_items:
        item.pop("_resolve_candidate", None)

    return result_items, {
        "blocked": False,
        "strict_first_n": True,
        "ordered_source": "dom_first_seen",
        "scroll_rounds": scroll_rounds,
        "stagnant_rounds": stagnant_rounds,
        "no_progress_rounds": no_progress_rounds,
        "raw_candidate_count": raw_candidate_count,
        "scroll_budget": scroll_budget,
        "scroll_budget_mode": scroll_budget_mode,
        "adaptive_stop_after": adaptive_stop_after,
        "reason": stop_reason,
        "end_marker": end_marker,
        "round_trace": round_trace[-30:],
        "network_items": network_items_meta,
        "link_resolve": link_meta,
    }


def cards(query: str, count: int = 10, max_scrolls: int | None = None, resolve_links: bool = False) -> dict[str, Any]:
    decision = scheduler_gate("cards")
    if not decision.get("allowed"):
        return {
            "ok": False,
            "query": query,
            "domain": DOMAIN,
            "reason": decision.get("reason"),
            "scheduler": decision,
            "items": [],
        }

    page = _new_page()
    try:
        network_store = _attach_network_link_collector(page)
        search_url = _open_search_page(page, query)
        if not search_url:
            record_scheduler_result("search_failed", blocked=True, pages_increment=0)
            return {
                "ok": False,
                "query": query,
                "domain": DOMAIN,
                "reason": "search_failed",
                "scheduler": decision,
                "items": [],
            }

        items, collect_meta = _collect_cards_incremental(page, query, max(count, 1), max_scrolls=max_scrolls, resolve_links=resolve_links, network_store=network_store)
        if collect_meta.get("blocked"):
            record_scheduler_result(collect_meta.get("reason", "blocked"), blocked=True, pages_increment=0)
            return {
                "ok": False,
                "query": query,
                "domain": DOMAIN,
                "reason": collect_meta.get("reason", "blocked"),
                "target_count": count,
                "card_count": len(items),
                "items": items,
                "scheduler": decision,
                "flow_evidence": {"search_opened": True, "search_url": search_url, "final_url": page.url, "video_tab": ("type=video" in page.url or "source=video" in page.url), "card_count": len(items), "resolve_links": resolve_links, **collect_meta},
            }
        link_meta = collect_meta.get("link_resolve") or {}
        links_ok = (not resolve_links) or int(link_meta.get("unresolved", 0) or 0) == 0
        ok = len(items) >= count and links_ok
        outcome = "ok" if ok else ("links_incomplete" if len(items) >= count and not links_ok else (collect_meta.get("reason") or "insufficient_results"))
        record_scheduler_result(outcome, blocked=False, pages_increment=0)
        return {
            "ok": ok,
            "query": query,
            "domain": DOMAIN,
            "reason": None if ok else outcome,
            "target_count": count,
            "card_count": len(items),
            "link_count": sum(1 for item in items if item.get("href")),
            "items": items,
            "scheduler": decision,
            "flow_evidence": {"search_opened": True, "search_url": search_url, "final_url": page.url, "video_tab": ("type=video" in page.url or "source=video" in page.url), "card_count": len(items), "resolve_links": resolve_links, **collect_meta},
        }
    finally:
        pw.close_page(page)
        pw.close_isolated_context()


# ---------------------------------------------------------------------------
# Click video card on search page -> opens detail/modal
# ---------------------------------------------------------------------------

def _click_video_card(page: Page, href: str) -> tuple[bool, str]:
    if not href or "/video/" not in href:
        return False, "NO_HREF"
    video_id = href.split("?")[0].rstrip("/").split("/")[-1]
    try:
        pw.strip_target_blank(page, f'a[href*="{video_id}"]')
        loc = page.locator(f'a[href*="{video_id}"]').first
        loc.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        # Use human mouse move before clicking
        box = loc.bounding_box()
        if box:
            pw.human_mouse_move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        loc.click()
        page.wait_for_timeout(3000)
        url = page.url
        if "modal_id=" in url or "/video/" in url:
            return True, url
        return False, f"no_modal_id: {url}"
    except Exception as exc:
        return False, f"click_failed: {exc}"


def _click_candidate_card(page: Page, cand: dict[str, Any]) -> tuple[bool, str]:
    href = cand.get("href") or ""
    if href and "/video/" in href:
        return _click_video_card(page, href)

    card_index = cand.get("card_index")
    if card_index is None:
        return False, "NO_HREF_OR_CARD_INDEX"
    try:
        loc = page.locator(f'[data-search-fetch-card-index="{card_index}"]').first
        loc.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        box = loc.bounding_box()
        if box:
            target_x = int(box["x"] + box["width"] / 2)
            target_y = int(box["y"] + min(box["height"] * 0.45, box["height"] - 20))
            pw.human_mouse_move(page, target_x, target_y)
            page.mouse.click(target_x + random.randint(-4, 4), target_y + random.randint(-4, 4))
        else:
            loc.click(timeout=3000)
        page.wait_for_timeout(3000)
        url = page.url
        if "modal_id=" in url or "/video/" in url:
            return True, url
        try:
            if page.locator('#dy-modal-video-container-search_multi_modal, div[data-e2e="search-video-card-detail"]').count() > 0:
                return True, url
        except Exception:
            pass
        return False, f"no_modal_after_card_click: {url}"
    except Exception as exc:
        return False, f"card_click_failed: {exc}"


# ---------------------------------------------------------------------------
# Detail/modal: read text, open comments, close
# ---------------------------------------------------------------------------

def _read_detail_text(page: Page) -> str:
    js = """(() => {
      const selectors = [
        '#slidelist',
        '#dy-modal-video-container-search_multi_modal',
        '#sliderVideo',
        'div[data-e2e="search-video-card-detail"]',
        'div[class*="video-container"]',
        'main',
      ];
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.innerText && el.innerText.length > 100) {
          return (el.innerText || '').slice(0, 30000);
        }
      }
      return document.body.innerText.slice(0, 30000);
    })()"""
    try:
        return page.evaluate(js) or ""
    except Exception:
        return ""


def _read_detail_with_comment_check(page: Page) -> tuple[str, bool]:
    """Single evaluate that returns both text and comment-view status.

    Avoids calling _read_detail_text + _is_comment_view as two separate
    evaluate() calls, which doubles the browser-side execution traces.
    """
    js = """(() => {
      const selectors = [
        '#slidelist',
        '#dy-modal-video-container-search_multi_modal',
        '#sliderVideo',
        'div[data-e2e="search-video-card-detail"]',
        'div[class*="video-container"]',
        'main',
      ];
      let text = '';
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.innerText && el.innerText.length > 100) {
          text = (el.innerText || '').slice(0, 30000);
          break;
        }
      }
      if (!text) text = document.body.innerText.slice(0, 30000);
      const hasComments = text.includes('全部评论') || text.includes('留下你的精彩评论吧');
      return { text, hasComments };
    })()"""
    try:
        result = page.evaluate(js)
        if isinstance(result, dict):
            return result.get("text", ""), bool(result.get("hasComments", False))
        return "", False
    except Exception:
        return "", False


def _is_comment_view(text: str) -> bool:
    return ('全部评论' in text) or ('留下你的精彩评论吧' in text)


def _try_click_comment_icon(page: Page) -> bool:
    """Try to click the comment icon/button on the video detail panel.

    Mimics a real user: finds the comment affordance and clicks it with
    a slight random offset, rather than pressing the X keyboard shortcut.
    Returns True if a comment element was clicked.
    """
    comment_selectors = [
        'div[data-e2e="feed-comment-icon"]',
        'div[data-e2e="browse-comment"]',
        'div[class*="comment"][class*="icon"]',
        'span[class*="comment"]',
        # Fallback: any element whose accessible name suggests comments
    ]
    for sel in comment_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=600):
                box = loc.bounding_box()
                if box:
                    pw.human_mouse_move(
                        page,
                        int(box["x"] + box["width"] / 2 + random.randint(-4, 4)),
                        int(box["y"] + box["height"] / 2 + random.randint(-4, 4)),
                    )
                    page.mouse.click(
                        int(box["x"] + box["width"] / 2 + random.randint(-3, 3)),
                        int(box["y"] + box["height"] / 2 + random.randint(-3, 3)),
                    )
                    page.wait_for_timeout(2500 + int(random.random() * 1500))
                    return True
        except Exception:
            continue

    # Last resort: try aria-label / text-based match via JS click
    try:
        clicked = page.evaluate("""(() => {
            const candidates = Array.from(document.querySelectorAll(
                'div[role="button"], button, span, a'
            ));
            for (const el of candidates) {
                const t = (el.innerText || '').trim();
                const label = (el.getAttribute('aria-label') || '').trim();
                if (t === '评论' || label.includes('评论') || t.includes('全部评论')) {
                    el.click();
                    return true;
                }
            }
            return false;
        })()""")
        if clicked:
            page.wait_for_timeout(2500 + int(random.random() * 1500))
            return True
    except Exception:
        pass

    return False


def _open_comments(page: Page, debug_log: list[dict[str, Any]] | None = None) -> bool:
    text, has_comments = _read_detail_with_comment_check(page)
    if debug_log is not None:
        debug_log.append({'step': 'detail_opened', 'text_head': text[:1200]})
    if has_comments:
        return True

    for attempt in range(3):
        icon_clicked = _try_click_comment_icon(page)
        if debug_log is not None:
            debug_log.append({'step': f'comment_click_attempt_{attempt + 1}', 'clicked': icon_clicked})

        if icon_clicked:
            page.wait_for_timeout(800 + int(random.random() * 800))
        else:
            modal_selectors = [
                '#slidelist',
                '#dy-modal-video-container-search_multi_modal',
                '#sliderVideo',
                'div[data-e2e="search-video-card-detail"]',
                'div[class*="video-container"]',
            ]
            for sel in modal_selectors:
                try:
                    modal = page.locator(sel).first
                    if modal.is_visible(timeout=800):
                        box = modal.bounding_box()
                        if box:
                            target_x = int(box["x"] + min(box["width"] * 0.88, box["width"] - 24))
                            target_y = int(box["y"] + min(box["height"] * 0.72, box["height"] - 24))
                            pw.human_mouse_move(page, target_x, target_y)
                            page.mouse.click(target_x + random.randint(-4, 4), target_y + random.randint(-4, 4))
                            page.wait_for_timeout(500 + int(random.random() * 500))
                        break
                except Exception:
                    continue

        text, has_comments = _read_detail_with_comment_check(page)
        if has_comments:
            if debug_log is not None:
                debug_log.append({'step': f'after_comment_click_{attempt + 1}', 'text_head': text[:1200]})
            return True

    if debug_log is not None:
        debug_log.append({'step': 'comment_open_failed', 'text_head': text[:1200]})
    return False


def _scroll_comments(page: Page, rounds: int = 4, debug_log: list[dict[str, Any]] | None = None) -> None:
    """Scroll down within the comment panel to trigger lazy-load of more comments.

    Stops early if:
      - no comments are visible (page has no comment panel)
      - comments reached the bottom (text stopped growing)
      - scroll caused navigation away from current video
    """
    vp = page.viewport_size or {"width": 1440, "height": 900}
    # Randomize mouse position each call to avoid fixed-coordinate fingerprint
    cx = int(vp["width"] * random.uniform(0.60, 0.80))
    cy = int(vp["height"] * random.uniform(0.40, 0.65))
    page.mouse.move(cx, cy)
    page.wait_for_timeout(300 + int(random.random() * 200))

    # Capture the current modal_id to detect navigation drift
    current_url = page.url
    prev_text, has_comments = _read_detail_with_comment_check(page)
    prev_text_len = len(prev_text)

    # Bail immediately if no comment panel at all
    if not has_comments:
        if debug_log is not None:
            debug_log.append({'step': 'comment_scroll_skip', 'reason': 'no_comment_view'})
        return

    for i in range(rounds):
        # Guard: still on the same video?
        if page.url != current_url and 'modal_id=' in current_url:
            new_id = page.url.split('modal_id=')[-1].split('&')[0] if 'modal_id=' in page.url else ''
            old_id = current_url.split('modal_id=')[-1].split('&')[0] if 'modal_id=' in current_url else ''
            if new_id != old_id:
                if debug_log is not None:
                    debug_log.append({'step': f'comment_scroll_abort_{i+1}', 'reason': 'video_changed', 'new_url': page.url[:200]})
                break

        # Human-like scroll: variable total delta, split into ticks with random variance
        base_delta = random.choice([600, 800, 1000, 1200, 1400, 1600])
        ticks = random.randint(2, 5)
        for t in range(ticks):
            # ±30% variance per tick so deltas are not uniform
            jitter = base_delta * random.uniform(0.7, 1.3)
            d = int(jitter / ticks)
            page.mouse.wheel(0, d)
            time.sleep(random.uniform(0.04, 0.25))
        page.wait_for_timeout(random.uniform(1.5, 3.5))

        snippet, has_comments = _read_detail_with_comment_check(page)
        cur_text_len = len(snippet)

        # Guard: comments disappeared (scrolled into next video's content)
        if not has_comments:
            if debug_log is not None:
                debug_log.append({'step': f'comment_scroll_abort_{i+1}', 'reason': 'comment_view_lost', 'text_len': cur_text_len})
            break

        # Guard: no growth for 2 consecutive rounds -> reached bottom
        if cur_text_len == prev_text_len and i > 0:
            if debug_log is not None:
                debug_log.append({'step': f'comment_scroll_abort_{i+1}', 'reason': 'text_stopped_growing', 'text_len': cur_text_len})
            break

        if debug_log is not None:
            debug_log.append({
                'step': f'comment_scroll_{i+1}',
                'text_len': cur_text_len,
                'text_head': snippet[:800],
            })
        prev_text_len = cur_text_len


def _close_detail(page: Page) -> bool:
    close_selectors = [
        'div[data-e2e="video-close"]',
        'button[data-e2e="video-close"]',
        'div[class*="close"]',
        'button[class*="close"]',
        'svg[class*="close"]',
    ]
    for _ in range(3):
        for sel in close_selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=600):
                    box = loc.bounding_box()
                    if box:
                        target_x = int(box["x"] + box["width"] / 2)
                        target_y = int(box["y"] + box["height"] / 2)
                        pw.human_mouse_move(page, target_x, target_y)
                        page.mouse.click(target_x + random.randint(-3, 3), target_y + random.randint(-3, 3))
                        page.wait_for_timeout(1200 + int(random.random() * 600))
                        if "modal_id=" not in page.url:
                            return True
            except Exception:
                continue

        try:
            modal = page.locator('#slidelist, #dy-modal-video-container-search_multi_modal, #sliderVideo, div[data-e2e="search-video-card-detail"], div[class*="video-container"]').first
            if modal.is_visible(timeout=600):
                box = modal.bounding_box()
                if box:
                    target_x = int(box["x"] + min(box["width"] * 0.08, 36))
                    target_y = int(box["y"] + min(box["height"] * 0.08, 36))
                    pw.human_mouse_move(page, target_x, target_y)
                    page.mouse.click(target_x + random.randint(-5, 5), target_y + random.randint(-5, 5))
                    page.wait_for_timeout(1200 + int(random.random() * 600))
                    if "modal_id=" not in page.url:
                        return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Comment extraction
# ---------------------------------------------------------------------------

def _extract_comments(text: str, limit: int = 20) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    out: list[str] = []
    in_comments = False
    skip_exact = {
        '发送', '倍速', '智能', '清屏', '连播', '搜索', '投稿', '通知', '私信',
        '详情', 'TA的作品', '评论', '问AI', '合集', '识别画面', '加载中',
        '留下你的精彩评论吧', '大家都在搜：'
    }
    for line in lines:
        if line.startswith('全部评论'):
            in_comments = True
            continue
        if not in_comments:
            continue
        if line in skip_exact:
            continue
        if line.startswith('@'):
            continue
        if line.startswith('第') and '集' in line:
            continue
        if line.startswith('展开') and '回复' in line:
            continue
        if '·' in line and ('分钟前' in line or '小时前' in line or '天前' in line or '周前' in line or '月前' in line):
            continue
        if line in {'分享', '回复'}:
            continue
        if line.isdigit():
            continue
        if line.startswith(('00:', '01:', '02:', '03:', '04:', '05:', '06:', '07:', '08:', '09:')):
            continue
        if '相关搜索' in line or '更新至第' in line or '合集：' in line:
            continue
        if line.startswith('· 20') or line.startswith('· 1'):
            continue
        if len(line) < 8:
            continue
        out.append(line)
    seen = set()
    cleaned = []
    for line in out:
        if line not in seen:
            seen.add(line)
            cleaned.append(line)
        if len(cleaned) >= limit:
            break
    return cleaned


# ---------------------------------------------------------------------------
# Main: sample()
# ---------------------------------------------------------------------------

def sample(query: str, count: int = 5, debug: bool = False) -> dict[str, Any]:
    decision = scheduler_gate("fetch")
    if not decision.get("allowed"):
        return {"ok": False, "query": query, "domain": DOMAIN, "reason": decision.get("reason"), "scheduler": decision, "items": [], "flow_evidence": {"site_search_complete": False}}

    page = _new_page()
    try:
        items: list[dict[str, Any]] = []

        # Phase 1: Open search page and collect candidates
        search_url = _open_search_page(page, query)
        if not search_url:
            record_scheduler_result("search_failed", blocked=True, pages_increment=0)
            return {"ok": False, "query": query, "domain": DOMAIN, "items": [], "reason": "captcha_blocked", "scheduler": decision, "flow_evidence": {"site_search_complete": False}}

        candidates = _collect_candidates(page, query, limit=max(count + 3, 8))
        if not candidates:
            debug_state = _debug_page_state(page, "no_search_results")
            record_scheduler_result("search_failed", blocked=True, pages_increment=0)
            return {"ok": False, "query": query, "domain": DOMAIN, "items": [], "reason": "no_search_results", "scheduler": decision, "flow_evidence": {"site_search_complete": True, "debug_state": debug_state}}

        visited_hrefs: set[str] = set()

        # Phase 2: For each candidate, click -> open comments -> capture -> close detail
        for cand in candidates:
            if len([i for i in items if i.get('parse_ok')]) >= count:
                break

            href = cand.get('href', '')
            visit_key = href or f"card:{cand.get('card_index')}"
            if not visit_key or visit_key in visited_hrefs:
                continue
            visited_hrefs.add(visit_key)

            # If a previous detail is open, close it first
            if "modal_id=" in page.url:
                _close_detail(page)
                time.sleep(1)

            # Ensure the link is visible (scroll if needed)
            if href:
                video_id = href.split("?")[0].rstrip("/").split("/")[-1]
                try:
                    loc = page.locator(f'a[href*="{video_id}"]').first
                    loc.scroll_into_view_if_needed()
                    page.wait_for_timeout(300)
                except Exception:
                    pw.scroll_pages(page, min_pages=1)
                    time.sleep(1)

            # Click the video card
            ok, status = _click_candidate_card(page, cand)
            if not ok:
                items.append({
                    'debug_candidate_title': cand.get('title'),
                    'candidate': cand,
                    'click_ok': False, 'click_status': status,
                    'url': page.url, 'parse_ok': False,
                    'title': cand.get('title'),
                    'main_text': '', 'comments': [],
                    'comment_text_length': 0, 'comment_opened': False,
                    'debug_steps': [],
                    'checklist': {'search_opened': True, 'real_video_modal_opened': False,
                                  'focus_returned_to_page': False, 'comments_opened': False,
                                  'comments_captured': False},
                })
                continue

            # Open comments with X
            debug_steps: list[dict[str, Any]] = []
            comment_opened = _open_comments(page, debug_steps)

            # Scroll down in comment panel to load more comments
            if comment_opened:
                _scroll_comments(page, rounds=1, debug_log=debug_steps)

            # Capture text and extract comments
            text = _read_detail_text(page)
            comments = _extract_comments(text)
            url = page.url
            parse_ok = bool(text and len(text) >= 80)

            items.append({
                'debug_candidate_title': cand.get('title'),
                'candidate': cand,
                'click_ok': True, 'click_status': status,
                'url': url, 'parse_ok': parse_ok,
                'title': cand.get('title'),
                'main_text': text,
                'comments': comments,
                'comment_text_length': sum(len(x) for x in comments),
                'comment_opened': comment_opened,
                'debug_steps': debug_steps,
                'checklist': {
                    'search_opened': True,
                    'real_video_modal_opened': True,
                    'focus_returned_to_page': True,
                    'comments_opened': comment_opened,
                    'comments_captured': len(comments) >= 2,
                },
            })

            # Close detail/modal before next iteration
            _close_detail(page)
            # Variable "consume content" pause: sometimes quick, sometimes linger
            # Weighted toward shorter pauses but with occasional long ones
            if random.random() < 0.15:
                # ~15% chance: long pause as if watching/reading
                time.sleep(8 + random.random() * 12)
            else:
                time.sleep(2 + random.random() * 4)

        success_count = len([i for i in items if i.get('parse_ok')])
        record_scheduler_result(
            "ok" if success_count >= count else "insufficient_results",
            blocked=success_count < count,
            pages_increment=len(items),
        )
        final_checklist = items[-1].get('checklist') if items else {
            'search_opened': False, 'real_video_modal_opened': False,
            'focus_returned_to_page': False, 'comments_opened': False,
            'comments_captured': False,
        }
        return {
            'ok': success_count >= count,
            'items': items,
            'checklist': final_checklist,
            'opened_modal_count': len(items),
            'successful_modal_count': success_count,
            'scheduler': decision,
            'flow_evidence': {'opened_modal_count': len(items), 'successful_modal_count': success_count},
        }
    finally:
        # Close page if not captcha-frozen; close isolated context to clean up
        pw.close_page(page)
        pw.close_isolated_context()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: douyin_sampler.py <query> [--debug]')
    q = sys.argv[1]
    debug = '--debug' in sys.argv[2:]
    print(json.dumps(sample(q, debug=debug), ensure_ascii=False, indent=2))
