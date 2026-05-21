---
name: search-fetch-web
description: 在搜索资料、网页检索、先搜再抓、抓取网页正文/评论/帖子/页面信息、处理 Google/Reddit/小红书/抖音/B站/X/Cloudflare 等高风险站点、需要 Playwright 真实浏览器、需要降低被判定为机器人的风险时使用。优先把执行收口到本 skill 的 CLI / scripts，而不是在对话里手工拼抓取步骤。
---

# Search Fetch

把这个 skill 当成 **路由层 + 规则层**。

- **SKILL.md 负责：** 触发条件、红线、CLI 入口、成功/失败判定口径
- **脚本 / CLI 负责：** 参数解析、执行流程、节流、冷却、检查清单、成功判定、JSON 输出

## 首次使用（Setup）

- **执行环境检测**：每次执行脚本前，必须先运行版本兼容性检查（见下方）。若环境不满足，先安装依赖再继续。
- 先确认 Playwright 已安装（`pip install playwright`）且 Edge 浏览器可用。
- 运行 `playwright install msedge` 安装 Edge 的 Playwright 驱动。
- **Playwright 启动策略**：`_playwright_base.py` 使用独立的临时 Profile（`%TEMP%/playwright_edge_shared`），**不会关闭或影响用户当前正在运行的 Edge 浏览器**。首次运行时从真实 Edge Profile 复制登录文件到临时目录，后续运行直接使用临时目录积累的登录态。脚本退出时尝试同步 cookie 回真实 Edge Profile（若 Edge 正在运行则保存 pending-sync 标记，下次启动时同步）。
- B站评论链路默认依赖 `scripts/bilibili_cookie_refresh.py` 刷新/校验 cookie；先在 Edge 中登录 bilibili.com，再跑刷新脚本。
- 小红书依赖 Edge 真实 profile 的登录态。**运行 XHS CLI 前确认 Edge 中已登录 xiaohongshu.com**，否则搜索页会触发登录墙。
- 淘宝/生意参谋依赖 Edge 真实 profile 的登录态。**运行前确认 Edge 中已登录 taobao.com / sycm.taobao.com**。
- Setup 阶段的目标是把 Playwright + cookie 链路准备好，不是在文档里承诺任何固定的评论接口直连方案。

## 登录态管理（强制流程）

**每次执行抓取前，必须按以下流程处理登录态：**

### 通用规则

1. **首次抓取某站点**：必须先提示用户登录，用户在浏览器窗口中手动完成登录后，脚本自动检测并保存登录态到临时 Profile。后续重跑同站点无需再次登录。
2. **后续运行**：自动读取临时 Profile 中的登录态，无需用户干预。
3. **登录态过期检测**：如果抓取过程中检测到登录墙（跳转到登录页、出现"请登录"提示等），立即停止抓取，提示用户登录态已过期，需要重新登录。
4. **用户手动登录工具**：`python scripts/search-fetch login --url "<站点URL>" --wait-text "<登录成功后页面文本>"`

### 各站点登录要求

| 站点 | 登录地址 | 检测方式 | 说明 |
|------|----------|----------|------|
| 抖音 | `https://www.douyin.com` | 检测"登录"入口消失 | **必须加 `--isolated`**，douyin_sampler 使用独立 Profile，login_saver 也需写入同一 Profile |
| 淘宝 | `https://login.taobao.com/member/login.jhtml` | **URL 跳转检测** | 导航到登录页，登录成功后自动跳转 |
| 天猫 | `https://login.tmall.com` | **URL 跳转检测** | 同上 |
| B站 | `https://passport.bilibili.com/login` | **URL 跳转检测** | 同上 |
| 生意参谋 | `https://sycm.taobao.com` | `--wait-text "生意参谋"` | 需先在淘宝登录 |
| 小红书 | `https://www.xiaohongshu.com` | `--wait-text "发现"` | 小红书登录态是硬性前提 |
| 贴吧 | `https://tieba.baidu.com` | 检测"登录"入口消失 | 贴吧登录后入口消失 |

**核心机制**：`login_saver.py` 自动判断 URL 是否为登录页（含 `login`/`passport`/`signin`）：
- **是登录页** → 等待 URL 离开登录页（登录成功自动跳转），无需轮询 DOM
- **不是登录页** → JS 轮询检测"登录"入口是否消失 或 登录态元素是否出现
| 生意参谋 | `https://sycm.taobao.com` | `生意参谋` | 需先在淘宝登录，再访问生意参谋 |
| B站 | `https://www.bilibili.com` | `首页` | 需在 Edge 中登录 B站 账号 |
| 小红书 | `https://www.xiaohongshu.com` | `发现` | 小红书登录态是硬性前提，未登录直接显示登录墙 |
| 贴吧 | `https://tieba.baidu.com` | `首页` | 需在 Edge 中登录百度账号 |

