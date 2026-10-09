# astrbot_plugin_github_star_notifier

<img src="logo.png" width="120" alt="logo" align="right" />

> 监控 GitHub 仓库的 **Star / Fork / 提交记录**，一旦出现新动态，立即推送提醒到指定的 QQ 群或其他会话。

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.17.0%2C%20%3C5-5865F2)
![License](https://img.shields.io/badge/License-MIT-green)
![Python](https://img.shields.io/badge/Python-%3E%3D3.10-blue)
![Ruff](https://img.shields.io/badge/code%20style-ruff-000000)

**版本要求**：AstrBot `>=4.17.0, <5`　|　**主要平台**：OneBot（NapCat / Lagrange）接入的 QQ 群

---

## ✨ 功能特性

| 能力 | 说明 |
| --- | --- |
| 🌟 新 Star 推送 | 检测到新的 Star 时推送，并列出新增的 Star 用户（超出上限自动概括为「等 N 位用户」） |
| 🍴 新 Fork 推送 | 可选，默认关闭 |
| 📦 提交推送 | 可选，检测到仓库有新提交时推送，可限制每次展示的条数 |
| 🖼️ 头像附图 | 可选，推送 Star 时附带用户头像 |
| 📣 @全体成员 | 可选，推送时 At 全体 |
| 🧩 多仓库并行监控 | 可在配置面板或群内指令中动态增删 |
| 💾 状态持久化 | 订阅关系与基线保存在 `data/plugin_data/` 下，更新插件不丢失 |
| 🔐 Token 支持 | 支持配置 GitHub Token，API 速率上限从 60 次/小时提升到 5000 次/小时 |

---

## 📦 安装

1. 将 `astrbot_plugin_github_star_notifier` 整个文件夹放入 AstrBot 的 `data/plugins/` 目录下。
2. 在 AstrBot 管理面板的「插件」页面点击刷新，或重启 AstrBot。
3. 进入插件配置页，填写要监控的仓库。

目录结构：

```
data/plugins/astrbot_plugin_github_star_notifier/
├── main.py              # 插件主程序
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置面板 Schema
├── requirements.txt     # 依赖（aiohttp）
├── logo.png             # 插件图标（256×256）
├── ruff.toml            # 代码风格配置
├── README.md
├── LICENSE              # MIT
└── .gitignore
```

---

## 🧩 兼容性

### AstrBot 版本

`astrbot_version: ">=4.17.0,<5"`

本插件依赖下列在 AstrBot v4.17 起稳定提供的接口：

| 接口 | 用途 |
| --- | --- |
| `astrbot.api.event.MessageChain` | 构造推送消息链 |
| `astrbot.api.star.Context.send_message()` | 主动消息推送 |
| `astrbot.api.event.filter` | 指令与权限装饰器 |
| `astrbot.core.utils.astrbot_path.get_astrbot_plugin_data_path()` | 数据持久化目录 |

### 支持的平台适配器

| 平台适配器 | 是否声明支持 | 说明 |
| --- | :---: | --- |
| `aiocqhttp` | ✅ | **推荐**，NapCat / Lagrange 接入 QQ，主动推送稳定 |
| `telegram` / `discord` / `slack` / `lark` / `dingtalk` / `kook` | ✅ | 支持主动消息 |
| `satori` / `matrix` / `misskey` / `line` / `vocechat` / `mattermost` | ✅ | 支持主动消息 |
| `wecom` / `wecom_ai_bot` / `weixin_oc` / `weixin_official_account` | ✅ | 支持主动消息（具体能力视平台策略） |
| `qq_official` / `qq_official_webhook` | ❌ | **未声明**：AstrBot 的 `Context.send_message()` 在这两个适配器上不可用，插件无法主动推送 |

> 判定标准：凡实现 `send_by_session()` 主动发送能力的适配器均予声明；QQ 官方机器人 API 按 AstrBot 官方说明不支持主动消息，故排除。

---

## ⚙️ 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 后台轮询总开关 |
| `repositories` | list | `[]` | 监控的仓库，每项为 `owner/repo` |
| `github_token` | string | `""` | GitHub Token（可选，强烈建议配置） |
| `poll_interval` | int | `300` | 轮询间隔（秒），最小 60 |
| `notify_star` | bool | `true` | 推送新的 Star |
| `notify_fork` | bool | `false` | 推送新的 Fork |
| `notify_commit` | bool | `false` | 推送新的提交 |
| `max_commits_per_push` | int | `3` | 单次最多展示的提交条数 |
| `branch` | string | `""` | 监控分支，留空为默认分支 |
| `notify_on_first_run` | bool | `false` | 首次监控时是否推送一次概览 |
| `mention_all` | bool | `false` | 推送时 @全体成员 |
| `send_avatar` | bool | `false` | Star 推送附带头像图片 |
| `allow_member_subscribe` | bool | `false` | 是否允许普通群成员订阅 |
| `target_sessions` | list | `[]` | 推送目标会话标识（一般不用手填） |
| `star_message_template` | text | 见默认值 | Star 消息模板 |
| `fork_message_template` | text | 见默认值 | Fork 消息模板 |
| `commit_message_template` | text | 见默认值 | 提交消息模板 |

### 消息模板占位符

| 模板 | 可用占位符 |
| --- | --- |
| `star_message_template` | `{repo}` `{user}` `{star_count}` `{delta}` `{user_count}` `{time}` `{url}` |
| `fork_message_template` | `{repo}` `{user}` `{fork_count}` `{delta}` `{user_count}` `{time}` `{url}` |
| `commit_message_template` | `{repo}` `{branch}` `{count}` `{commits}` `{time}` `{url}` |

> 模板渲染失败时会自动回退到内置默认模板，不会导致插件报错。

---

## 🎮 使用指令

### 管理员指令

| 指令 | 说明 |
| --- | --- |
| `/ghstar sub` | 在**当前会话**订阅推送（把机器人拉进群后，在群里发一次即可） |
| `/ghstar unsub` | 取消当前会话订阅 |
| `/ghstar add owner/repo` | 添加监控仓库 |
| `/ghstar del owner/repo` | 移除监控仓库 |
| `/ghstar list` | 查看运行状态、监控仓库、推送目标 |
| `/ghstar check` | 立即检查一次所有仓库 |
| `/ghstar test` | 向订阅会话发送一条测试消息 |
| `/ghstar help` | 显示帮助 |

### 群成员指令

| 指令 | 说明 |
| --- | --- |
| `/ghsub` | 订阅当前会话（需在配置中开启「允许群成员订阅」） |
| `/ghunsub` | 退订当前会话 |

### 快速上手

```
/ghstar add Soulter/astrbot     # 添加要监控的仓库
/ghstar sub                     # 在当前群订阅推送
/ghstar check                   # 立即检查一次，验证配置
```

---

## 🧠 工作原理

插件在后台以固定间隔轮询 GitHub REST API：

| 目标 | 接口 | 说明 |
| --- | --- | --- |
| 仓库概览 | `GET /repos/{owner}/{repo}` | 获取 `stargazers_count`、`forks_count`、默认分支 |
| 新增 Star 用户 | `GET /repos/{owner}/{repo}/stargazers` | 带 `Accept: application/vnd.github.star+json`，取**最新一页**倒推新增用户 |
| 新增 Fork 用户 | `GET /repos/{owner}/{repo}/forks?sort=newest` | 取最新若干条 |
| 新增提交 | `GET /repos/{owner}/{repo}/commits` | 与上一次记录的 SHA 对比，向前回溯收集新提交 |

**为什么不使用 Webhook？**
AstrBot 插件通过 `context.register_web_api()` 注册的路由位于管理面板下（`/api/plug/...`）且需要面板鉴权，无法作为公开的、免鉴权的 GitHub Webhook 回调地址。
因此本插件采用轮询方案：**无需公网 IP、无需反向代理、无需域名和 HTTPS**，部署成本最低。

**首次运行行为**：首次拉取到某个仓库时只建立基线（记录当前 Star 数、Fork 数、最新提交 SHA），**不会**推送历史数据，避免刷屏。可通过 `notify_on_first_run` 调整。

---

## ⚠️ 速率限制

GitHub REST API 对未认证请求限制为 **60 次/小时**，认证请求为 **5000 次/小时**。

粗略估算（单仓库单次轮询）：

- 基础检查：1 次请求
- 有新 Star 时：额外 1 次请求
- 有新提交时：额外 1 次请求

即最坏情况约 `3 × 仓库数 × (3600 / 轮询间隔)` 次/小时。为确保稳定运行，**建议在配置中填写 GitHub Token**。

> Token 权限建议：Fine-grained Token，仅授予目标仓库的 **Contents: Read-only** 与 **Metadata: Read-only**；公开仓库使用经典 Token 时只需勾选 `public_repo`。

---

## 🔒 数据存储

| 路径 | 内容 |
| --- | --- |
| `data/plugin_data/astrbot_plugin_github_star_notifier/state.json` | 订阅会话列表、各仓库的 Star/Fork/最新提交 SHA 基线 |

数据目录独立于插件目录，插件更新或重装不会覆盖。

---

## 🛠️ 常见问题

**Q：推送没反应？**
按顺序排查：① `/ghstar list` 确认推送目标不为空；② `/ghstar test` 测试通道；③ 查看 AstrBot 日志中 `[astrbot_plugin_github_star_notifier]` 前缀的输出；④ 确认平台适配器支持**主动消息**（OneBot / NapCat 支持；QQ 官方机器人 API 不支持 `send_message` 主动推送）。

**Q：QQ 官方机器人推送失败？**
QQ 官方机器人平台不支持 AstrBot 的主动消息接口，请改用 NapCat / Lagrange 等 OneBot 适配器接入。

**Q：Star 用户显示为「N 位用户」而不是名字？**
新增 Star 数超过单页容量（100）时只会展示最新一页；或多位用户在同一轮询周期内 Star，仅能列出可获取的用户名。

**Q：如何只看提交，不看 Star？**
关闭 `notify_star`，开启 `notify_commit`。

---

## ✅ 开发原则遵循情况

对照 [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) 的开发原则：

| 原则 | 落实情况 |
| --- | --- |
| 功能需经过测试 | 已编写离线测试（桩替 AstrBot / aiohttp），覆盖仓库名解析、消息渲染、模板回退、消息链构建、基线建立与 Star/Fork/提交三类推送、订阅持久化、无目标降级、生命周期初始化与销毁，全部断言通过 |
| 需包含良好的注释 | 主程序含模块级说明、分区注释与逐方法 docstring |
| 持久化数据存于 `data` 目录 | 状态写入 `data/plugin_data/astrbot_plugin_github_star_notifier/state.json`，并采用「临时文件 + `os.replace`」原子写入 |
| 良好的错误处理机制 | 网络请求、JSON 解析、模板渲染、消息发送均有兜底；单仓库异常不影响其余仓库；插件不会因单次错误崩溃 |
| 提交前使用 `ruff` 格式化 | 已配置 `ruff.toml` 并通过 `ruff format` + `ruff check`（select E/W/F/I/UP/B/C4/SIM） |
| 不使用 `requests`，改用异步网络库 | 全程使用 `aiohttp` 异步请求，共享单个 `ClientSession` |
| 避免提交无关文件 | 提供 `.gitignore` 排除 `__pycache__`、`data/`、`.venv`、IDE 配置等，发布包体积 < 16MB |

---

## 📄 许可证

MIT License © 2026 akexin
