#!/usr/bin/env python3
"""Core Playwright browser lifecycle module using Edge with persistent user profile.

Provides a singleton browser context shared across all platform samplers.
Uses the user's Edge profile (with login cookies) via persistent context.

Strategy:
1. Copy essential login files from real Edge to temp profile (first time only)
2. Use temp profile for Playwright — never touch the user's running Edge
3. On shutdown, try to sync cookies back to real Edge profile
4. If Edge is running (files locked), save a pending-sync marker for next time
"""

from __future__ import annotations

import atexit
import json as _json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Page

_lock = threading.Lock()
_pw: Playwright | None = None
_context: BrowserContext | None = None
_used_real_profile: bool = False

_STEALTH_INIT_JS = """
// Overwrite navigator.webdriver to prevent automated browser detection
// Overwrite navigator.webdriver on the prototype to avoid instance-level descriptor detection
// Real browsers define this on Navigator.prototype, not on the instance
Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined, configurable: true});
// Remove Playwright/automation indicators from navigator
delete navigator.__proto__.webdriver;
// Override permissions query to avoid detection via permissions API
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        origQuery(parameters)
);
// Override plugins to match real Edge — must not be an Array, must contain objects
(function() {
    var pluginData = [
        {name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}
    ];
    var Fp = function() {};
    Fp.prototype.item = function(i) { return this[i] || null; };
    Fp.prototype.namedItem = function(n) {
        for (var k = 0; k < this.length; k++) { if (this[k] && this[k].name === n) return this[k]; }
        return null;
    };
    Fp.prototype.refresh = function() {};
    Object.defineProperty(Fp.prototype, Symbol.toStringTag, {value: 'PluginArray'});
    var obj = new Fp();
    for (var j = 0; j < pluginData.length; j++) { obj[j] = pluginData[j]; }
    obj.length = pluginData.length;
    Object.defineProperty(navigator, 'plugins', {
        get: function() { return obj; },
        configurable: true,
    });
})();
// Override languages on prototype to avoid instance-level descriptor detection
Object.defineProperty(Navigator.prototype, 'languages', {
    get: function() { return ['zh-CN', 'zh', 'en-US', 'en']; },
    configurable: true,
});
// Clean up Playwright injection artifacts that sites can detect
// Playwright injects scripts with specific file names that appear in stack traces
const origError = Error;
const origCaptureStackTrace = Error.captureStackTrace;
if (origCaptureStackTrace) {
    Error.captureStackTrace = function(target, constructor) {
        origCaptureStackTrace.call(this, target, constructor);
        if (target.stack) {
            target.stack = target.stack.split('\\n').filter(line =>
                !line.includes('__playwright') &&
                !line.includes('playwright_evaluation_script')
            ).join('\\n');
        }
    };
}
// Mask automation indicators in toString checks
// Some sites check Function.prototype.toString on native APIs
const origToString = Function.prototype.toString;
const nativeToStringMap = new WeakMap();
const markNative = (fn) => { nativeToStringMap.set(fn, 'function ' + fn.name + '() { [native code] }'); };
// Pre-mark key APIs that sites check
if (window.fetch) markNative(window.fetch);
if (window.XMLHttpRequest) markNative(window.XMLHttpRequest.prototype.open);
if (window.EventSource) markNative(window.EventSource);
Function.prototype.toString = function() {
    if (nativeToStringMap.has(this)) return nativeToStringMap.get(this);
    return origToString.call(this);
};
"""

# Edge user data directory
_local_app = os.environ.get('LOCALAPPDATA', '')
EDGE_USER_DATA = Path(_local_app) / 'Microsoft' / 'Edge' / 'User Data' if _local_app else None

# Temp profile paths (persistent, NOT deleted between runs)
_TEMP_SHARED = Path(os.environ.get('TEMP', os.environ.get('TMP', ''))) / "playwright_edge_shared"
_TEMP_ISOLATED = Path(os.environ.get('TEMP', os.environ.get('TMP', ''))) / "playwright_edge_profile"