### 登录执行命令

```bash
# 抖音 — 必须用 --isolated（douyin_sampler 使用独立 Profile）
python scripts/search-fetch login --url "https://www.douyin.com" --timeout 180 --isolated

# 淘宝 — 登录页，等待自动跳转
python scripts/search-fetch login --url "https://login.taobao.com/member/login.jhtml" --timeout 180

# 天猫 — 登录页，等待自动跳转
python scripts/search-fetch login --url "https://login.tmall.com" --timeout 180

# 生意参谋 — 指定登录后页面文本
python scripts/search-fetch login --url "https://sycm.taobao.com" --wait-text "生意参谋" --timeout 180

# B站 — 登录页，等待自动跳转
python scripts/search-fetch login --url "https://passport.bilibili.com/login" --timeout 180

# 小红书
python scripts/search-fetch login --url "https://www.xiaohongshu.com" --wait-text "发现"

# 贴吧 — 首页检测登录入口消失
python scripts/search-fetch login --url "https://tieba.baidu.com" --timeout 180
```

**交互流程：**
1. 提示用户："需要在 XX 站点登录，我会打开浏览器窗口，请在窗口中手动完成登录"
2. 启动 Edge 浏览器窗口（使用临时 Profile，不影响用户当前浏览器）
3. 导航到目标登录页
4. 用户在浏览器中手动完成登录（含扫码/验证码等）
5. 脚本自动检测登录完成（`--wait-text` 文本匹配或 URL 离开登录页）
6. **登录检测成功后，向用户确认：** "检测到登录成功，确认可以关闭窗口吗？" — 避免淘宝等站点的导航栏文字导致误判
7. Cookie 自动持久化到临时 Profile
8. 登录完成后继续执行抓取流程

**已知坑：**
- **抖音登录必须加 `--isolated`**：`douyin_sampler` 使用独立 Profile（`_TEMP_ISOLATED`），而 `login_saver` 默认写共享 Profile（`_TEMP_SHARED`）。不加 `--isolated` 会导致登录态写入错误的 Profile，抓取时读不到。
- **避免用主页 URL 做登录检测**：淘宝 `taobao.com`、天猫 `tmall.com` 未登录也会显示完整主页，JS 检测登录入口不可靠。**必须用登录页 URL**，依赖登录成功后的自动跳转来检测。
- **`--wait-text` 文本可能在未登录页就存在**：如抖音"推荐"、淘宝"我的淘宝"都是导航栏固定文本，不可用作登录检测文本。
- **不要刷新页面**：刷新会打断用户的登录操作。
- **抖音抓取必须走 CLI 入口**：排查/重跑抖音 `cards/run/deep` 时也必须先按版本检查，再调用 `scripts/search-fetch douyin ...` 或等价的 `scripts/search_fetch_cli.py douyin ...`。不要为了省事在对话里直接 `import douyin_sampler` 调用 `cards()`，否则容易绕过 CLI 的 HTML 输出、调度证据、编码处理和失败口径。
- **天猫首页搜索可能弹新页**：天猫搜索按钮/表单可能带 `target=_blank` 或触发 popup。实现层必须在提交前移除 `a/form[target]`、强制同页提交；如果仍弹出新页，必须接管新页 URL 回当前页并关闭弹页。禁止在同一次首页搜索中循环 Enter/反复点击多个搜索按钮，否则会连续开页并显著提高风控风险。

## 版本兼容性检查

**每次执行脚本前，必须先运行以下版本检查**（若失败则先修复再继续）：

```bash
python -c "from playwright.sync_api import sync_playwright; pw=sync_playwright().start(); b=pw.chromium.launch(channel='msedge',headless=True); b.close(); pw.stop(); print('ok')" 2>&1
```

**Windows 编码问题**：`playwright_fetch.py` 等脚本输出 JSON 时，Windows 默认 GBK 编码可能无法编码特殊 Unicode 字符（如淘宝页面的  图标），导致 `UnicodeEncodeError`。

- **PowerShell** 环境：`$env:PYTHONIOENCODING = 'utf-8'; python script.py`
- **Bash** 环境（Git Bash/WSL）：`PYTHONIOENCODING=utf-8 python script.py`
- **注意** `$env:` 语法仅在 PowerShell 中有效，Bash 中需要用 `export` 或 `VAR=val cmd` 语法
- **建议**：优先使用 PowerShell 执行脚本以统一编码环境

