#!/usr/bin/env python3
"""通用登录态保存工具。

弹出 Edge 浏览器窗口，导航到指定站点，等待用户手动登录。
登录完成后自动检测并保存 cookie 到 Edge profile，下次脚本直接复用。

用法:
    python3 scripts/login_saver.py --url "https://www.douyin.com" --wait-text "推荐"
    python3 scripts/login_saver.py --url "https://sycm.taobao.com" --wait-text "生意参谋"
    python3 scripts/login_saver.py --url "https://www.douyin.com"  # 不指定 wait-text 则等 120 秒
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _playwright_base import (
    new_page, new_isolated_page, close_page, shutdown, is_using_real_profile,
)


def main():
    parser = argparse.ArgumentParser(description="保存登录态到 Edge profile")
    parser.add_argument("--url", required=True, help="目标站点 URL")
    parser.add_argument("--wait-text", default="", help="登录成功后页面应包含的文本（用于自动检测登录完成）")
    parser.add_argument("--timeout", type=int, default=120, help="最长等待秒数（默认 120）")
    parser.add_argument("--manual", action="store_true", help="手动确认模式：用户登录后按 Enter 确认，不做自动检测")
    parser.add_argument("--isolated", action="store_true", help="使用独立 Profile（抖音等高风险站点必须使用，避免登录态同步问题）")
    args = parser.parse_args()
    run(url=args.url, wait_text=args.wait_text, timeout=args.timeout, manual=args.manual, isolated=args.isolated)


def run(url: str, wait_text: str = "", timeout: int = 120, manual: bool = False, isolated: bool = False) -> None:
    print(f"========== 登录态保存 ==========")
    print(f"URL: {url}")
    mode_desc = "手动确认" if manual else (f"检测文本: '{wait_text}'" if wait_text else "检测登录入口消失")
    print(f"检测模式: {mode_desc}")
    print(f"超时: {timeout}s")
    print(f"Edge Profile: {'真实' if is_using_real_profile() else '清洁'}")
    print(f"=================================")
    print()
    print(">>> 浏览器窗口已打开，请在窗口中手动登录 <<<")
    if not manual:
        print(">>> 登录完成后自动检测，无需手动操作 <<<")
    print()

    page = new_isolated_page() if isolated else new_page()

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        if manual:
            print()
            input(">>> 请在浏览器中完成登录后，回到此处按 Enter 继续...")
            logged_in = True
        else:
            start = time.time()
            logged_in = False
            seen_login_entry = False  # 追踪是否曾检测到登录入口

            # 判断是否导航到了登录页（URL 含 login/passport）
            login_url_patterns = ["login", "passport", "signin", "sign_in"]
            on_login_page = any(p in page.url.lower() for p in login_url_patterns)

            while time.time() - start < timeout:
                time.sleep(3)
                elapsed = int(time.time() - start)

                try:
                    cur_url = page.url
                    cur_title = page.title() or ""
                except Exception:
                    print(f"[{elapsed}s] 页面正在加载...")
                    continue

                if wait_text:
                    # 指定了 wait-text：检测文本是否出现
                    try:
                        body_text = page.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''") or ""
                    except Exception:
                        body_text = ""
                    if wait_text in body_text or wait_text in cur_title:
                        logged_in = True
                        print(f"\n[{elapsed}s] 检测到 \"{wait_text}\"，登录成功!")
                        break
                elif on_login_page:
                    # 在登录页：检测 URL 是否离开登录页（登录成功会自动跳转）
                    still_on_login = any(p in cur_url.lower() for p in login_url_patterns)
                    if not still_on_login:
                        logged_in = True
                        print(f"\n[{elapsed}s] 已离开登录页 → {cur_url[:80]}，登录成功!")
                        break
                    print(f"[{elapsed}s] 等待登录... (登录页: {cur_url[:60]})")
                else:
                    # 不在登录页：用 JS 检测登录入口是否消失 或 登录态元素是否出现
                    has_entry = _has_login_entry(page)
                    has_indicator = _has_logged_in_indicator(page)

                    if has_indicator:
                        logged_in = True
                        print(f"\n[{elapsed}s] 检测到登录态元素，登录成功!")
                        break

                    if has_entry:
                        seen_login_entry = True
                        print(f"[{elapsed}s] 等待登录... (登录入口仍存在)")
                    elif seen_login_entry and not has_entry:
                        logged_in = True
                        print(f"\n[{elapsed}s] 登录入口已消失，登录成功! (URL: {cur_url[:80]})")
                        break
                    else:
                        print(f"[{elapsed}s] 等待登录... (未检测到登录入口或登录态元素)")

        if logged_in:
            time.sleep(3)
            print()
            print("=" * 40)
            print("Cookie 已保存到 Edge profile!")
            print("下次运行抓取脚本时将自动使用此登录态。")
            print("=" * 40)
        else:
            print()
            print(f"[超时] {timeout} 秒内未检测到登录完成。")
            print("如果你已经登录成功，cookie 也已保存（persistent context 自动持久化）。")

    except Exception as e:
        print(f"\n[错误] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        close_page(page)
        shutdown()


def _has_login_entry(page) -> bool:
    """用 JS 检测页面上是否还存在可见的"登录"入口。

    Returns True if a visible login button/link is found, False otherwise.
    """
    js = """(() => {
        // 查找可见的「登录」入口按钮/链接
        const loginKeywords = ['登录', 'login', 'sign in', 'signin'];
        const candidates = Array.from(document.querySelectorAll(
            'a, button, span, div[class*="login"], div[class*="Login"]'
        ));
        for (const el of candidates) {
            const text = (el.textContent || '').trim().toLowerCase();
            // 必须是短文本（导航按钮），排除正文内容
            if (text.length > 10) continue;
            // 检查是否可见
            if (el.offsetParent === null) continue;
            for (const kw of loginKeywords) {
                if (text === kw || text === kw + '/注册' || text === '请' + kw) {
                    return { found: true, text: el.textContent.trim().slice(0, 20) };
                }
            }
        }
        return { found: false };
    })()"""
    try:
        result = page.evaluate(js)
        return bool(result.get("found"))
    except Exception:
        return False


def _has_logged_in_indicator(page) -> bool:
    """用 JS 检测页面上是否出现登录态专属元素（退出链接、用户头像等）。

    作为 _has_login_entry 的补充——当登录入口检测不到时，用正向指标判断。
    """
    js = """(() => {
        // 检测「退出」/「注销」链接 — 登录后才可能出现的元素
        const logoutKws = ['退出', '注销', 'logout'];
        const allEls = Array.from(document.querySelectorAll('a, span, div, button'));
        for (const el of allEls) {
            const text = (el.textContent || '').trim();
            if (text.length > 10) continue;
            if (el.offsetParent === null) continue;
            for (const kw of logoutKws) {
                if (text.includes(kw)) return { found: true, type: 'logout', text: text };
            }
        }
        // 检测用户头像（header 中登录后才出现的 avatar）
        const avatars = Array.from(document.querySelectorAll(
            'header img[class*="avatar"], .site-nav img[src], img[class*="avatar"], img[alt*="头像"]'
        ));
        for (const img of avatars) {
            if (img.offsetParent !== null && img.naturalWidth > 20) {
                return { found: true, type: 'avatar' };
            }
        }
        return { found: false };
    })()"""
    try:
        result = page.evaluate(js)
        return bool(result.get("found"))
    except Exception:
        return False


if __name__ == "__main__":
    main()
