#!/usr/bin/env python3
"""HTML renderer for search-fetch results. Produces self-contained HTML reports."""

from __future__ import annotations

import html
import json as _json
import os as _os
import re
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _escape(s: Any) -> str:
    return html.escape(str(s) if s else "")


def _douyin_search_url(query: str) -> str:
    return f"https://www.douyin.com/search/{urllib.parse.quote(query)}"


def _douyin_video_url(href: str) -> str:
    if not href:
        return "#"
    if href.startswith("http"):
        return href
    return f"https://www.douyin.com{href}"


def _parse_interaction(val: str) -> float:
    """Parse interaction string like '4.1万' or '7936' to a numeric value for sorting."""
    if not val:
        return 0.0
    val = str(val).strip()
    try:
        if "万" in val:
            return float(val.replace("万", "")) * 10000
        return float(val)
    except ValueError:
        return 0.0


def _parse_cn_date_sort_value(val: str, now: datetime | None = None) -> int:
    """Return a comparable timestamp for Douyin date strings like '2小时前' or '4月1日'."""
    if now is None:
        now = datetime.now()
    text = str(val or "").strip()
    if not text:
        return 0
    patterns = [
        (r"(\d+)\s*秒前", lambda n: now - timedelta(seconds=n)),
        (r"(\d+)\s*分钟前", lambda n: now - timedelta(minutes=n)),
        (r"(\d+)\s*小时前", lambda n: now - timedelta(hours=n)),
        (r"(\d+)\s*天前", lambda n: now - timedelta(days=n)),
        (r"(\d+)\s*周前", lambda n: now - timedelta(weeks=n)),
        (r"(\d+)\s*月前", lambda n: now - timedelta(days=30 * n)),
        (r"(\d+)\s*年前", lambda n: now - timedelta(days=365 * n)),
    ]
    for pattern, maker in patterns:
        m = re.fullmatch(pattern, text)
        if m:
            return int(maker(int(m.group(1))).timestamp())
    if text in ("刚刚", "刚才"):
        return int(now.timestamp())
    if text == "今天":
        return int(datetime(now.year, now.month, now.day).timestamp())
    if text == "昨天":
        return int((datetime(now.year, now.month, now.day) - timedelta(days=1)).timestamp())
    if text == "前天":
        return int((datetime(now.year, now.month, now.day) - timedelta(days=2)).timestamp())
    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        try:
            return int(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp())
        except ValueError:
            return 0
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)))
            if dt > now + timedelta(days=1):
                dt = datetime(now.year - 1, int(m.group(1)), int(m.group(2)))
            return int(dt.timestamp())
        except ValueError:
            return 0
    return 0


CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Microsoft YaHei", sans-serif; background: #f5f6fa; color: #2d3436; padding: 24px; }
.container { max-width: 1400px; margin: 0 auto; }
h1 { font-size: 24px; margin-bottom: 8px; color: #1a1a2e; }
.subtitle { color: #636e72; font-size: 14px; margin-bottom: 20px; }
.stats { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
.stat-card { background: #fff; border-radius: 10px; padding: 16px 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); min-width: 120px; }
.stat-card .label { font-size: 12px; color: #636e72; text-transform: uppercase; letter-spacing: .5px; }
.stat-card .value { font-size: 28px; font-weight: 700; color: #1a1a2e; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
thead { background: #1a1a2e; color: #fff; }
th { padding: 12px 16px; text-align: left; font-size: 13px; font-weight: 600; letter-spacing: .3px; cursor: pointer; user-select: none; }
th:hover { background: #2d2d4e; }
th .sort-arrow { font-size: 10px; margin-left: 4px; opacity: .5; }
th.sorted .sort-arrow { opacity: 1; }
td { padding: 10px 16px; border-bottom: 1px solid #f0f0f0; font-size: 13px; vertical-align: top; }
tr:hover td { background: #f8f9ff; }
.col-num { width: 40px; color: #b2bec3; text-align: center; }
.col-title { max-width: 420px; }
.col-title a { color: #1a1a2e; text-decoration: none; font-weight: 500; line-height: 1.4; }
.col-title a:hover { color: #e74c3c; text-decoration: underline; }
.col-author { color: #636e72; white-space: nowrap; }
.col-date { color: #636e72; white-space: nowrap; font-size: 12px; }
.col-interaction { font-weight: 600; color: #e74c3c; white-space: nowrap; text-align: right; }
.col-duration { color: #636e72; white-space: nowrap; text-align: center; font-size: 12px; }
.col-price { font-weight: 700; color: #e74c3c; white-space: nowrap; text-align: right; font-size: 14px; }
.col-sales { font-weight: 600; color: #2d3436; white-space: nowrap; text-align: right; }
.col-shop { color: #636e72; white-space: nowrap; max-width: 140px; overflow: hidden; text-overflow: ellipsis; }
.col-location { color: #b2bec3; white-space: nowrap; text-align: center; font-size: 12px; }
.tag-live { display: inline-block; background: #e74c3c; color: #fff; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 6px; }
.search-link { display: inline-block; margin-top: 20px; color: #0984e3; text-decoration: none; font-size: 14px; }
.search-link:hover { text-decoration: underline; }
.footer { margin-top: 24px; color: #b2bec3; font-size: 12px; }
"""

JS_SORT = """
<script>
(function() {
  const getNum = (row, cls) => {
    const cell = row.querySelector('.' + cls);
    if (!cell) return 0;
    const t = cell.textContent.trim().replace(/[¥,]/g, '');
    if (!t) return 0;
    const n = parseFloat(t.replace(/[^0-9.]/g, ''));
    return t.includes('万') ? n * 10000 : n;
  };
  const getDuration = (row) => {
    const cell = row.querySelector('.col-duration');
    if (!cell) return 0;
    const parts = cell.textContent.trim().split(':');
    if (parts.length === 2) return parseInt(parts[0]) * 60 + parseInt(parts[1]);
    return 0;
  };
  const sortTable = (colIndex, type, th) => {
    const tbody = document.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const isAsc = th.classList.contains('asc');
    document.querySelectorAll('th').forEach(h => { h.classList.remove('sorted','asc','desc'); });
    th.classList.add('sorted', isAsc ? 'desc' : 'asc');
    rows.sort((a, b) => {
      let va, vb;
      if (type === 'interaction' || type === 'sales') { va = getNum(a, 'col-interaction'); vb = getNum(b, 'col-interaction'); if (!va) { va = getNum(a, 'col-sales'); vb = getNum(b, 'col-sales'); } }
      else if (type === 'price') { va = getNum(a, 'col-price'); vb = getNum(b, 'col-price'); }
      else if (type === 'duration') { va = getDuration(a); vb = getDuration(b); }
      else if (type === 'date') { va = parseInt(a.cells[colIndex]?.dataset.sortValue || '0', 10); vb = parseInt(b.cells[colIndex]?.dataset.sortValue || '0', 10); }
      else if (type === 'rank') { va = parseInt(a.cells[colIndex]?.textContent.trim() || '0', 10); vb = parseInt(b.cells[colIndex]?.textContent.trim() || '0', 10); }
      else { va = a.cells[colIndex]?.textContent.trim() || ''; vb = b.cells[colIndex]?.textContent.trim() || ''; }
      if (typeof va === 'number') return isAsc ? va - vb : vb - va;
      return isAsc ? String(vb).localeCompare(String(va)) : String(va).localeCompare(String(vb));
    });
    rows.forEach(r => tbody.appendChild(r));
  };
  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.addEventListener('click', () => sortTable(parseInt(th.dataset.col), th.dataset.sort, th));
  });
})();
</script>
"""


def render_douyin_cards(result: dict, output_path: str | None = None) -> str:
    """Render douyin cards/run results as a self-contained HTML page."""
    items = result.get("video_cards") or result.get("cards", {}).get("items") or result.get("items") or []
    query = result.get("query", "")
    target = result.get("target_count", len(items))
    ok = result.get("ok", False)
    mode = result.get("mode", "douyin.run")
    flow = (result.get("cards") or {}).get("flow_evidence") or {}

    # Stats
    total_interaction = sum(_parse_interaction(it.get("interaction", "")) for it in items)
    live_count = sum(1 for it in items if it.get("text", "").startswith("直播中"))
    author_set = {it.get("author", "") for it in items if it.get("author")}

    rows_html: list[str] = []
    generated_at = datetime.now()
    for i, it in enumerate(items, 1):
        title = it.get("title", "")
        author = it.get("author", "")
        date = it.get("date", "")
        date_sort = _parse_cn_date_sort_value(date, generated_at)
        interaction = it.get("interaction", "")
        duration = it.get("duration", "")
        href = it.get("href", "")
        text = it.get("text", "")
        is_live = text.startswith("直播中")

        url = _douyin_video_url(href)
        title_html = _escape(title)
        if is_live:
            title_html += ' <span class="tag-live">直播</span>'

        rows_html.append(
            f'<tr>'
            f'<td class="col-num">{i}</td>'
            f'<td class="col-title"><a href="{_escape(url)}" target="_blank" rel="noopener">{title_html}</a></td>'
            f'<td class="col-author">{_escape(author)}</td>'
            f'<td class="col-date" data-sort-value="{date_sort}">{_escape(date)}</td>'
            f'<td class="col-interaction">{_escape(interaction)}</td>'
            f'<td class="col-duration">{_escape(duration) if duration else "-"}</td>'
            f'</tr>'
        )

    scroll_rounds = flow.get("scroll_rounds", "")
    raw_candidates = flow.get("raw_candidate_count", "")

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>抖音搜索: {_escape(query)} — 抓取报告</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
<h1>抖音搜索抓取报告</h1>
<p class="subtitle">关键词: <strong>{_escape(query)}</strong> &nbsp;|&nbsp; 模式: {_escape(mode)} &nbsp;|&nbsp; 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>

<div class="stats">
<div class="stat-card"><div class="label">抓取结果</div><div class="value">{len(items)} / {target}</div></div>
<div class="stat-card"><div class="label">状态</div><div class="value">{'OK' if ok else '未达标'}</div></div>
<div class="stat-card"><div class="label">总互动量</div><div class="value">{_format_num(total_interaction)}</div></div>
<div class="stat-card"><div class="label">作者数</div><div class="value">{len(author_set)}</div></div>
<div class="stat-card"><div class="label">直播中</div><div class="value">{live_count}</div></div>
{f'<div class="stat-card"><div class="label">滚动轮次</div><div class="value">{scroll_rounds}</div></div>' if scroll_rounds else ''}
{f'<div class="stat-card"><div class="label">原始候选</div><div class="value">{raw_candidates}</div></div>' if raw_candidates else ''}
</div>

<table>
<thead>
<tr>
<th class="col-num sorted asc" data-sort="rank" data-col="0"># <span class="sort-arrow">▲</span></th>
<th class="col-title" data-sort="title" data-col="1">标题</th>
<th data-sort="author" data-col="2">作者</th>
<th data-sort="date" data-col="3">日期</th>
<th data-sort="interaction" data-col="4">互动</th>
<th data-sort="duration" data-col="5">时长</th>
</tr>
</thead>
<tbody>
{chr(10).join(rows_html)}
</tbody>
</table>

<a class="search-link" href="{_douyin_search_url(query)}" target="_blank" rel="noopener">→ 在抖音中打开搜索</a>
<div class="footer">Generated by search-fetch CLI &mdash; {datetime.now().isoformat()}</div>
</div>
{JS_SORT}
</body>
</html>"""

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)

    return html_content


def _format_num(n: float) -> str:
    if n >= 10000:
        return f"{n/10000:.1f}万"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(int(n))


def _parse_sales(val: str) -> float:
    """Parse sales string like '已售2万+件' or '已售5000+件' to numeric."""
    if not val:
        return 0.0
    val = str(val).strip()
    try:
        if "万" in val:
            num = val.replace("已售", "").replace("万", "").replace("+", "").replace("件", "").replace("人付款", "").strip()
            return float(num) * 10000
        num = val.replace("已售", "").replace("+", "").replace("件", "").replace("人付款", "").strip()
        return float(num)
    except ValueError:
        return 0.0


def _parse_price(val: str) -> float:
    """Parse price string like '¥399' or '¥2699' to numeric."""
    if not val:
        return 0.0
    val = str(val).replace("¥", "").replace(",", "").split(".")[0].strip()
    try:
        return float(val)
    except ValueError:
        return 0.0


def _taobao_search_url(query: str) -> str:
    return f"https://s.taobao.com/search?q={urllib.parse.quote(query)}"


def _marketplace_search_url(query: str, mode: str) -> str:
    if mode.startswith("tmall."):
        return f"https://list.tmall.com/search_product.htm?q={urllib.parse.quote(query)}"
    if mode.startswith("jd."):
        return f"https://search.jd.com/Search?keyword={urllib.parse.quote(query)}&enc=utf-8"
    return _taobao_search_url(query)


def render_taobao_cards(result: dict, output_path: str | None = None) -> str:
    """Render marketplace product cards/run results as a self-contained HTML page."""
    items = result.get("product_cards") or result.get("cards", {}).get("items") or result.get("items") or []
    query = result.get("query", "")
    target = result.get("target_count", len(items))
    ok = result.get("ok", False)
    platform = str(result.get("platform") or "").lower()
    mode = result.get("mode") or (f"{platform}.cards" if platform else "taobao.run")
    label = "京东" if mode.startswith("jd.") or platform == "jd" else ("天猫" if mode.startswith("tmall.") or platform == "tmall" else "淘宝")

    total_sales = sum(_parse_sales(it.get("sales", "")) for it in items)
    shop_set = {it.get("shop", "") for it in items if it.get("shop")}
    parsed_prices = [_parse_price(it.get("price", "0")) for it in items if it.get("price")]
    parsed_prices = [price for price in parsed_prices if price > 0]
    price_min = min(parsed_prices, default=0)
    price_max = max(parsed_prices, default=0)

    rows_html: list[str] = []
    for i, it in enumerate(items, 1):
        title = it.get("title", "")
        price = it.get("price", "")
        sales = it.get("sales", "")
        shop = it.get("shop", "")
        location = it.get("location", "")
        href = it.get("href", "")

        title_attr = f' title="{_escape(href)}"' if href else ""
        rows_html.append(
            f'<tr>'
            f'<td class="col-num">{i}</td>'
            f'<td class="col-title"><a href="{_escape(href) if href else "#"}" target="_blank" rel="noopener"{title_attr}>{_escape(title)}</a></td>'
            f'<td class="col-price">{_escape(price)}</td>'
            f'<td class="col-sales">{_escape(sales)}</td>'
            f'<td class="col-shop">{_escape(shop) if shop else "-"}</td>'
            f'<td class="col-location">{_escape(location) if location else "-"}</td>'
            f'</tr>'
        )

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_escape(label)}搜索: {_escape(query)} — 抓取报告</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
<h1>{_escape(label)}搜索抓取报告</h1>
<p class="subtitle">关键词: <strong>{_escape(query)}</strong> &nbsp;|&nbsp; 模式: {_escape(mode)} &nbsp;|&nbsp; 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>

<div class="stats">
<div class="stat-card"><div class="label">抓取结果</div><div class="value">{len(items)} / {target}</div></div>
<div class="stat-card"><div class="label">状态</div><div class="value">{'OK' if ok else '未达标'}</div></div>
<div class="stat-card"><div class="label">总销量</div><div class="value">{_format_num(total_sales)}</div></div>
<div class="stat-card"><div class="label">店铺数</div><div class="value">{len(shop_set)}</div></div>
<div class="stat-card"><div class="label">价格区间</div><div class="value" style="font-size:18px;">¥{int(price_min)} ~ ¥{int(price_max)}</div></div>
</div>

<table>
<thead>
<tr>
<th class="col-num">#</th>
<th class="col-title" data-sort="title" data-col="1">商品</th>
<th class="sorted desc" data-sort="price" data-col="2">价格 <span class="sort-arrow">▼</span></th>
<th data-sort="sales" data-col="3">销量</th>
<th data-sort="shop" data-col="4">店铺</th>
<th data-sort="location" data-col="5">发货地</th>
</tr>
</thead>
<tbody>
{chr(10).join(rows_html)}
</tbody>
</table>

<a class="search-link" href="{_marketplace_search_url(query, mode)}" target="_blank" rel="noopener">→ 打开原始搜索</a>
<div class="footer">Generated by search-fetch CLI &mdash; {datetime.now().isoformat()}</div>
</div>
{JS_SORT}
</body>
</html>"""

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)

    return html_content


def render_generic(result: dict, output_path: str | None = None) -> str:
    """Fallback HTML renderer for any result dict."""
    query = result.get("query", "")
    mode = result.get("mode", "unknown")
    ok = result.get("ok", False)

    # Try to find items in common places
    items = (
        result.get("video_cards")
        or result.get("cards", {}).get("items")
        or result.get("items")
        or []
    )

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>搜索抓取报告: {_escape(query)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
<h1>搜索抓取报告</h1>
<p class="subtitle">关键词: <strong>{_escape(query)}</strong> &nbsp;|&nbsp; 模式: {_escape(mode)} &nbsp;|&nbsp; 生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
<div class="stats">
<div class="stat-card"><div class="label">结果数</div><div class="value">{len(items)}</div></div>
<div class="stat-card"><div class="label">状态</div><div class="value">{'OK' if ok else '未达标'}</div></div>
</div>
<pre style="background:#fff;padding:20px;border-radius:10px;overflow:auto;font-size:12px;line-height:1.5;">{_escape(_json.dumps(result, ensure_ascii=False, indent=2))}</pre>
<div class="footer">Generated by search-fetch CLI</div>
</div>
</body>
</html>"""

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)

    return html_content


def render(result: dict, output_dir: str | None = None) -> str:
    """Auto-detect result type and render HTML. Returns the file path."""
    mode = result.get("mode", "")
    query = result.get("query", "result")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if output_dir is None:
        output_dir = str(Path(__file__).resolve().parent.parent / ".data" / "html_reports")
    _os.makedirs(output_dir, exist_ok=True)

    safe_query = "".join(c if c.isalnum() or c in "._- " else "_" for c in query)[:40].strip().replace(" ", "_")
    fname = f"{safe_query}_{ts}.html"
    output_path = _os.path.join(output_dir, fname)

    if mode.startswith("douyin."):
        render_douyin_cards(result, output_path)
    elif mode.startswith(("taobao.", "tmall.", "jd.")):
        render_taobao_cards(result, output_path)
    else:
        render_generic(result, output_path)

    return output_path


# Allow running standalone for testing
if __name__ == "__main__":
    import json as _json
    test = {
        "mode": "douyin.run",
        "ok": True,
        "query": "极萌水光",
        "target_count": 100,
        "card_count": 3,
        "video_cards": [
            {"title": "测试视频1 #极萌", "author": "@test1", "date": "5月1日", "interaction": "1.2万", "duration": "01:23", "href": "/video/123", "text": "01:23\n1.2万\n测试视频1"},
            {"title": "直播测试", "author": "@test2", "date": "", "interaction": "直播测试", "duration": "", "href": "", "text": "直播中\n直播测试\n@test2"},
            {"title": "普通视频", "author": "@test3", "date": "4月20日", "interaction": "456", "duration": "00:30", "href": "/video/789", "text": "00:30\n456\n普通视频"},
        ],
        "cards": {"flow_evidence": {"scroll_rounds": 5, "raw_candidate_count": 50}},
    }
    path = render(test)
    print(f"HTML saved to: {path}")