# Common viewport sizes to mimic real users
_VIEWPORTS = [
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1920, "height": 1080},
    {"width": 1600, "height": 900},
    {"width": 1280, "height": 720},
]

# Cookie/login files to manage
_COOKIE_FILES = [
    "Cookies", "Cookies-journal",
    "Login Data", "Login Data-journal",
    "Web Data", "Web Data-journal",
    "Preferences", "Secure Preferences",
    "Network/Cookies", "Network/Cookies-journal",
]


def _random_viewport() -> dict:
    """Return a random common viewport size."""
    return random.choice(_VIEWPORTS)


def _is_edge_running() -> bool:
    """Check if msedge.exe is currently running."""
    try:
        result = subprocess.run(
            ['tasklist.exe', '/FI', 'IMAGENAME eq msedge.exe', '/NH'],
            capture_output=True, text=True, timeout=5,
        )
        return 'msedge.exe' in result.stdout
    except Exception:
        return False


def _close_edge_gracefully(timeout: int = 10) -> bool:
    """Close Edge gracefully and wait for it to exit."""
    try:
        subprocess.run(
            ['taskkill.exe', '/IM', 'msedge.exe', '/F'],
            capture_output=True, timeout=timeout,
        )
        # Also kill WebView2 processes that hold the profile lockfile
        subprocess.run(
            ['taskkill.exe', '/IM', 'msedgewebview2.exe', '/F'],
            capture_output=True, timeout=timeout,
        )
        # Wait for Edge to fully exit
        for _ in range(timeout * 2):
            if not _is_edge_running():
                return True
            time.sleep(0.5)
        return not _is_edge_running()
    except Exception:
        return False


def _pending_sync_path(temp_profile: Path) -> Path:
    return temp_profile / ".pending_sync"


def _try_apply_pending_sync(temp_profile: Path) -> bool:
    """If there's a pending sync from a previous session and Edge is not running,
    apply it now. Returns True if sync was applied."""
    marker = _pending_sync_path(temp_profile)
    if not marker.exists():
        return False
    if _is_edge_running():
        return False  # Edge is running, can't write to its profile files yet
    if not _sync_cookies_back(temp_profile):
        return False
    try:
        marker.unlink()
    except Exception:
        pass
    return True


def _sync_cookies_back(temp_profile: Path) -> bool:
    """Copy updated cookies/login state from temp profile back to real Edge profile.

    Does NOT close Edge. If Edge is running and files are locked, returns False
    so the caller can create a pending-sync marker for next time.

    Returns True if at least one file was synced successfully.
    """
    if not EDGE_USER_DATA or not EDGE_USER_DATA.exists():
        return False
    import shutil
    default_src = temp_profile / "Default"
    default_dst = EDGE_USER_DATA / "Default"
    if not default_src.exists():
        return False
    synced = 0
    for fname in _COOKIE_FILES:
        src = default_src / fname
        dst = default_dst / fname
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(src), str(dst))
                synced += 1
            except Exception:
                pass
    if synced:
        print(f"[search-fetch] 已同步 {synced} 个文件回真实 Edge profile，登录态已保存", file=sys.stderr)
    return synced > 0


def _save_pending_sync(temp_profile: Path) -> None:
    """Mark that there are unsynced cookies waiting to be written back to real Edge."""
    marker = _pending_sync_path(temp_profile)
    try:
        marker.write_text(str(time.time()))
    except Exception:
        pass