如果报 `TargetClosedError` 或浏览器立即崩溃，说明 **Playwright 版本与系统 Edge 不兼容**，执行：

```bash
pip install --upgrade playwright && playwright install msedge
```

版本要求：Playwright >= 1.60，Edge >= 120。可通过以下命令确认：
```bash
playwright --version
python -c "import subprocess; r=subprocess.run(['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','--version'],capture_output=True,text=True); print(r.stdout.strip())"
```

## 与 Trinity 工作流对齐

- Trinity 默认先验证 CLI / 脚本真实链路，再决定是否要修 skill 文档。
- 对高风险站点，优先保留"真实浏览器进入内容层"的证据，而不是只保留搜索结果片段。
- 如果测试和实现口径不一致，先修实现，再把测试同步到同一链路定义。

## 默认原则

1. **先低风险，后高风险**：SearXNG → 百度搜索 → Playwright
2. **优先用 CLI / scripts**，不要在对话里手工拼高风险抓取步骤
3. **结果够用就停**，不要为了"更完整"盲目升级抓取层级
4. **遇到 challenge / 访问频繁 / 登录墙就熔断**，不要硬刷
5. **高风险社区站点默认串行**，不要并发开抓
6. **每次重跑自动清理冷却**：每次新的抓取任务启动时，自动清除上一次运行遗留的冷却/节流状态（cooldown、rate-limit 等），确保不会因上次的冷却期阻塞新任务。冷却只应在同一次运行内生效。

## HTML 输出（默认行为）

**所有 `run` / `cards` / `deep` 等完整结果命令，默认生成 HTML 报告文件**，保存在 `.data/html_reports/` 目录下，文件路径输出到 stderr（格式: `[HTML] <path>`）。

- JSON 结果仍然打印到 stdout（保持机器可读兼容性）
- HTML 报告包含：统计卡片、可排序表格、搜索链接、响应式样式
- 如需禁用 HTML 输出：加 `--no-html` 参数
- 强制开启（已是默认）：`--html` 参数

```bash
# 默认产出 JSON(stdout) + HTML(stderr 报告路径)
python scripts/search-fetch douyin run --query "xxx" --target-count 50

# 仅 JSON
python scripts/search-fetch douyin run --query "xxx" --no-html
```

## 小红书触发规则

- **仅当用户提示词中明确要求抓取小红书时，才执行小红书抓取。** 默认不抓取小红书
- 当用户要求抓取小红书时，**必须优先执行小红书（第一个抓取）**，再执行其他平台
- 若用户未提及小红书，不得自动将其加入抓取计划

## 红线

- 不要把 Playwright 当默认搜索引擎
- 不要把 SearXNG / 普通 HTTP 抓取当成知乎、贴吧、B站的正文完成态
- 不要绕过统一入口脚本直接手搓高风险抓取
- 不要自由发挥抓取流程；必须严格按本 skill 已定义的 CLI 入口、参数、调度和失败口径执行
- 不要在出现反爬信号后继续自动重试同域名
- 不要把 discovery layer 当 content layer
- 不要在用户未明确要求时自动抓取小红书

## 站点口径

### B站

- 发现层：可走 SearXNG / 搜索入口
- 正文层：Playwright 进入真实视频页、确认真实 BV
- **评论层：两条路径，按入口选择**
  - `search-fetch bili run` CLI 路径：走 cookie API 抓评论（推荐，更完整）
  - `guarded_search_fetch.py` profile 驱动路径：走 Playwright 可见评论（受 `comment_fetch_mode` 配置控制）
- 没有 cookie 文件、cookie 校验失败、或评论不是 cookie 路径得到时，**B站评论层必须判失败**（仅适用于 `bili run` CLI 路径）

合格链路（CLI 路径）：

`搜索页 → 至少打开 5 个真实视频页 → 确认 BV → 抓正文摘要 → 用 cookie 抓评论样本`

合格链路（profile 驱动路径）：

`搜索页 → 至少打开 5 个真实视频页 → 确认 BV → 抓正文摘要 → Playwright 可见评论抓取`

### 小红书

- **登录态是硬性前提**：XHS 搜索页对未登录用户直接显示登录墙，无任何笔记内容。Playwright 必须使用已登录 xiaohongshu.com 的 Edge profile。
- 发现层：直接导航到 `xiaohongshu.com/search_result?keyword=<query>` ，不走搜索引擎中转
- 正文层：从搜索结果页提取笔记卡片 URL，点击进入笔记详情页
- 评论层：从笔记详情页的正文 DOM 中提取可见评论

