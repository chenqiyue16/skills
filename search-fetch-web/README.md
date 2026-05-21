# search-fetch-web Skill

`search-fetch-web` 是一个搜索与抓取路由技能包。

核心定位：

- **路由层 + 规则层**：SKILL.md 定义触发条件、红线、CLI 入口、成功/失败判定口径
- **CLI 脚本层**：scripts/ 下的 Python 脚本负责实际的搜索和页面抓取
- **覆盖平台**：Google、Reddit、小红书、抖音、B站、贴吧、TapTap、X（Twitter）、Cloudflare 保护的站点等

## 包内容

分发包只包含运行所需的最小文件：

- `SKILL.md`
- `README.md`
- `package.json`
- `assets/` — domain-policies.json、research-profiles.json
- `scripts/` — 所有 CLI 脚本和辅助模块

不会打包这些运行时或开发期内容：

- `.data/`
- `tests/`
- `scripts/plans/`
- `__pycache__/`
- `*.pyc`

## 运行前提

- 本机可用的 `Python 3`
- 本机可用的 `Node.js`（用于 Playwright）
- `playwright` Python 包：`pip install playwright && playwright install chromium`

## 安装

1. 解压 zip 包到当前宿主应用的技能目录，保持目录名为 `search-fetch-web`
2. 确保 Python 依赖已安装
3. 重启当前宿主应用，让新 skill 被重新加载

## 注意

- `scripts/bilibili_cookies.json` 为运行时 Cookie 文件，首次使用需登录 B站后刷新
- 该 skill 是 `deep-research` 和 `research-craft` 的底层依赖