def _copy_profile_once(temp_profile: Path) -> None:
    """Copy essential login files from real Edge to temp profile.
    Only copies files that don't already exist in temp — never overwrites.
    This ensures Playwright session cookies accumulate rather than being wiped each run.
    """
    import shutil
    if not temp_profile.exists():
        temp_profile.mkdir(parents=True)

    default_src = EDGE_USER_DATA / "Default"
    default_dst = temp_profile / "Default"
    if not default_dst.exists():
        default_dst.mkdir(parents=True)

    for fname in _COOKIE_FILES:
        src = default_src / fname
        dst = default_dst / fname
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(src), str(dst))
            except Exception:
                pass

    local_state_src = EDGE_USER_DATA / "Local State"
    local_state_dst = temp_profile / "Local State"
    if local_state_src.exists() and not local_state_dst.exists():
        try:
            shutil.copy2(str(local_state_src), str(local_state_dst))
        except Exception:
            pass


def _clear_restored_tabs(temp_profile: Path) -> None:
    """Keep login state, but prevent Edge from restoring previous scraper tabs."""
    import shutil
    default_dir = temp_profile / "Default"
    for rel in ("Sessions",):
        target = default_dir / rel
        try:
            if target.exists():
                shutil.rmtree(target)
        except Exception:
            pass
    for rel in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        target = default_dir / rel
        try:
            if target.exists():
                target.unlink()
        except Exception:
            pass


def _close_existing_pages(ctx: BrowserContext) -> None:
    for page in list(ctx.pages):
        try:
            page.close()
        except Exception:
            pass


def _ensure_playwright() -> tuple[Playwright, BrowserContext]:
    global _pw, _context, _used_real_profile
    if _context and _pw:
        return _pw, _context
    with _lock:
        if _context and _pw:
            return _pw, _context
        _pw = sync_playwright().start()

        use_real = (
            EDGE_USER_DATA
            and EDGE_USER_DATA.exists()
        )

        if use_real:
            try:
                # Try to apply pending sync from previous session before starting
                _try_apply_pending_sync(_TEMP_SHARED)

                # Copy profile files that don't exist yet (first run only)
                _copy_profile_once(_TEMP_SHARED)
                _clear_restored_tabs(_TEMP_SHARED)

                _used_real_profile = True
                _context = _pw.chromium.launch_persistent_context(
                    str(_TEMP_SHARED),
                    channel="msedge",
                    headless=False,
                    viewport=_random_viewport(),
                    locale="zh-CN",
                    args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-session-crashed-bubble",
                    ],
                )
                _context.add_init_script(_STEALTH_INIT_JS)
            except Exception:
                # Fallback to clean context
                _used_real_profile = False
                browser = _pw.chromium.launch(channel="msedge", headless=False)
                _context = browser.new_context(
                    viewport=_random_viewport(),
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
                    ),
                    locale="zh-CN",
                )
                _context.add_init_script(_STEALTH_INIT_JS)
        else:
            # No Edge profile found — clean context
            _used_real_profile = False
            browser = _pw.chromium.launch(channel="msedge", headless=False)
            _context = browser.new_context(
                viewport=_random_viewport(),
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
                ),
                locale="zh-CN",
            )
            _context.add_init_script(_STEALTH_INIT_JS)

        atexit.register(shutdown)
        return _pw, _context


def shutdown() -> None:
    global _pw, _context, _used_real_profile
    with _lock:
        if _context:
            # Check if any page in the shared context is captcha-frozen
            for page in _context.pages:
                if _is_page_captcha_frozen(page):
                    remaining = _CAPTCHA_FREEZE_SECONDS - (time.time() - _captcha_frozen.get(id(page), 0))
                    print(f"[search-fetch] 页面因验证码被冻结，{remaining:.0f}秒内不可关闭，等待人类处理", file=sys.stderr)
                    return
            try:
                _context.close()
            except Exception:
                pass
            # Try to sync cookies back; if Edge is running, save pending marker
            if not _sync_cookies_back(_TEMP_SHARED) and _is_edge_running():
                _save_pending_sync(_TEMP_SHARED)
                print("[search-fetch] Edge 正在运行，登录态将在下次 Edge 关闭时自动同步", file=sys.stderr)
        try:
            if _pw:
                _pw.stop()
        except Exception:
            pass
        _context = None
        _pw = None
        _used_real_profile = False