合格链路：

`直接导航 XHS 搜索 URL → 提取笔记卡片链接 → 点击进入至少 3 个真实笔记详情页 → 抓正文摘要 + 可见评论样本`

失败判定（小红书特有）：
- 搜索页出现"登录后查看更多搜索结果" → **登录态丢失，必须判失败**
- 搜索结果页无笔记卡片链接 → discovery failed
- 详情页 body 仅含页脚备案信息 → 未进入真实内容层

### 知乎 / 贴吧

- 发现层可走搜索
- 正文 / 帖子 / 回答 / 楼层内容必须走 Playwright
- 非 Playwright backend 的正文结果，不算完成态

### 抖音 / 淘宝 / Reddit / Cloudflare 高防页面

- 默认按高风险站点处理
- 优先走统一脚本入口
- 需要真实浏览器时再升级到 Playwright
- **抖音 (douyin.com)**：`cards/run` 默认走轻量搜索卡片，不进入详情；必须通过 CLI 入口执行，保留 `SEARCH_FETCH_RUN_SCOPE=douyin.com`、调度记录和 HTML 报告。
- **淘宝 (taobao.com)**：搜索结果页依赖 JS 渲染和登录态，**必须走 Playwright**，不能走 web_fetch/SearXNG。优先使用专用 `taobao` platform CLI。

### 淘宝 / 天猫 / 电商

- 淘宝发现层：先打开 `https://s.taobao.com/search` 搜索页，输入 query，模拟点击一次搜索按钮，进入结果页后滚动加载；不走搜索引擎中转
- 正文层：从搜索结果页提取商品卡片（标题/价格/销量/店铺）
- **登录态要求**：浏览商品列表不需要登录，但查看详情/评论需要登录态
- 合格链路：`打开 s.taobao.com/search → 输入 query → 点击搜索 → 结果页滚动加载 → 提取商品列表（标题+价格+销量+店铺）`

## CLI 入口

优先使用 `scripts/search-fetch`，默认输出 JSON。

**强制执行口径：**
- 抓取任务必须严格按照本 SKILL.md 的站点入口、CLI 命令、参数口径、登录态流程、调度规则和失败判定执行。
- 禁止自由发挥：不得临时改用 sampler 直调、手写 Playwright 流程、额外开页、并发多个高风险平台、扩大页数/滚动预算、进入详情层，除非用户明确要求且本 skill 已有对应命令口径。
- 如果用户指出"按 skill"或"流程不对"，先回到本文件核对流程，再执行；不要凭记忆继续跑。
- 如果现有 skill 口径不能覆盖用户需求，先更新 skill/脚本口径并说明变更，再执行抓取。

### 给外部模型 / DeepSeek 的执行清单

外部模型执行本 skill 时必须按下面的顺序汇报和判定，不允许只凭肉眼看浏览器或凭最后一句日志下结论。

1. 先执行版本兼容性检查；失败则先修环境，不跑抓取。
2. 使用 `scripts/search-fetch` 或 `scripts/search_fetch_cli.py`，不要直接 import sampler，也不要自己写 Playwright 流程。
3. Windows 中文 query 推荐用 Python `subprocess.run([...])` 参数数组传参；如果命令行输出或 URL 里出现 `????`，判为编码失败，必须重跑。
4. 每次新任务只跑一个高风险域名；不要同时开淘宝、抖音、天猫、京东多个浏览器任务。
5. 运行结束后必须同时读取 stdout JSON 和 stderr 的 `[HTML] <path>`；没有最终 JSON 或没有 HTML 路径，不能宣称完成。
6. 必须报告这些字段：`mode`、`ok`、`target_count`、`card_count`、`reason`、`flow_evidence`、HTML 文件路径。
7. `ok=false` 或 `card_count < target_count` 一律判为未达标，只能说"本轮不足"，不能解释成"这个品类可能只有这么多"，除非 `flow_evidence` 明确显示已翻页/已滚到底且无更多候选。
8. 出现 `captcha_blocked`、`challenge_or_rate_limited`、`login_required`、访问频繁、安全验证时立即停止；不要自动换入口、硬刷、并发补跑。
9. 用户需要抖音链接时必须保持默认链接补齐；只有用户明确说"不要链接/极速/只要标题作者互动数"时才可加 `--no-resolve-links`。淘宝/天猫/京东只抓列表字段，不进入详情。

### B站常用命令

