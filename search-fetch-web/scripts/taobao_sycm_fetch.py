#!/usr/bin/env python3
"""淘宝生意参谋（SYCM）数据抓取脚本。

使用真实 Edge profile 登录态访问生意参谋页面，提取表格/数据内容。
前提：用户已在 Edge 中登录 taobao.com / sycm.taobao.com。

用法:
    python3 scripts/taobao_sycm_fetch.py --url "<sycm_url>" [--wait 8] [--mode content|screenshot|both]
"""

import argparse
import json
import os
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path

# 确保能 import 同目录下的 _playwright_base
sys.path.insert(0, str(Path(__file__).parent))
from _playwright_base import (
    new_page, close_page, shutdown,
    scroll_pages, wait_for_captcha_or_proceed,
    human_type, is_using_real_profile,
)

DESKTOP = Path.home() / "Desktop"


def is_login_page(url: str, title: str = "") -> bool:
    login_indicators = ["login.taobao", "login.tmall", "havanalogin", "/login", "passport"]
    url_lower = url.lower()
    for ind in login_indicators:
        if ind in url_lower:
            return True
    if "登录" in title and "生意参谋" not in title:
        return True
    return False


def extract_table_data(page) -> list[dict]:
    """提取页面中的表格数据"""
    tables = page.evaluate("""() => {
        const results = [];
        const tables = document.querySelectorAll('table');
        for (const table of tables) {
            const headers = [];
            const rows = [];
            const ths = table.querySelectorAll('thead th, tr:first-child th');
            ths.forEach(th => headers.push(th.innerText.trim()));
            const trs = table.querySelectorAll('tbody tr');
            for (const tr of trs) {
                const cells = [];
                tr.querySelectorAll('td').forEach(td => cells.push(td.innerText.trim()));
                if (cells.length > 0) rows.push(cells);
            }
            if (headers.length > 0 || rows.length > 0) {
                results.push({headers, rows});
            }
        }
        return results;
    }""")
    return tables or []


def extract_page_text(page) -> str:
    """提取页面主要文本内容"""
    return page.evaluate("""() => {
        // 优先取主内容区
        const candidates = [
            '.content-area', '.main-content', '#content',
            '.oui-table-container', '.data-table',
            'main', '[class*="rank"]', '[class*="table"]',
            '.next-table', '.oui-table',
        ];
        for (const sel of candidates) {
            const el = document.querySelector(sel);
            if (el && el.innerText.trim().length > 50) {
                return el.innerText.trim();
            }
        }
        return document.body ? document.body.innerText.trim().slice(0, 20000) : '';
    }""")


def save_results(title: str, content: str, tables: list[dict], url: str) -> Path:
    safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:50].strip() or "sycm"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = DESKTOP / f"taobao_{safe_title}_{timestamp}.md"

    lines = [
        f"# {title}",
        "",
        f"> URL: {url}",
        f"> 抓取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "---",
        "",
    ]

    if tables:
        for i, table in enumerate(tables):
            lines.append(f"## 表格 {i+1}")
            lines.append("")
            if table.get("headers"):
                lines.append("| " + " | ".join(table["headers"]) + " |")
                lines.append("| " + " | ".join(["---"] * len(table["headers"])) + " |")
            for row in table.get("rows", []):
                lines.append("| " + " | ".join(row) + " |")
            lines.append("")
    else:
        lines.append(content)

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="淘宝生意参谋数据抓取")
    parser.add_argument("--url", required=True, help="生意参谋页面 URL")
    parser.add_argument("--wait", type=int, default=8, help="页面加载后额外等待秒数")
    parser.add_argument("--mode", default="both", choices=["content", "screenshot", "both"])
    args = parser.parse_args()

    print(f"========== 淘宝生意参谋抓取 ==========")
    print(f"URL: {args.url}")
    print(f"模式: {args.mode}")
    print(f"等待: {args.wait}s")
    print(f"Edge Profile: {'真实' if is_using_real_profile() else '清洁(无登录态)'}")
    print(f"======================================")

    page = new_page()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        print("[步骤 1] 导航到页面...")
        page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # 检查是否跳转到登录页
        cur_url = page.url
        cur_title = page.title()
        print(f"[信息] 当前 URL: {cur_url[:80]}")
        print(f"[信息] 页面标题: {cur_title}")

        if is_login_page(cur_url, cur_title):
            if not is_using_real_profile():
                print("[错误] 需要登录但当前使用清洁 profile（无登录态）。")
                print("[提示] 请先在 Edge 浏览器中手动登录 taobao.com，然后重试。")
                shot = str(DESKTOP / f"taobao_need_login_{timestamp}.png")
                page.screenshot(path=shot)
                print(f"[截图] {shot}")
                sys.exit(1)
            else:
                print("[警告] 使用了真实 profile 但仍跳转到登录页，可能 cookie 过期。")
                print("[提示] 请重新在 Edge 中登录 taobao.com / sycm.taobao.com。")
                shot = str(DESKTOP / f"taobao_login_expired_{timestamp}.png")
                page.screenshot(path=shot)
                print(f"[截图] {shot}")
                sys.exit(1)

        # 检查验证码
        captcha_result = wait_for_captcha_or_proceed(page)
        if captcha_result.get("blocked"):
            print("[错误] 验证码未解决")
            sys.exit(1)
        if captcha_result.get("solved"):
            print("[信息] 验证码已通过")

        # 等待页面渲染
        print(f"[步骤 2] 等待 {args.wait}s...")
        time.sleep(args.wait)

        # 滚动加载懒加载内容
        print("[步骤 3] 滚动页面加载数据...")
        scroll_pages(page, min_pages=3, pause_min=1.0, pause_max=2.0)
        time.sleep(2)

        title = page.title() or "sycm_page"

        if args.mode in ("content", "both"):
            print("[步骤 4] 提取数据...")
            tables = extract_table_data(page)
            content = extract_page_text(page)

            out_path = save_results(title, content, tables, page.url)
            print(f"\n[完成] 内容已保存: {out_path}")
            print(f"  表格数: {len(tables)}")
            print(f"  文本长度: {len(content)} 字符")

            # stdout 输出
            print("\n========== 页面内容 ==========")
            preview = content[:5000]
            print(preview)
            if len(content) > 5000:
                print(f"\n... (共 {len(content)} 字符，完整内容见文件)")

        if args.mode in ("screenshot", "both"):
            safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:50].strip()
            shot_path = DESKTOP / f"taobao_{safe_title}_{timestamp}.png"
            page.screenshot(path=str(shot_path), full_page=True)
            print(f"\n[完成] 截图已保存: {shot_path}")

    except Exception as e:
        print(f"\n[错误] {e}")
        import traceback
        traceback.print_exc()
        err_shot = str(DESKTOP / f"taobao_error_{timestamp}.png")
        try:
            page.screenshot(path=err_shot)
            print(f"[错误截图] {err_shot}")
        except:
            pass
        sys.exit(1)
    finally:
        close_page(page)
        shutdown()


if __name__ == "__main__":
    main()
