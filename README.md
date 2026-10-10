# astrbot_plugin_github_star_notifier

<img src="logo.png" width="120" alt="logo" align="right" />

> 监控 GitHub 仓库的 **Star / Fork / 提交记录**，一旦出现新动态，立即推送提醒到指定的 QQ 群或其他会话。

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.17.0%2C%20%3C5-5865F2)
![License](https://img.shields.io/badge/License-MIT-green)
![Python](https://img.shields.io/badge/Python-%3E%3D3.10-blue)
![Ruff](https://img.shields.io/badge/code%20style-ruff-000000)

**版本要求**：AstrBot `>=4.17.0, <5`　|　**当前版本**：`v1.0.1`　|　**主要平台**：OneBot（NapCat / Lagrange）接入的 QQ 群

---

## ✨ 功能特性

| 能力 | 说明 |
| --- | --- |
| 🌟 新 Star 推送 | 检测到新的 Star 时推送。Star 增量始终基于仍公开的 `stargazers_count` 计算，**不受 GitHub 端点限制影响**；若能取到用户名则一并列出 |
| 🍴 新 Fork 推送 | 可选，默认关闭 |
| 📦 提交推送 | 可选，检测到仓库有新提交时推送，可限制每次展示的条数 |
| 🖼️ 头像附图 | 可选，推送 Star 时附带用户头像 |
| 📣 @全体成员 | 可选，推送时 At 全体 |
| 🧩 多仓库并行监控 | 可在配置面板或群内指令中动态增删 |
| ✅ 仓库自动校验 | `/ghstar add` 会先调用 GitHub API 校验仓库是否存在，避免填错仓库名后静默失效 |
| 🩺 进度可观测 | `/ghstar diag` 逐仓库展示基线时间、最近检查/成功/错误与端点可用性 |
| 💾 状态持久化 | 订阅关系与逐仓库推送进度保存在 `data/plugin_data/` 下，更新插件不丢失 |
| 🔐 Token 支持 | 支持配置 GitHub Token，API 速率上限从 60 次/小时提升到 5000 次/小时 |

---

## 📦 安装

1. 将 `astrbot_plugin_github_star_notifier` 整个文件夹放入 AstrBot 的 `data/plugins/` 目录下。
2. 在 AstrBot 管理面板的「插件」页面点击刷新，或重启 AstrBot。
3. 进入插件配置页，填写要监控的仓库。

目录结构：

```
data/plugins/astrbot_plugin_github_star_notifier/
├── main.py                  # 插件主程序
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # 配置面板 Schema
├── requirements.txt         # 依赖（aiohttp）
├── logo.png                 # 插件图标（256×256）
├── ruff.toml                # 代码风格配置
├── tests/test_offline.py    # 离线回归测试（97 项断言）
├── README.md
├── LICENSE                  # MIT
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
| `poll_interval` | int | `120` | 轮询间隔（秒），最小 30 |
| `notify_star` | bool | `true` | 推送新的 Star |
| `notify_fork` | bool | `false` | 推送新的 Fork |
| `notify_commit` | bool | `false` | 推送新的提交 |
| `max_commits_per_push` | int | `3` | 单次最多展示的提交条数 |
| `branch` | string | `""` | 监控分支，留空为默认分支 |
| `notify_on_first_run` | bool | `true` | 添加仓库后推送一条「已开始监控」概览，便于当场确认推送链路 |
| `mention_all` | bool | `false` | 推送时 @全体成员 |
| `send_avatar` | bool | `false` | Star 推送附带头像图片 |
| `fetch_stargazer_details` | bool | `true` | 尝试获取新增 Star 的具体用户名（因 GitHub 已限制该端点，失败会自动降级为「+N 位用户」） |
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
| `/ghstar add owner/repo` | 添加监控仓库：**先校验仓库是否存在**，成功则立即建立基线并回报当前 Star 数 |
| `/ghstar del owner/repo` | 移除监控仓库 |
| `/ghstar list` | 查看运行状态、监控仓库、推送目标 |
| `/ghstar check` | 立即检查一次所有仓库，并逐仓库回报结果 |
| `/ghstar diag` | 推送进度诊断：基线时间、最近检查/成功/错误、Star 用户列表可用性 |
| `/ghstar test` | 向订阅会话发送一条测试消息 |
| `/ghstar help` | 显示帮助 |

### 群成员指令

| 指令 | 说明 |
| --- | --- |
| `/ghsub` | 订阅当前会话（需在配置中开启「允许群成员订阅」） |
| `/ghunsub` | 退订当前会话 |

### 快速上手

```
/ghstar add akexin/astrbot_plugin_github_star_notifier   # 添加仓库（自动校验 + 立即建立基线）
/ghstar sub                                              # 在当前群订阅推送
/ghstar diag                                             # 查看推送进度诊断
```

> `/ghstar add` 成功后**立即**会推送一条「已开始监控」概览；若仓库名写错或仓库不存在，会直接返回错误而**不会**写入配置，避免出现「加了仓库却永远没有推送」的情况。

---

## 🧠 工作原理

插件在后台以固定间隔轮询 GitHub REST API：

| 目标 | 接口 | 说明 |
| --- | --- | --- |
| 仓库概览 | `GET /repos/{owner}/{repo}` | 获取 `stargazers_count`、`forks_count`、默认分支 —— **Star/Fork 增量判定的唯一依据** |
| 新增 Star 用户 | `GET /repos/{owner}/{repo}/stargazers` | 带 `Accept: application/vnd.github.star+json`，取最新一页并按 `starred_at` 排序。**可选增强，受限时自动降级** |
| 新增 Fork 用户 | `GET /repos/{owner}/{repo}/forks?sort=newest` | 取最新若干条 |
| 新增提交 | `GET /repos/{owner}/{repo}/commits` | 与上一次记录的 SHA 对比，向前回溯收集新提交 |

### ⚠️ 关于 GitHub 的 Star 用户列表限制（重要）

据 GitHub 官方 changelog [《Upcoming access restrictions to public API endpoints and UI views》](https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/)，自 **2026 年 7 月** 起：

| 端点 | 变更 |
| --- | --- |
| `GET /repos/{owner}/{repo}/stargazers` | 访问权限**限制为仓库管理员与协作者** |
| `GET /repos/{owner}/{repo}/subscribers` | 同上 |
| `GET /users/{username}/subscriptions` | 弃用，弃用期内返回空响应 |

实测行为（普通账号请求他人公开仓库）：**403 / 404**；匿名请求返回 **401 Requires authentication**。仅当请求者本人是仓库 owner 或协作者时才会返回 `200`。

**本插件的应对策略：**

1. **Star 增量不依赖该端点。** 新增数量来自 `GET /repos/{owner}/{repo}` 的 `stargazers_count`，该字段仍然公开。因此**即使该端点被限制，每新增一个 Star 依旧会正常推送**。
2. **用户名列表自动降级。** 若该端点返回 401/403/404，推送消息写作 `用户：1 位用户（GitHub 已限制该仓库的 Star 用户列表）`，而不是静默失败。
3. **失败后不再重试。** 一旦确认某仓库该端点不可访问，即打标记跳过后续请求，避免每轮都浪费一次 API 额度；`/ghstar diag` 会展示该标记。
4. **可完全关闭。** 将 `fetch_stargazer_details` 设为 `false` 即不再发起该请求。

> 提示：若你监控的是**自己拥有或参与协作**的仓库，用户名列表与头像功能可正常工作。

**为什么不使用 Webhook？**
AstrBot 插件通过 `context.register_web_api()` 注册的路由位于管理面板下（`/api/plug/...`）且需要面板鉴权，无法作为公开的、免鉴权的 GitHub Webhook 回调地址。
因此本插件采用轮询方案：**无需公网 IP、无需反向代理、无需域名和 HTTPS**，部署成本最低。

**首次运行行为**：`/ghstar add` 或首次拉取到某个仓库时会建立基线（记录当前 Star 数、Fork 数、最新提交 SHA），并**默认推送一条「已开始监控」概览**（由 `notify_on_first_run` 控制），方便当场确认推送链路是否打通。基线建立后，**每新增一个 Star 都会即时推送**，无需再手动触发。

> 基线机制的意义：只记录「当前状态快照」，不回放历史数据。这样无论仓库已有 1 个还是 10000 个 Star，添加监控的瞬间都不会刷屏。

---

## ⚠️ 速率限制

GitHub REST API 对未认证请求限制为 **60 次/小时**，认证请求为 **5000 次/小时**。

粗略估算（单仓库单次轮询）：

- 基础检查：1 次请求
- 有新 Star 时：额外 1 次请求（若该仓库的 Star 用户列表已被 GitHub 限制，则**仅首次尝试一次**，之后不再请求）
- 有新提交时：额外 1 次请求

即最坏情况约 `3 × 仓库数 × (3600 / 轮询间隔)` 次/小时。默认 120 秒间隔、单仓库时约 90 次/小时。为确保稳定运行，**建议在配置中填写 GitHub Token**。

> Token 权限建议：Fine-grained Token，仅授予目标仓库的 **Contents: Read-only** 与 **Metadata: Read-only**；公开仓库使用经典 Token 时只需勾选 `public_repo`。

---

## 🔒 数据存储

| 路径 | 内容 |
| --- | --- |
| `data/plugin_data/astrbot_plugin_github_star_notifier/state.json` | 订阅会话列表；各仓库的 Star/Fork/最新提交 SHA 基线；推送进度（`baseline_at`、`last_check_at`、`last_success_at`、`last_error`、`checks`、`star_detail_ok`） |

数据目录独立于插件目录，插件更新或重装不会覆盖。

---

## 🛠️ 常见问题

**Q：推送没反应？**
先执行 `/ghstar diag`，它会逐仓库告诉你：是否已建立基线、上次检查时间、最近一次错误是什么、Star 用户列表是否可用。然后按需排查：① `/ghstar list` 确认推送目标不为空；② `/ghstar test` 测试通道；③ 查看 AstrBot 日志中 `[astrbot_plugin_github_star_notifier]` 前缀的输出；④ 确认平台适配器支持**主动消息**（OneBot / NapCat 支持；QQ 官方机器人 API 不支持 `send_message` 主动推送）。

**Q：添加了仓库，但一直没有推送？**
最常见的原因是仓库名写错。v1.0.1 起 `/ghstar add` 会先调 API 校验，仓库不存在会直接报错；若你旧版本添加过错误仓库，执行 `/ghstar del 错误仓库名` 后再重新添加即可。另外请注意：**建立基线后只推送「新增」的 Star**，加仓库那一刻已有的 Star 不会补推。

**Q：QQ 官方机器人推送失败？**
QQ 官方机器人平台不支持 AstrBot 的主动消息接口，请改用 NapCat / Lagrange 等 OneBot 适配器接入。

**Q：Star 用户显示为「N 位用户」而不是名字？**
三种可能：① 该仓库你不是 owner / 协作者，GitHub 已限制 `/stargazers` 端点（见上文「关于 GitHub 的 Star 用户列表限制」），可用 `/ghstar diag` 确认；② 新增用户的用户名分散在多个分页，只能取到最新一页；③ 将 `fetch_stargazer_details` 关掉了。

> 注意：这与推送能否正常工作**无关** —— Star 数量与增量推送始终正常。

**Q：如何只看提交，不看 Star？**
关闭 `notify_star`，开启 `notify_commit`。

---

## ✅ 开发原则遵循情况

对照 [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) 的开发原则：

| 原则 | 落实情况 |
| --- | --- |
| 功能需经过测试 | 已编写离线测试（桩替 AstrBot / aiohttp），覆盖仓库名解析、HTTP 错误翻译、默认配置、`/ghstar add` 校验与拒绝、首次基线推送、Star/Fork/提交三类推送、**stargazers 端点受限时的降级与去重请求**、`starred_at` 排序、`list`/`diag` 文案、状态持久化往返、生命周期初始化与重复销毁、模板回退，**共 97 项断言全部通过** |
| 需包含良好的注释 | 主程序含模块级说明、分区注释与逐方法 docstring |
| 持久化数据存于 `data` 目录 | 状态写入 `data/plugin_data/astrbot_plugin_github_star_notifier/state.json`，并采用「临时文件 + `os.replace`」原子写入 |
| 良好的错误处理机制 | 网络请求、JSON 解析、模板渲染、消息发送均有兜底；单仓库异常不影响其余仓库；插件不会因单次错误崩溃 |
| 提交前使用 `ruff` 格式化 | 已配置 `ruff.toml` 并通过 `ruff format` + `ruff check`（select E/W/F/I/UP/B/C4/SIM） |
| 不使用 `requests`，改用异步网络库 | 全程使用 `aiohttp` 异步请求，共享单个 `ClientSession` |
| 避免提交无关文件 | 提供 `.gitignore` 排除 `__pycache__`、`data/`、`.venv`、IDE 配置等，发布包体积 < 16MB |

---

## 🧪 测试

仓库自带离线回归测试，**无需安装 AstrBot** 即可运行（仅依赖标准库）：

```bash
git clone https://github.com/akexin/astrbot_plugin_github_star_notifier.git
cd astrbot_plugin_github_star_notifier
python tests/test_offline.py
```

测试会桩替 `astrbot` 与 `aiohttp` 模块，直接验证核心逻辑，输出形如：

```
通过 97 项，失败 0 项
```

覆盖范围：

| 分组 | 内容 |
| --- | --- |
| 输入解析 | 仓库名多种写法归一化、HTTP 状态码中文翻译 |
| 基线流程 | `/ghstar add` 校验与拒绝、首次基线推送、基线随实际值下修 |
| 端点受限 | stargazers 404/403 时降级为「+N 位用户」且**仍正常推送**、失败后不再重复请求、`star_detail_ok` 标记 |
| 增量推送 | Star / Fork / 提交三类推送、`starred_at` 排序取最新、Star 下降不推送 |
| 持久化与生命周期 | 状态往返读写、`initialize` 建任务、`terminate` 幂等销毁 |
| 文案渲染 | 模板自定义生效、非法模板自动回退 |

---

## 📄 许可证

MIT License © 2026 akexin