```bash
# 搜索层决策
python3 scripts/search-fetch bili search --query "倩女幽魂手游"

# 在 Playwright Edge 中打开并确认真实 BV
python3 scripts/search-fetch bili open --query "倩女幽魂手游" --target-count 5

# 刷新 B站 cookie
python3 scripts/search-fetch bili refresh-cookie

# 用 cookie 抓单个 BV 的评论
python3 scripts/search-fetch bili comments --bvid BV1G5d5BvEtz --pages 3 --include-sub --sub-pages 2

# 串行执行：搜索 → 打开视频 → 用 cookie 抓评论
python3 scripts/search-fetch bili run --query "倩女幽魂手游" --target-count 5 --comment-pages 3
```

### 小红书常用命令

```bash
# 完整链路（推荐，一步到位）：
# 直接导航 XHS 搜索 URL → 提取笔记链接 → 进入详情页 → 抓正文/评论
python3 scripts/search-fetch xhs run --query "异环" --target-count 5
```

小红书入口规则（已实现，无需手动操作）：
- 直接导航到 `xiaohongshu.com/search_result?keyword=<query>` ，不走搜索引擎
- 从搜索结果页 DOM 提取笔记卡片 URL（`/explore/` 或 `/search_result/` 路径）
- 点击笔记卡片进入详情页，抓取正文和可见评论
- 依赖 Edge 真实 profile 的登录态（`_playwright_base.py` 自动处理）

### TapTap 常用命令

```bash
# 搜索层决策
python3 scripts/search-fetch taptap search --query "炉石传说"

# 在 Playwright Edge 中进入真实游戏详情页
python3 scripts/search-fetch taptap open --query "炉石传说"

# 进入评价页并抓综合/最新评价
python3 scripts/search-fetch taptap reviews --query "炉石传说" --target-count 10

# 完整链路：搜索 → 详情页 → 评价页 → 输出评价样本
python3 scripts/search-fetch taptap run --query "炉石传说" --target-count 10
```

### 贴吧常用命令

```bash
# 搜索层决策
python3 scripts/search-fetch tieba search --query "倩女幽魂手游"

# 进入真实吧页并确认候选帖子池
python3 scripts/search-fetch tieba open --query "倩女幽魂手游" --target-count 5 --max-attempts 10

# 打开真实讨论帖并抓 5 个帖子详情样本
python3 scripts/search-fetch tieba threads --query "倩女幽魂手游" --target-count 5 --max-attempts 10

# 完整链路：找吧 → 进吧页 → 跳过低价值帖 → 打开 5 个真实帖子详情页
python3 scripts/search-fetch tieba run --query "倩女幽魂手游" --target-count 5 --max-attempts 10
```

### 抖音常用命令

```bash
# 搜索层决策
python3 scripts/search-fetch douyin search --query "倩女幽魂手游"

# 默认轻量：只抓搜索结果卡片（标题/作者/互动数/时长），不进入详情页
python3 scripts/search-fetch douyin cards --query "倩女幽魂手游" --target-count 10

# open / videos / run 也默认走轻量卡片模式
python3 scripts/search-fetch douyin run --query "倩女幽魂手游" --target-count 10

# 默认会补对应链接；如果用户明确只要极速列表且不要链接，才关闭链接补齐
python3 scripts/search-fetch douyin cards --query "倩女幽魂手游" --target-count 10 --no-resolve-links

# 轻量卡片使用独立调度 action=cards，并在同一搜索页内轻滚补足，避免反复重启补抓
python3 scripts/search-fetch douyin cards --query "倩女幽魂手游" --target-count 100 --max-scrolls 18

# 只需要标题/作者/互动数/时长且明确不要链接时，才关闭链接补齐
python3 scripts/search-fetch douyin cards --query "倩女幽魂手游" --target-count 100 --max-scrolls 18 --no-resolve-links

# 显式深抓：进入视频详情弹窗，抓正文/评论样本，慢且更容易遇到风控
python3 scripts/search-fetch douyin deep --query "倩女幽魂手游" --target-count 5
```

