#!/usr/bin/env python3
"""Fetch bilibili video comments via public/API-style endpoints, preferring API over DOM."""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from functools import reduce
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _config import BILIBILI_COOKIE_PATH as COOKIE_PATH

VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
REPLY_API = "https://api.bilibili.com/x/v2/reply?{query}"
SUB_REPLY_API = "https://api.bilibili.com/x/v2/reply/reply?{query}"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
    ),
    "Referer": "https://www.bilibili.com",
    "Origin": "https://www.bilibili.com",
}
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _load_cookie() -> str:
    if COOKIE_PATH.exists():
        text = COOKIE_PATH.read_text().strip()
        if text:
            return text
    import os
    refresh_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bilibili_cookie_refresh.py')
    print(f"[search-fetch] B站 cookie 未找到: {COOKIE_PATH}", file=sys.stderr)
    print(f"  请先在 Edge 中登录 bilibili.com，然后运行:", file=sys.stderr)
    print(f"  python3 {refresh_script}", file=sys.stderr)
    return ""


def _build_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    cookie = _load_cookie()
    if cookie:
        headers['Cookie'] = cookie
    if extra:
        headers.update(extra)
    return headers


def _get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or _build_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return {"code": e.code, "message": str(e), "data": {}}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        return {"code": -1, "message": f"network_error:{type(e).__name__}:{e}", "data": {}}


def get_video_info(bvid: str) -> dict[str, Any]:
    data = _get_json(VIEW_API.format(bvid=bvid))
    if data.get("code") != 0:
        raise ValueError(f"view api failed: {data.get('code')} {data.get('message')}")
    payload = data.get("data") or {}
    return {
        "bvid": payload.get("bvid") or bvid,
        "aid": payload.get("aid"),
        "cid": payload.get("cid"),
        "title": payload.get("title"),
        "owner": ((payload.get("owner") or {}).get("name")),
        "reply_count": ((payload.get("stat") or {}).get("reply")),
        "view_count": ((payload.get("stat") or {}).get("view")),
        "pubdate": payload.get("pubdate"),
    }


def _get_wbi_keys() -> tuple[str, str]:
    nav = _get_json("https://api.bilibili.com/x/web-interface/nav")
    wbi_img = (nav.get("data") or {}).get("wbi_img") or {}
    img_url = wbi_img.get("img_url", "")
    sub_url = wbi_img.get("sub_url", "")
    img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
    return img_key, sub_key


def _gen_mixin_key(orig: str) -> str:
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def _sign_wbi_params(params: dict[str, Any]) -> dict[str, Any]:
    img_key, sub_key = _get_wbi_keys()
    mixin_key = _gen_mixin_key(img_key + sub_key)
    signed = dict(params)
    signed["wts"] = int(time.time())
    filtered = {k: str(v).translate({ord(c): None for c in "!'()*"}) for k, v in signed.items() if v is not None}
    query = urllib.parse.urlencode(sorted(filtered.items()))
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


def fetch_root_comments(aid: int | str, pn: int = 1, ps: int = 20, sort: int = 2) -> dict[str, Any]:
    params = {
        "type": 1,
        "oid": str(aid),
        "pn": pn,
        "ps": min(max(ps, 1), 49),
        "sort": sort,
    }
    signed = _sign_wbi_params(params)
    url = REPLY_API.format(query=urllib.parse.urlencode(signed))
    return _get_json(url)


def fetch_sub_comments(aid: int | str, root: int | str, pn: int = 1, ps: int = 20) -> dict[str, Any]:
    params = {
        "type": 1,
        "oid": str(aid),
        "root": str(root),
        "pn": pn,
        "ps": min(max(ps, 1), 49),
    }
    signed = _sign_wbi_params(params)
    url = SUB_REPLY_API.format(query=urllib.parse.urlencode(signed))
    return _get_json(url)