def is_using_real_profile() -> bool:
    """Return True if using the user's real Edge profile (with login state)."""
    return _used_real_profile


def new_page() -> Page:
    """Create or reuse a page in the shared browser context."""
    _, ctx = _ensure_playwright()
    pages = [page for page in list(ctx.pages) if not page.is_closed()]
    if pages:
        return pages[0]
    return ctx.new_page()


# Isolated context for high-risk sites that must not share browser state
_isolated_pw: Playwright | None = None
_isolated_context: BrowserContext | None = None


def _sync_shared_to_isolated() -> None:
    """Copy recent login files from shared profile to isolated profile.

    This ensures that login sessions created via login_saver.py (shared context)
    are available to high-risk site samplers (isolated context).
    """
    import shutil
    shared_default = _TEMP_SHARED / "Default"
    isolated_default = _TEMP_ISOLATED / "Default"
    if not shared_default.exists():
        return
    isolated_default.mkdir(parents=True, exist_ok=True)
    login_files = ["Cookies", "Cookies-journal", "Login Data", "Login Data-journal", "Web Data", "Web Data-journal"]
    synced = 0
    for fname in login_files:
        src = shared_default / fname
        dst = isolated_default / fname
        if src.exists():
            try:
                shutil.copy2(str(src), str(dst))
                synced += 1
            except Exception:
                pass
    if synced:
        print(f"[search-fetch] 已从共享 Profile 同步 {synced} 个登录文件到独立 Profile", file=sys.stderr)


def _ensure_isolated() -> tuple[Playwright, BrowserContext]:
    """Get or create an isolated browser context with clean cookies but real login state.

    Copies the user's Edge profile to a temp directory so the original Edge
    remains usable while the script runs. The user can browse freely in their
    normal Edge without lockfile conflicts.

    Syncs login files from the shared profile first, so that login_saver.py
    sessions are available in the isolated context.
    """
    global _isolated_pw, _isolated_context
    if _isolated_context and _isolated_pw:
        return _isolated_pw, _isolated_context
    with _lock:
        if _isolated_context and _isolated_pw:
            return _isolated_pw, _isolated_context

        _isolated_pw = sync_playwright().start()

        if EDGE_USER_DATA and EDGE_USER_DATA.exists():
            # Try to apply pending sync from previous session
            _try_apply_pending_sync(_TEMP_ISOLATED)

            # Copy profile files that don't exist yet (first run only)
            _copy_profile_once(_TEMP_ISOLATED)

            # Sync login cookies from shared context (where login_saver writes)
            _sync_shared_to_isolated()

        _isolated_context = _isolated_pw.chromium.launch_persistent_context(
            str(_TEMP_ISOLATED),
            channel="msedge",
            headless=False,
            viewport=_random_viewport(),
            locale="zh-CN",
        )
        _isolated_context.add_init_script(_STEALTH_INIT_JS)
        return _isolated_pw, _isolated_context


def new_isolated_page() -> Page:
    """Create a new page in an isolated browser context (separate from shared context).

    Use for high-risk sites (e.g. Douyin) that may detect bot behavior from
    cross-site cookies/history in the shared context.
    """
    _, ctx = _ensure_isolated()
    return ctx.new_page()