抖音执行规则：
- 每次抓取前先跑版本兼容性检查，再走 `scripts/search-fetch douyin ...` / `scripts/search_fetch_cli.py douyin ...`，不要直接 import `douyin_sampler`。
- Windows 下如需避免中文参数乱码，可用 Python `subprocess.run([...])` 传参调用 CLI；不要把中文 query 直接拼成 PowerShell 字符串后再怀疑页面结果。
- 默认轻量抓取必须补链接；不补链接只能在用户明确不要链接时使用 `--no-resolve-links`，但不要升级到详情/评论抓取。
- 抖音 `cards` 成功口径：`ok=true`、`card_count >= target_count`、默认链接模式下 `link_count == card_count`、`flow_evidence.search_opened=true`，并生成 HTML 报告路径。
- 抖音搜索入口必须先打开首页、在搜索框输入 query 并提交；落到搜索结果页后必须确认/点击一次"视频"tab，再按视频结果列表滚动采集。不能把综合搜索页的 30 多条结果误判为视频列表全集。
- 抖音滚动口径：`cards` 必须在同一搜索页内滚动补卡，不要反复重启搜索；`--max-scrolls` 是滚动上限/保护阈值，不是必须跑满的固定轮数。不传时脚本按 `target_count` 估算动态上限，运行中根据每轮新增数、DOM 卡片数增长和滚动证据自适应提前停止。
- 如果 `card_count < target_count` 且 `flow_evidence.scroll_rounds == 0`，判定为"未滚动/滚动失败"，不要说"内容只有这么多"。
- 如果 `card_count < target_count` 且 `scroll_rounds > 0`，只能说"本轮不足"，并报告 `raw_candidate_count`、`stagnant_rounds`、`scroll_budget`、`round_trace`；是否继续由用户决定。
- `candidate_pool_exhausted` 只能在页面明确出现"没有更多/到底了/已加载全部"等 `end_marker` 时使用；不要把"当前 DOM 候选数没增长"当作候选池耗尽，因为抖音搜索结果是虚拟列表，滚动后旧卡片可能被替换。
- `adaptive_no_progress` 表示已完成最小探测轮数，并且连续多轮没有新增 item、候选池没有增长、DOM 卡片数也没有增长；这是节省时间的动态收口，不等于内容全集耗尽。
- 如果跑满 `--max-scrolls` 仍未达标，原因应为 `scroll_budget_exhausted`，表示"本轮滚动预算耗尽且未达标"，不是"内容只有这么多"。
- 链接补齐必须在滚动收集完成后集中执行，不要边滚动边逐条点卡片；优先级为：网络响应 ID 匹配（`network_resolved`）→ DOM/React 隐藏属性扫描（`dom_resolved`）→ 静默 history/open 拦截（`quiet`）→ 遮罩下可见点击兜底（`visible_click_fallback`）。正常情况下 `visible_click_fallback` 应明显小于 `card_count`；如果 `flow_evidence.link_resolve.unresolved > 0`，判为 `links_incomplete`，不要宣称完成。
- 抖音 `cards` 默认必须保证"前 N 条"口径：结果只来自搜索结果列表 DOM 的首次出现顺序（`flow_evidence.strict_first_n=true`、`ordered_source=dom_first_seen`）。网络响应 JSON 只能用于补链接和诊断，不能用来补条数，因为网络返回顺序不等于页面前 N 顺序。
- `flow_evidence.network_items` 只能作为诊断字段：必须报告 `available/eligible/would_add_if_unordered/skipped_*`，并保持 `used_for_order=false`、`added=0`。如果 DOM 顺序只抓到 66 条，即使网络里还有候选，也必须判"前 100 未达标"，不能凑满。
- 抖音 HTML 表格字段必须是结构化的标题/作者/互动数/时长；如果出现 `00:5245标题@作者` 这类压缩串，说明归一化失败，不能把该 HTML 当成合格结果。链接补齐后还要再按最终 href 去重，避免同一视频重复出现在 HTML 中。

### 通用登录态保存

用于需要人工参与的登录场景（验证码、二次验证、扫码等）。启动独立 Edge 浏览器窗口，等用户手动登录后自动保存 cookie。

详细登录流程和各站点登录命令见上方 **登录态管理** 章节。

```bash
python scripts/search-fetch login --url "<站点URL>" --wait-text "<登录后页面文本>" --timeout 120
```

### 淘宝商品搜索常用命令

```bash
# 手动登录并保存登录态；后续 cards/run 复用
python3 scripts/search-fetch taobao login --timeout 180

# 默认轻量：只抓搜索结果商品卡片（标题/价格/销量/店铺/地区/链接），不进入详情页
python3 scripts/search-fetch taobao cards --query "极萌水光" --target-count 20

# run 也默认走轻量卡片模式，并在同一轮内滚动/翻页补足
python3 scripts/search-fetch taobao run --query "极萌水光" --target-count 100 --max-scrolls 18
```

