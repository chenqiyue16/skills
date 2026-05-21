#!/usr/bin/env python3
"""Refresh bilibili cookie from Playwright Edge session into the configured search-fetch cookie file.

Replaces the Safari-based version — uses Playwright with Edge.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import playwright_fetch  # type: ignore
import _playwright_base as pw
from _config import BILIBILI_COOKIE_PATH as COOKIE_PATH

BILIBILI_URL = 'https://www.bilibili.com'
COOKIE_ORDER = [
    'bili_jct', 'bili_ticket', 'bili_ticket_expires', 'buvid3', 'buvid4', 'SESSDATA',
    'DedeUserID', 'DedeUserID__ckMd', 'sid', '_uuid', 'b_lsid', 'b_nut',
    'CURRENT_FNVAL', 'CURRENT_QUALITY', 'buvid_fp', 'bp_t_offset_478751937',
    'browser_resolution', 'home_feed_column', 'rpdid', 'theme-avatar-tip-show', 'theme-tip-show'
]
REQUIRED = ['SESSDATA', 'bili_jct', 'DedeUserID']


def refresh() -> dict:
    page = pw.new_page()
    try:
        # Navigate to bilibili first to ensure cookies are loaded
        page.goto(BILIBILI_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # Use Playwright context.cookies() — can access httpOnly cookies like SESSDATA
        _, ctx = pw._ensure_playwright()
        raw_cookies = ctx.cookies(BILIBILI_URL)
        cookies = {c['name']: c['value'] for c in raw_cookies if c.get('name') and c.get('value')}

        missing = [name for name in REQUIRED if not cookies.get(name)]
        if missing:
            return {
                'ok': False,
                'url': page.url,
                'title': page.title() if page else '',
                'missing': missing,
                'available_keys': sorted(cookies.keys()),
            }
        cookie_str = playwright_fetch.write_cookie_file(COOKIE_PATH, cookies, order=COOKIE_ORDER)
        return {
            'ok': True,
            'url': page.url,
            'title': page.title() if page else '',
            'path': str(COOKIE_PATH),
            'written_keys': [name for name in COOKIE_ORDER if cookies.get(name)],
            'cookie_length': len(cookie_str),
        }
    finally:
        pw.close_page(page)


if __name__ == '__main__':
    print(json.dumps(refresh(), ensure_ascii=False, indent=2))