def close_isolated_context() -> None:
    """Close the isolated browser context and stop its Playwright instance.

    Skips closing if any page in the context is captcha-frozen.
    Tries to sync cookies back; saves pending marker if Edge is running.
    """
    global _isolated_context, _isolated_pw
    with _lock:
        if _isolated_context:
            # Check if any page in this context is captcha-frozen
            for page in _isolated_context.pages:
                if _is_page_captcha_frozen(page):
                    remaining = _CAPTCHA_FREEZE_SECONDS - (time.time() - _captcha_frozen.get(id(page), 0))
                    print(f"[search-fetch] 独立浏览器页面因验证码被冻结，{remaining:.0f}秒内不可关闭，等待人类处理", file=sys.stderr)
                    return
            try:
                _isolated_context.close()
            except Exception:
                pass
            # Try to sync cookies back; if Edge is running, save pending marker
            if not _sync_cookies_back(_TEMP_ISOLATED) and _is_edge_running():
                _save_pending_sync(_TEMP_ISOLATED)
                print("[search-fetch] Edge 正在运行，独立上下文的登录态将在下次 Edge 关闭时自动同步", file=sys.stderr)
            _isolated_context = None
        if _isolated_pw:
            try:
                _isolated_pw.stop()
            except Exception:
                pass
            _isolated_pw = None


def human_type(page: Page, text: str, min_delay: int = 35, max_delay: int = 140) -> None:
    """Type text character by character with randomized delays, like a human."""
    for char in text:
        page.keyboard.type(char, delay=random.randint(min_delay, max_delay))


# Captcha freeze: pages with detected captchas are protected from closing for 10 min
_CAPTCHA_FREEZE_SECONDS = 600  # 10 minutes
_captcha_frozen: dict[int, float] = {}  # page hash -> freeze start time


def _is_page_captcha_frozen(page: Page) -> bool:
    pid = id(page)
    start = _captcha_frozen.get(pid)
    if start is None:
        return False
    if time.time() - start >= _CAPTCHA_FREEZE_SECONDS:
        _captcha_frozen.pop(pid, None)
        return False
    return True


def _freeze_page_for_captcha(page: Page) -> None:
    _captcha_frozen[id(page)] = time.time()


def _unfreeze_page(page: Page) -> None:
    _captcha_frozen.pop(id(page), None)


def close_page(page: Page) -> None:
    """Close a page, ignoring errors if already closed.

    If the page is captcha-frozen (human solving captcha), skip closing.
    """
    if _is_page_captcha_frozen(page):
        remaining = _CAPTCHA_FREEZE_SECONDS - (time.time() - _captcha_frozen.get(id(page), 0))
        print(f"[search-fetch] 页面因验证码被冻结，{remaining:.0f}秒内不可关闭，等待人类处理", file=sys.stderr)
        return
    try:
        page.close()
    except Exception:
        pass


def close_all_pages() -> None:
    """Close all open pages in the shared browser context.

    Skips captcha-frozen pages.
    """
    _, ctx = _ensure_playwright()
    for page in ctx.pages[:]:
        close_page(page)


def strip_target_blank(page: Page, selector: str = 'a[target="_blank"]') -> int:
    removed = page.evaluate(f"""(() => {{
      let count = 0;
      for (const a of document.querySelectorAll('{selector}')) {{
        if (a.target === '_blank') {{ a.removeAttribute('target'); count += 1; }}
      }}
      return count;
    }})()""")
    return int(removed) if isinstance(removed, (int, float)) else 0


def pop_newly_opened_page(context_pages_before: list[Page], current_page: Page) -> Page | None:
    _, ctx = _ensure_playwright()
    for p in ctx.pages:
        if p not in context_pages_before:
            return p
    return None


def scroll_pages(page: Page, min_pages: int = 5, pause_min: float = 1.0, pause_max: float = 2.5) -> int:
    """Scroll down at least `min_pages` full viewport heights for lazy-loaded content.

    Required before scraping high-risk sites (小红书, 抖音, B站, 贴吧, TapTap, etc.)
    to trigger lazy loading and gather more content before extracting data.
    Uses mouse.wheel() to generate real wheel events + scroll events, avoiding
    the detection pattern of window.scrollBy() which skips wheel events.
    """
    scrolled = 0
    for _ in range(min_pages):
        factor = random.uniform(0.3, 1.4)
        delta = int(page.evaluate("window.innerHeight") * factor)
        # Split into multiple small wheel ticks like a real user's scroll gesture
        ticks = random.randint(3, 6)
        remainder = delta % ticks
        for t in range(ticks):
            # ±30% variance per tick to avoid uniform deltas
            base = delta // ticks + (remainder if t == 0 else 0)
            d = int(base * random.uniform(0.7, 1.3))
            if d < 1:
                d = 1
            page.mouse.wheel(0, d)
            time.sleep(random.uniform(0.04, 0.2))
        time.sleep(random.uniform(pause_min, pause_max))
        scrolled += 1
    return scrolled