淘宝商品搜索入口规则：
- 必须先进入 `https://s.taobao.com/search`，定位搜索框，输入 query，模拟点击一次搜索按钮；进入结果页后再滚动采集
- 输入必须是可见输入过程：鼠标点入可见搜索框，清空旧值，逐字输入 query，并确认输入框 value 包含 query 后再点击搜索
- 如果搜索按钮弹出新页，接管新页 URL 回当前页并关闭弹页；禁止循环触发搜索按钮或反复打开搜索 URL
- 只抓列表卡片字段，不进入商品详情和评论页
- 使用独立调度 `action=cards`，等待低于详情/页面抓取
- `--max-pages` 是翻页安全上限；不传时淘宝会按 `target_count` 动态估算页数上限（100 条默认最多 5 页），第一页不足必须尝试 UI 翻页补足，不能静默停在 `pages_opened=1`。
- 淘宝/电商列表默认抓"可见商品卡片"，不是唯一商品详情 URL；去重应按 `href(id)+标题+价格` 的视觉卡片级 key，缺 href 的可见商品卡才退回标题+价格+销量+店铺，不能因为同 href 或链接暂时没解析出来就丢掉视觉上不同的商品。
- 淘宝/电商列表应尽量补齐商品链接；每页 `flow_evidence.pages[].href_count/missing_href_count` 必须报告链接覆盖情况。缺 href 的卡片可计入 `card_count`，但最终结论要说明缺链接数量。
- 翻页必须是页面 UI 操作：优先点击分页区真正的"下一页"按钮（如 class 含 `next-next`、aria/text 为"下一页"），找不到时才使用页面底部"到第 X 页 + 确定"跳页控件；禁止把 `page=2` 直接拼到 URL 里当作翻页
- 如果跳转登录页、出现安全验证/访问频繁，立即停止并返回原因，不自动硬刷

淘宝结果判读规则：
- `card_count < target_count` 时必须判为未达标；不能推断"淘宝可能只有这些结果"。
- 目标未达标时必须检查 `flow_evidence.pages_opened`、`page_budget`、`page_budget_mode` 和每页 `page_card_count`；`pages_opened == 1` 且 `page_budget > 1` 说明没有真正翻页。
- 真正翻页必须在 `flow_evidence.next_page_attempts` 中看到 `ok=true`，并且 `after_page > before_page` 或 `after_url` 从 `page=1` 变为 `page=2`。
- 如果 `pages_opened == 1` 且未达标，正确结论是"第一页滚动不足或翻页未发生"，不是"已完成"。
- 如果 `flow_evidence.pages` 里后续页 `page_card_count == 0` 或没有后续页，要报告"下一页点击失败/没有更多可解析卡片"，不要静默吞掉。
- HTML 表格行数必须与 JSON 的 `card_count` 对齐；不一致时以 JSON 为准并报告 HTML 可能是旧文件或渲染异常。

### 天猫 / 京东商品搜索常用命令

```bash
# 天猫：先登录保存登录态
python3 scripts/search-fetch tmall login --timeout 180

# 天猫：轻量抓商品卡片并输出 HTML
python3 scripts/search-fetch tmall run --query "极萌水光" --target-count 100 --max-scrolls 18 --max-pages 3

# 京东：先登录保存登录态
python3 scripts/search-fetch jd login --timeout 180

# 京东：轻量抓商品卡片并输出 HTML
python3 scripts/search-fetch jd run --query "极萌水光" --target-count 100 --max-scrolls 18 --max-pages 3
```

天猫 / 京东入口规则：
- 天猫从 `https://www.tmall.com/` 或 `list.tmall.com/search_product.htm?q=<query>` 进入；当前天猫官方搜索可能落到 `s.taobao.com/search?...&tab=mall` / `fromTmallRedirect=true`，只要页面顶部选中的是"天猫" tab，即按天猫搜索结果处理。
- 京东直接导航到 `search.jd.com/Search?keyword=<query>&enc=utf-8`
- `login` 命令会打开对应登录页，检测登录页跳出后保存登录态；后续抓取复用临时 Profile 登录态
- `cards/open/run` 默认输出 JSON，同时生成 HTML 报告路径到 stderr 的 `[HTML] ...`
- 登录墙、安全验证、访问频繁直接停止并返回原因，不自动硬刷

天猫执行规则：
- 天猫首页搜索按钮可能新开页面；实现和调试时必须保证搜索提交最多一次，并做同页提交/弹页接管，不能在循环里连续按 Enter 或点击多个搜索按钮。
- 天猫 tab 结果允许位于 `s.taobao.com`，但必须确认是 `tab=mall` 或 `fromTmallRedirect=true`；普通淘宝 tab 不能算天猫。
- 天猫默认应轻量单页滚动抓取；如需补量，优先在当前结果页滚动，不要连续打开多个结果页。

### 淘宝生意参谋（SYCM）常用命令