def normalize_reply(reply: dict[str, Any], level: str = "root") -> dict[str, Any]:
    content = reply.get("content") or {}
    member = reply.get("member") or {}
    return {
        "level": level,
        "rpid": reply.get("rpid"),
        "root": reply.get("root"),
        "parent": reply.get("parent"),
        "mid": reply.get("mid"),
        "uname": member.get("uname"),
        "ctime": reply.get("ctime"),
        "like": reply.get("like"),
        "message": content.get("message") or "",
        "reply_count": reply.get("rcount") or reply.get("count") or 0,
    }


def collect_comments(
    bvid: str,
    max_root_pages: int = 10,
    sort: int = 2,
    include_sub: bool = False,
    max_sub_pages: int = 5,
    sleep_sec: float = 0.6,
    within: Any | None = None,
) -> dict[str, Any]:
    video = get_video_info(bvid)
    aid = video["aid"]
    root_comments: list[dict[str, Any]] = []
    sub_comments: list[dict[str, Any]] = []
    root_errors: list[dict[str, Any]] = []
    sub_errors: list[dict[str, Any]] = []

    for pn in range(1, max_root_pages + 1):
        payload = fetch_root_comments(aid, pn=pn, sort=sort)
        if payload.get("code") != 0:
            root_errors.append({"page": pn, "code": payload.get("code"), "message": payload.get("message")})
            break
        replies = ((payload.get("data") or {}).get("replies") or [])
        if not replies:
            break
        for reply in replies:
            root_comments.append(normalize_reply(reply, level="root"))
            if include_sub and (reply.get("rcount") or reply.get("count") or 0) > 0:
                root_id = reply.get("rpid")
                for sub_pn in range(1, max_sub_pages + 1):
                    sub_payload = fetch_sub_comments(aid, root_id, pn=sub_pn)
                    if sub_payload.get("code") != 0:
                        sub_errors.append({"root": root_id, "page": sub_pn, "code": sub_payload.get("code"), "message": sub_payload.get("message")})
                        break
                    sub_replies = ((sub_payload.get("data") or {}).get("replies") or [])
                    if not sub_replies:
                        break
                    for sub in sub_replies:
                        sub_comments.append(normalize_reply(sub, level="sub"))
                    time.sleep(sleep_sec)
        time.sleep(sleep_sec)

    # Filter by time range if within is specified
    if within is not None:
        from _time_filter import filter_bili_comments_by_ctime
        root_comments = filter_bili_comments_by_ctime(root_comments, within)
        sub_comments = filter_bili_comments_by_ctime(sub_comments, within)

    return {
        "video": video,
        "sort": sort,
        "max_root_pages": max_root_pages,
        "include_sub": include_sub,
        "root_comments": root_comments,
        "sub_comments": sub_comments,
        "root_errors": root_errors,
        "sub_errors": sub_errors,
    }