def human_mouse_move(page: Page, target_x: int, target_y: int, steps: int | None = None) -> None:
    """Move mouse to (target_x, target_y) via intermediate waypoints.

    Avoids teleporting directly to the target, which is a strong bot detection signal.
    Moves through a few random intermediate points to create a non-linear path,
    then lands near the target with a small random offset (humans don't land
    pixel-perfect).
    """
    if steps is None:
        steps = random.randint(4, 8)
    for _ in range(steps):
        rx = target_x + random.randint(-120, 120)
        ry = target_y + random.randint(-120, 120)
        page.mouse.move(rx, ry)
        time.sleep(random.uniform(0.05, 0.2))
    # Land near target with ±5px human imprecision
    final_x = target_x + random.randint(-5, 5)
    final_y = target_y + random.randint(-5, 5)
    page.mouse.move(final_x, final_y)
    time.sleep(random.uniform(0.02, 0.06))


def human_navigate_to_site(page: Page, site_name: str, expected_domain: str) -> dict[str, Any]:
    """Navigate to a high-risk site via Bing search + click, simulating human behavior.

    Direct URL navigation triggers bot detection on high-risk sites.
    This function:
    1. Goes to bing.com
    2. Types the site name character by character in the search box
    3. Clicks the link to the expected domain in search results
    """
    try:
        page.goto("https://www.bing.com", wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        return {"ok": False, "reason": "bing_navigation_failed", "error": str(exc)}

    time.sleep(1.5 + random.random())

    # Find and interact with Bing search box
    try:
        search_box = page.locator('textarea[name="q"], input[name="q"]').first
        search_box.click()
        time.sleep(0.3 + random.random() * 0.5)
        # Type site name character by character like a human
        for char in site_name:
            page.keyboard.type(char, delay=random.randint(40, 120))
        time.sleep(0.3 + random.random() * 0.5)
        page.keyboard.press("Enter")
    except Exception as exc:
        return {"ok": False, "reason": "bing_search_input_failed", "error": str(exc)}

    # Wait for Bing search results
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    time.sleep(2.0 + random.random())

    # Find link to expected domain — click via Playwright using matching text as selector
    domain_json = _json.dumps(expected_domain)
    js = f"""
    (() => {{
        const domain = {domain_json};
        const anchors = Array.from(document.querySelectorAll('a[href]'));
        // Bing wraps results in tracking URLs; check both href and text content
        const match = anchors.find(a => {{
            const h = a.href || '';
            const t = (a.innerText || '') + ' ' + (a.textContent || '');
            return h.includes(domain) || t.includes(domain);
        }});
        if (!match) return null;
        // Scroll into view and get bounding rect for Playwright click
        match.scrollIntoView({{block:'center'}});
        const r = match.getBoundingClientRect();
        const t = (match.innerText || '').trim();
        return {{
            href: match.href,
            text: t.slice(0, 80),
            left: Math.round(r.left),
            top: Math.round(r.top),
            width: Math.round(r.width),
            height: Math.round(r.height)
        }};
    }})()
    """
    result = evaluate_json(page, js)
    if not result or not isinstance(result, dict):
        return {"ok": False, "reason": "site_link_not_found_in_bing", "expected_domain": expected_domain}

    # Strip target="_blank" from ALL links so Bing results open in the same tab.
    # Without this, clicking a Bing result opens a new tab, which then causes
    # the "operating behind a tab" bug: the new tab covers the original page,
    # and all Playwright operations happen on the original (now hidden) tab.
    strip_target_blank(page)

    ctx_pages_before = page.context.pages[:]
    try:
        x = result["left"] + result["width"] // 2
        y = result["top"] + result["height"] // 2
        human_mouse_move(page, x, y)
        page.mouse.click(x, y)
    except Exception as exc:
        return {"ok": False, "reason": "playwright_click_failed", "error": str(exc)}

    # Wait for redirect to complete (Bing tracking → target site)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    time.sleep(2.0)

    current_url = page.url
    if expected_domain in current_url:
        return {"ok": True, "method": "bing_search_click", "url": current_url}

    # Defensive: close any new tabs that opened despite strip_target_blank
    # (e.g., JS-based window.open). This prevents the "operating behind" bug.
    new_pages = [p for p in page.context.pages if p not in ctx_pages_before]
    for new_page in new_pages:
        try:
            new_page.close()
        except Exception:
            pass

    # If the click didn't navigate the page (still on Bing), try waiting longer
    # for the tracking redirect chain to resolve.
    time.sleep(3.0)
    current_url = page.url
    if expected_domain in current_url:
        return {"ok": True, "method": "bing_search_click", "url": current_url}

    return {"ok": False, "reason": "wrong_domain_after_click", "url": current_url, "expected": expected_domain}


def evaluate_json(page: Page, js: str) -> Any:
    """Evaluate JS that returns JSON and parse it. Returns None on failure."""
    try:
        result = page.evaluate(js)
        if isinstance(result, str):
            import json
            return json.loads(result)
        return result
    except Exception:
        return None


def evaluate_json_list(page: Page, js: str) -> list:
    """Evaluate JS that returns a JSON list. Returns [] on failure."""
    result = evaluate_json(page, js)
    if isinstance(result, list):
        return result
    return []


# Captcha / challenge markers used across all high-risk sites
CAPTCHA_MARKERS = [
    "captcha", "challenge", "验证码", "请完成验证", "人机验证",
    "slider", "滑块", "请拖动", "图形验证", "access denied",
    "请输入验证码", "安全验证", "识别验证码",
    # 抖音/字节特有
    "请通过验证", "网络环境异常", "点击按住滑块", "向右拖动",
    "验证即可继续访问", "完成验证后继续",
]


def _detect_captcha(page: Page) -> bool:
    """Check if the current page shows a captcha/challenge.

    Checks both body text and page title.
    """
    text = ""
    try:
        text = page.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''") or ""
    except Exception:
        pass
    title = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    combined = (text + " " + title).lower()
    return any(marker.lower() in combined for marker in CAPTCHA_MARKERS)


def wait_for_captcha_or_proceed(page: Page, wait_seconds: float = 60.0) -> dict[str, Any]:
    """Wait for human to solve a captcha if one is present.

    If a captcha is detected, waits up to _CAPTCHA_FREEZE_SECONDS (10 min)
    for a human to solve it, regardless of the wait_seconds parameter.
    The page is frozen so callers cannot close it during solving.

    Returns:
        {"blocked": False} — no captcha detected, safe to proceed
        {"blocked": False, "solved": True} — captcha detected and solved
        {"blocked": True, "reason": "captcha_unsolved"} — captcha found but not solved in 10 min
    """
    if not _detect_captcha(page):
        return {"blocked": False}

    # Captcha detected — freeze page so caller cannot close it during solving
    _freeze_page_for_captcha(page)

    # Wait up to the freeze duration (10 min) for human to solve
    deadline = time.time() + _CAPTCHA_FREEZE_SECONDS
    while time.time() < deadline:
        time.sleep(2.0)
        if not _detect_captcha(page):
            _unfreeze_page(page)
            return {"blocked": False, "solved": True}

    # Captcha not solved within wait_seconds, but page stays frozen for the full freeze duration
    return {"blocked": True, "reason": "captcha_unsolved"}