```bash
# 抓取生意参谋页面数据（首次使用需先登录，见上方登录态管理章节）
python3 scripts/taobao_sycm_fetch.py --url "https://sycm.taobao.com/cc/item_rank?dateRange=2026-05-20%7C2026-05-20&dateType=today" --wait 8 --mode both

# 仅截图
python3 scripts/taobao_sycm_fetch.py --url "<url>" --mode screenshot

# 仅提取文本/表格
python3 scripts/taobao_sycm_fetch.py --url "<url>" --mode content --wait 10
```

淘宝入口规则：
- **前提**：用户已在 Edge 中登录 taobao.com / sycm.taobao.com（登录流程见上方 **登录态管理**）
- 使用独立临时 Profile 的登录态（`_playwright_base.py` 自动处理）
- 如果 cookie 过期（跳转到登录页），提示用户重新执行 `search-fetch login` 登录

## 执行顺序

### 仅搜索

先走：

```bash
python3 scripts/guarded_search_fetch.py search "<query>" [--profile <profile>] [--domain <domain>]
```

### 抓取 / 高风险站点

优先走：

```bash
python3 scripts/guarded_search_fetch.py fetch "<url>" [--profile <profile>] [--run-id <run_id>] [--backend <backend>] [--domain <domain>]
```

### B站

优先走：

```bash
python3 scripts/search-fetch bili ...
```

### 小红书

优先走：

```bash
python3 scripts/search-fetch xhs run --query "<query>" --target-count 5
```

## 参数口径

- `--target-count`: 目标样本数；抖音默认轻量模式下表示目标搜索卡片数，`douyin deep` 下才表示目标详情数
- `--max-scrolls`: 抖音/淘宝轻量卡片模式的同页滚动上限；不传时按目标数自动估算，100 条通常不需要外层补抓
- `--max-pages`: 淘宝轻量卡片模式的搜索翻页上限；不传时默认只在当前搜索结果页滚动，只有显式传入大于 1 时才尝试点击"下一页"
- `--max-attempts`: 单轮最多尝试多少个候选（当前主要用于贴吧）
- CLI 只执行一轮并报告是否达标；是否补抓由大语言模型层决定

## 何时升级到 Playwright

只在这些场景升级：

- 前两层结果不足
- 页面强依赖 JS / 懒加载 / 登录态
- 遇到 challenge / 人机验证 / Cloudflare
- 目标本来就是高风险社区正文或评论层

## 成功判定

在宣称某个平台"抓到了"之前，至少确认：

- 已进入真实详情页 / 正文页 / 视频页 / 帖子页，而不是只停在搜索或列表页
- 抓到的内容来自目标内容层，不是壳页、推荐流或搜索结果片段
- 使用的 backend 符合站点规则（例如：知乎/贴吧正文必须 Playwright，B站评论通过 `bili run` 走 cookie API 或通过 profile 驱动走 Playwright，小红书必须使用已登录的 Edge profile）
- 没有被 challenge / 登录墙 / 访问频繁提示污染
- 对轻量卡片任务，成功必须满足 `ok=true` 且 `card_count >= target_count`；只生成 HTML 或 stdout 有部分 items 不算成功。
- 最终回复必须给出 HTML 路径和核心计数；不要只说"跑完了"。

## 失败判定

出现以下任一情况，应直接报失败或阻塞：

- challenge / captcha
- 访问频繁
- 登录墙或空白壳页
- 站点规则要求的 backend 没有满足
- 只拿到 discovery layer，没有进入 content layer
- `ok=false`
- `card_count < target_count`
- 命令超时、中断、只有中间日志没有最终 JSON
- 中文 query 变成 `????` 或 URL 中 q 参数明显乱码
- 抖音 `cards` 未达标且 `flow_evidence.scroll_rounds == 0`
- 淘宝传入 `--max-pages > 1` 但 `flow_evidence.pages_opened == 1`

## 参考脚本

优先复用这些脚本，不要重复发明流程：

- `scripts/search-fetch`
- `scripts/xhs_sampler.py`（小红书核心：直接导航搜索 URL → 笔记详情 → 评论）
- `scripts/_playwright_base.py`（Playwright 生命周期：独立临时 Profile 启动 Edge，不影响用户当前浏览器）
- `scripts/guarded_search_fetch.py`
- `scripts/safe_fetch_router.py`
- `scripts/playwright_fetch.py`
- `scripts/bilibili_comment_fetch.py`
- `scripts/bilibili_cookie_refresh.py`
- `scripts/search_gate.py`
- `scripts/scheduler.py`

如果需要扩展行为，优先改脚本；只有在触发条件、红线或使用口径变化时，才改这个 SKILL.md。