def find_comment_by_substring(
    bvid: str,
    needle: str,
    max_root_pages: int = 20,
    include_sub: bool = True,
    max_sub_pages: int = 10,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    video = get_video_info(bvid)
    aid = video["aid"]
    for sort in (2, 0):
        for pn in range(1, max_root_pages + 1):
            payload = fetch_root_comments(aid, pn=pn, sort=sort)
            if payload.get("code") != 0:
                break
            replies = ((payload.get("data") or {}).get("replies") or [])
            if not replies:
                break
            for reply in replies:
                row = normalize_reply(reply, level="root")
                if needle in (row.get("message") or ""):
                    row["sort"] = sort
                    row["pn"] = pn
                    matches.append(row)
                if include_sub and (reply.get("rcount") or reply.get("count") or 0) > 0:
                    root_id = reply.get("rpid")
                    for sub_pn in range(1, max_sub_pages + 1):
                        sub_payload = fetch_sub_comments(aid, root_id, pn=sub_pn)
                        if sub_payload.get("code") != 0:
                            break
                        sub_replies = ((sub_payload.get("data") or {}).get("replies") or [])
                        if not sub_replies:
                            break
                        for sub in sub_replies:
                            srow = normalize_reply(sub, level="sub")
                            if needle in (srow.get("message") or ""):
                                srow["sort"] = sort
                                srow["pn"] = pn
                                srow["sub_pn"] = sub_pn
                                matches.append(srow)
                        time.sleep(0.3)
            if matches:
                return {"ok": True, "video": video, "matches": matches}
            time.sleep(0.3)
    return {"ok": False, "video": video, "matches": [], "needle": needle}


if __name__ == "__main__":
    import re

    if len(sys.argv) < 3:
        raise SystemExit(
            "usage: bilibili_comment_fetch.py <collect|find> <bvid-or-query> [needle] [--root-pages N] [--include-sub] [--sub-pages N] [--print-bvid] [--save-cookie]"
        )

    mode = sys.argv[1]
    target = sys.argv[2]
    args = sys.argv[3:]

    def _resolve_bvid(value: str, save_cookie: bool = False) -> str:
        if re.fullmatch(r"BV[0-9A-Za-z]+", value):
            return value
        import _playwright_base as pw
        page = pw.new_page()
        try:
            # Navigate to bilibili via Bing (simulates human behavior)
            nav = pw.human_navigate_to_site(page, "B站", "bilibili.com")
            if nav.get("ok"):
                # Check for captcha — wait 60s for human to solve, then abort
                captcha = pw.wait_for_captcha_or_proceed(page, wait_seconds=60.0)
                if captcha.get("blocked"):
                    return ""

                # Use bilibili's search to find the video
                time.sleep(2)
                try:
                    search_input = page.locator('input.nav-search-input, input[class*="search-input"]').first
                    search_input.click()
                    time.sleep(0.3)
                    for char in value:
                        page.keyboard.type(char, delay=40)
                    time.sleep(0.3)
                    page.keyboard.press("Enter")
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    time.sleep(5)
                except Exception:
                    pass
            else:
                # Fallback to direct URL
                query = urllib.parse.quote(value)
                url = f"https://search.bilibili.com/all?keyword={query}"
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(7000)
            page.wait_for_timeout(7000)
            # Scroll at least 5 full pages before scraping search results
            pw.scroll_pages(page, min_pages=5)
            href = page.evaluate("""(() => {
              const links = Array.from(document.querySelectorAll('a[href*="/video/BV"]'));
              const hrefs = links.map(a => a.href).filter(h => /\\/video\\/BV[0-9A-Za-z]+/.test(h));
              return hrefs[0] || '';
            })()""")
            if not href:
                return ""
            page.goto(href, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(8000)
            detail_url = page.url
            detail_cookie = page.evaluate("document.cookie || ''")
            m = re.search(r"/video/(BV[0-9A-Za-z]+)", detail_url or href)
            bvid = m.group(1) if m else ""
            if save_cookie:
                cookie = detail_cookie.strip()
                if cookie:
                    COOKIE_PATH.write_text(cookie)
            if not re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
                raise SystemExit(f"could not resolve bvid from query: {value}")
            return bvid
        finally:
            pw.close_page(page)

    save_cookie = "--save-cookie" in args
    print_bvid = "--print-bvid" in args
    bvid = _resolve_bvid(target, save_cookie=save_cookie)
    if print_bvid:
        print(bvid)
        raise SystemExit(0)

    def _flag(name: str, default: str | None = None):
        if name in args:
            idx = args.index(name)
            if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
                return args[idx + 1]
            return default
        return default

    max_root_pages = int(_flag("--root-pages", "10") or "10")
    max_sub_pages = int(_flag("--sub-pages", "5") or "5")
    include_sub = "--include-sub" in args

    if mode == "collect":
        result = collect_comments(bvid, max_root_pages=max_root_pages, include_sub=include_sub, max_sub_pages=max_sub_pages)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif mode == "find":
        needle = args[0] if args and not args[0].startswith("--") else ""
        if not needle:
            raise SystemExit("find mode requires needle")
        result = find_comment_by_substring(bvid, needle, max_root_pages=max_root_pages, include_sub=include_sub, max_sub_pages=max_sub_pages)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        raise SystemExit(f"unknown mode: {mode}")
