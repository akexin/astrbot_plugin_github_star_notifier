"""AstrBot 插件：GitHub 仓库动态推送。

功能概述
--------
监控一个或多个 GitHub 仓库，当出现以下动态时主动推送到指定会话（如 QQ 群）：

* 新的 Star（可附带 Star 用户头像）
* 新的 Fork
* 新的提交记录（仓库更新）

实现方式
--------
采用 **轮询 GitHub REST API** 的方式，无需公网 IP、无需反向代理，开箱即用。
（AstrBot 的 ``register_web_api`` 注册的路由挂在管理面板下且需要鉴权，
无法直接接收 GitHub Webhook 推送，故未采用 Webhook 方案。）

增量判定（v1.0.1 起的关键设计）
-------------------------------
Star / Fork 的「新增数量」**只依赖** ``GET /repos/{owner}/{repo}`` 返回的
``stargazers_count`` 与 ``forks_count``。这两个字段至今仍然公开可用。

而「列出具体是谁点了 Star」所依赖的 ``/repos/{owner}/{repo}/stargazers``
已被 GitHub 收紧：据官方 changelog（2026-06-30）与官方文档，该端点自
**2026 年 7 月** 起 **仅仓库管理员与协作者可访问**，其他人会收到
403 / 404（匿名请求为 401）。

因此本插件把用户名列表视为**可选增强**：能取到就列名字，取不到就只报
「+N 位用户」，并且一旦确认不可用就停止重复请求，避免浪费 API 额度。
**推送本身永远不会因为该限制而失效。**

网络加速与镜像（v1.0.2 起）
---------------------------
部分服务器（尤其是国内主机）**无法访问 ``api.github.com``**，表现为
``网络请求失败（无法连接 api.github.com）``。为此插件支持**多端点自动切换**：

1. **自定义 API 地址**（``custom_api_base``，可选，优先级最高）
2. **直连** ``https://api.github.com``
3. **镜像**（``github_api_mirrors``，按配置顺序尝试）

请求时按上述顺序逐个尝试，**第一个成功的端点会被记住**并在后续请求中优先使用，
从而避免每次都先等直连超时。镜像支持两种写法：

* 前缀式：``https://gh-proxy.com/https://api.github.com`` → 直接拼接路径
* 模板式：``https://api.allorigins.win/raw?url={url}`` → ``{url}`` 替换为
  完整且已 URL 编码的目标地址

出于安全考虑，**默认不会把 GitHub Token 发送给第三方镜像**
（``mirror_send_token`` 可显式打开）。使用 ``/ghstar ping`` 可手动检测
全部端点的连通性与耗时，并把可用的最快端点设为当前使用端点。

数据持久化
----------
状态文件保存在 ``data/plugin_data/astrbot_plugin_github_star_notifier/state.json``，
存放订阅会话列表、各仓库的 Star/Fork/最新提交基线，以及每次检查的进度
（检查次数、最近检查时间、最近成功时间、最近错误），插件更新或重装不会丢失。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import AtAll, Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_github_star_notifier"
PLUGIN_VERSION = "v1.0.2"
API_BASE = "https://api.github.com"
CST = timezone(timedelta(hours=8))

DEFAULT_API_MIRRORS = [
    "https://gh-proxy.com/https://api.github.com",
    "https://hk.gh-proxy.com/https://api.github.com",
]
"""默认镜像列表（2026-10-11 于真实网络实测通过）。

* ``gh-proxy.com`` —— 实测最快（约 0.25s），前缀式代理，直接在其后拼接接口路径
* ``hk.gh-proxy.com`` —— 同一家的备用节点，会 302 跳转一次作为兜底

镜像均为第三方服务，随时可能失效，可在插件配置中自由增删。
除前缀式外还支持模板式（``...?url={url}``），适合自建反向代理或通用代理，
详见 ``_build_url``。
"""

PROBE_PATH = "/repos/octocat/Hello-World"
"""连通性探测使用的公开仓库接口，稳定存在且无需鉴权。"""

PLACEHOLDER_REPOS = {
    "owner/repo",
    "owner/name",
    "owner/repository",
    "user/repo",
    "username/repo",
    "your/repo",
    "yourname/repo",
    "org/repo",
    "repo/name",
    "name/repo",
    "example/repo",
    "xxx/xxx",
    "test/test",
    "用户名/仓库名",
    "仓库/名称",
    "作者/仓库",
}
"""帮助文本中的示例占位符。用户误把示例当参数时直接提示，不再发起无效请求。"""

STARGAZER_PAGE_SIZE = 100
"""stargazers 接口单页容量。接口按 ``starred_at`` 升序返回，最新在最后一页。"""

MAX_NAMED_USERS = 20
"""单条推送消息中最多列出多少个用户名，超出以「等 N 位用户」概括。"""

DEFAULT_STAR_TEMPLATE = """🌟 新的 Star！

仓库：{repo}
用户：{user}
Star 总数：{star_count}（+{delta}）
时间：{time}
{url}"""

DEFAULT_FORK_TEMPLATE = """🍴 新的 Fork！

仓库：{repo}
用户：{user}
Fork 总数：{fork_count}（+{delta}）
时间：{time}
{url}"""

DEFAULT_COMMIT_TEMPLATE = """📦 仓库更新

仓库：{repo}
分支：{branch}
新增 {count} 次提交：
{commits}
时间：{time}
{url}"""


@register(
    PLUGIN_NAME,
    "akexin",
    "监控 GitHub 仓库的 Star / Fork / 提交动态，并推送到 QQ 群等会话。",
    PLUGIN_VERSION,
    "https://github.com/akexin/astrbot_plugin_github_star_notifier",
)
class GithubStarNotifier(Star):
    """GitHub 仓库动态推送插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 数据目录：data/plugin_data/<插件名>/
        self.data_dir = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
        os.makedirs(self.data_dir, exist_ok=True)
        self.state_file = os.path.join(self.data_dir, "state.json")

        self._state: dict = self._load_state()
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._check_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        """插件加载完成时启动后台轮询任务。"""
        self._ensure_session()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"[{PLUGIN_NAME}] {PLUGIN_VERSION} 已启动：轮询间隔 {self._poll_interval()} 秒，"
            f"监控 {len(self._get_repos())} 个仓库，推送目标 {len(self._get_sessions())} 个会话。"
        )

    async def terminate(self) -> None:
        """插件被禁用或重载时清理后台任务与网络连接。"""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

        self._save_state()

    # ------------------------------------------------------------------ #
    # 配置读取辅助
    # ------------------------------------------------------------------ #
    def _conf_bool(self, key: str, default: bool = False) -> bool:
        try:
            return bool(self.config.get(key, default))
        except Exception:
            return default

    def _conf_str(self, key: str, default: str = "") -> str:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return str(value).strip() if value is not None else default

    def _conf_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _poll_interval(self) -> int:
        """轮询间隔（秒），下限 30 秒以避免过于频繁地请求 GitHub。"""
        return max(30, self._conf_int("poll_interval", 120))

    def _get_repos(self) -> list[str]:
        """从配置读取并规范化待监控仓库列表。"""
        raw = self.config.get("repositories") or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,;]+", raw)
        result: list[str] = []
        for item in raw:
            repo = self._normalize_repo(item)
            if repo and repo not in result:
                result.append(repo)
        return result

    def _get_sessions(self) -> list[str]:
        """推送目标会话 = 配置项 target_sessions ∪ 通过指令订阅的会话。"""
        merged: list[str] = []
        raw = self.config.get("target_sessions") or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,;]+", raw)
        for item in raw:
            value = str(item).strip()
            if value and value not in merged:
                merged.append(value)
        for value in self._state.get("sessions", []):
            value = str(value).strip()
            if value and value not in merged:
                merged.append(value)
        return merged

    @staticmethod
    def _normalize_repo(raw: object) -> str:
        """把各种写法的仓库地址统一成 ``owner/repo``，无法解析时返回空串。"""
        if not raw:
            return ""
        text = str(raw).strip().strip("\"'`（()）[]【】《》「」<>").strip()
        for prefix in (
            "https://github.com/",
            "http://github.com/",
            "github.com/",
            "git@github.com:",
        ):
            if text.lower().startswith(prefix):
                text = text[len(prefix) :]
                break
        if text.endswith(".git"):
            text = text[:-4]
        # 逐段 strip，容忍 "owner / repo"、"owner/repo/" 等写法。
        parts = [p.strip() for p in text.split("/")]
        parts = [p for p in parts if p]
        if len(parts) < 2:
            return ""
        return f"{parts[0]}/{parts[1]}"

    def _pick_repo(self, tokens: list[str]) -> tuple[str, list[str], bool]:
        """从指令参数中挑出真实仓库名。

        GitHub 的帮助文本写的是 ``/ghstar add owner/repo``，不少用户会把
        ``owner/repo`` 一起敲进去（``/ghstar add owner/repo akiooo/mcd-save-master``），
        导致插件去请求并不存在的占位符仓库。此处会：

        * 按空白 / 逗号 / 分号切分所有参数
        * 跳过占位符（``owner/repo`` 等）与非法片段
        * 返回第一个**看起来真实**的仓库名

        返回 ``(仓库名, 被忽略的片段列表, 是否命中占位符)``。
        """
        pieces: list[str] = []
        for token in tokens or []:
            pieces.extend(p for p in re.split(r"[\s,;，；]+", str(token)) if p.strip())

        repo = ""
        ignored: list[str] = []
        placeholder_hit = False
        for piece in pieces:
            piece = piece.strip()
            normalized = self._normalize_repo(piece)
            if not normalized:
                ignored.append(piece)
                continue
            if normalized.lower() in PLACEHOLDER_REPOS:
                placeholder_hit = True
                ignored.append(piece)
                continue
            if not repo:
                repo = normalized
            else:
                ignored.append(piece)
        return repo, ignored, placeholder_hit

    # ------------------------------------------------------------------ #
    # API 端点（直连 / 自定义 / 镜像）
    # ------------------------------------------------------------------ #
    def _mirror_list(self) -> list[str]:
        """读取镜像列表；配置中缺失该键时回退到内置默认镜像。"""
        raw = self.config.get("github_api_mirrors")
        if raw is None:
            raw = list(DEFAULT_API_MIRRORS)
        if isinstance(raw, str):
            raw = re.split(r"[\s,;\n]+", raw)
        result: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                url = str(item or "").strip().rstrip("/") or ""
                if not url.startswith("http") or url == API_BASE:
                    continue
                if url not in result:
                    result.append(url)
        return result

    def _api_bases(self) -> list[str]:
        """按优先级返回候选端点：自定义 → 直连 → 镜像。"""
        bases: list[str] = []
        custom = self._conf_str("custom_api_base").rstrip("/")
        if custom.startswith("http"):
            bases.append(custom)
        bases.append(API_BASE)
        if self._conf_bool("auto_mirror", True):
            bases.extend(b for b in self._mirror_list() if b not in bases)
        return bases

    def _ordered_bases(self) -> list[str]:
        """把上次成功的端点提到最前，避免每次都先等直连超时。"""
        bases = self._api_bases()
        known = str(self._state.get("api_base") or "")
        if known and known in bases:
            return [known] + [b for b in bases if b != known]
        return bases

    @staticmethod
    def _endpoint_label(base: str) -> str:
        """端点的可读名称。"""
        if not base:
            return "未知端点"
        if base == API_BASE:
            return "直连 api.github.com"
        host = urlsplit(base).netloc or base
        return f"{host}（镜像）"

    def _remember_base(self, base: str) -> None:
        if base and self._state.get("api_base") != base:
            self._state["api_base"] = base
            self._state["api_base_at"] = self._now()
            logger.info(f"[{PLUGIN_NAME}] 当前使用 API 端点：{self._endpoint_label(base)}")

    def _current_endpoint(self) -> str:
        return str(self._state.get("api_base") or "")

    def _token_allowed(self, base: str) -> bool:
        """是否允许把 Token 发给该端点。

        非官方端点一律不发，避免把个人访问令牌泄露给第三方镜像；
        确需透传时可在配置中打开 ``mirror_send_token``。
        """
        if base.startswith(API_BASE):
            return True
        return self._conf_bool("mirror_send_token")

    def _headers_for(self, base: str, accept: str | None = None) -> dict:
        headers = {
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._conf_str("github_token")
        if token and self._token_allowed(base):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _build_url(self, base: str, path: str, params: dict | None) -> tuple[str, dict | None]:
        """构造请求地址，兼容前缀式与 ``{url}`` 模板式镜像。

        返回 ``(url, 仍需交给 aiohttp 拼接的查询参数)``。模板式镜像会把查询
        参数编码进 URL，因此第二个元素为 ``None``。
        """
        target = f"{API_BASE}{path}"
        if params:
            target = f"{target}?{urlencode({k: str(v) for k, v in params.items()})}"
        if "{url}" in base:
            return base.replace("{url}", quote(target, safe="")), None
        return f"{base}{path}", params

    # ------------------------------------------------------------------ #
    # 状态持久化
    # ------------------------------------------------------------------ #
    def _load_state(self) -> dict:
        state = {"sessions": [], "repos": {}}
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if isinstance(data.get("sessions"), list):
                        state["sessions"] = data["sessions"]
                    if isinstance(data.get("repos"), dict):
                        state["repos"] = data["repos"]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 读取状态文件失败：{exc}")
        return state

    def _save_state(self) -> None:
        """原子写入状态文件，避免进程中断导致文件损坏。"""
        try:
            tmp = f"{self.state_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 保存状态文件失败：{exc}")

    def _save_config(self) -> None:
        try:
            self.config.save_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 保存插件配置失败：{exc}")

    # ------------------------------------------------------------------ #
    # 后台轮询
    # ------------------------------------------------------------------ #
    async def _poll_loop(self) -> None:
        # 稍等片刻，等待 AstrBot 平台适配器就绪。
        await asyncio.sleep(5)
        while True:
            try:
                if self._conf_bool("enabled", True):
                    await self._check_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 轮询过程中出现异常：{exc}", exc_info=True)
            await asyncio.sleep(self._poll_interval())

    async def _check_all(self) -> list[str]:
        """检查所有监控仓库，返回每个仓库的一行结果摘要。"""
        repos = self._get_repos()
        if not repos:
            return []
        summaries: list[str] = []
        async with self._check_lock:
            for repo in repos:
                try:
                    summaries.append(await self._check_repo(repo))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[{PLUGIN_NAME}] 检查仓库 {repo} 失败：{exc}")
                    summaries.append(f"❌ {repo}：{exc}")
                self._save_state()
                # 轻微间隔，避免短时间内打满 GitHub 速率限制。
                await asyncio.sleep(1.2)
        return summaries

    async def _check_repo(self, repo: str, info: dict | None = None) -> str:
        """检查单个仓库并推进其推送进度，返回一行人类可读的结果摘要。

        ``info`` 若已由调用方取到可直接传入，避免重复请求。
        """
        record: dict = self._state["repos"].setdefault(repo, {})
        record["checks"] = int(record.get("checks") or 0) + 1
        record["last_check_at"] = self._now()

        if info is None:
            status, info = await self._request(f"/repos/{repo}")
            if status != 200 or not isinstance(info, dict):
                error = self._describe_error(status, f"/repos/{repo}")
                record["last_error"] = error
                logger.warning(f"[{PLUGIN_NAME}] {repo} 读取失败：{error}")
                return f"❌ {repo}：{error}"

        stars = int(info.get("stargazers_count") or 0)
        forks = int(info.get("forks_count") or 0)
        default_branch = info.get("default_branch") or "main"
        full_name = info.get("full_name") or repo
        html_url = info.get("html_url") or f"https://github.com/{repo}"

        prev_stars = record.get("stars")
        prev_forks = record.get("forks")
        first_run = prev_stars is None

        record["full_name"] = full_name
        record["url"] = html_url
        record["last_success_at"] = self._now()
        record["last_error"] = ""
        record["endpoint"] = self._endpoint_label(self._current_endpoint() or API_BASE)

        await self._handle_stars(repo, record, first_run, prev_stars, stars, full_name, html_url)
        await self._handle_forks(repo, record, prev_forks, forks, full_name, html_url)
        await self._handle_commits(repo, record, default_branch, full_name, html_url)

        tip = "已建立基线" if first_run else "增量检查"
        return f"✅ {repo}：Star {stars}　Fork {forks}（{tip}）"

    # ------------------------------------------------------------------ #
    # 各类动态处理
    # ------------------------------------------------------------------ #
    async def _handle_stars(
        self,
        repo: str,
        record: dict,
        first_run: bool,
        prev_stars: object,
        stars: int,
        full_name: str,
        html_url: str,
    ) -> None:
        if first_run:
            record["stars"] = stars
            record["baseline_at"] = self._now()
            # 首次建立基线时默认推送一条，便于用户当场确认推送链路是否打通。
            if self._conf_bool("notify_on_first_run", True):
                endpoint = self._endpoint_label(self._current_endpoint() or API_BASE)
                text = (
                    f"👀 已开始监控 {full_name}\n"
                    f"当前 Star：{stars}　Fork：{record.get('forks', 0)}\n"
                    f"API 端点：{endpoint}\n"
                    f"此后新增 Star 会即时推送到本会话。\n"
                    f"{html_url}"
                )
                await self._broadcast(lambda t=text: self._build_chain(t))
            return

        if stars <= int(prev_stars or 0):
            record["stars"] = stars
            return

        delta = stars - int(prev_stars or 0)
        record["stars"] = stars
        if not self._conf_bool("notify_star", True):
            return

        users = await self._fetch_latest_stargazers(repo, record, stars, delta)
        named = self._format_users(users, delta, "位用户")
        if named:
            user_text = named
        elif record.get("star_detail_ok") is False:
            user_text = f"{delta} 位用户（GitHub 已限制该仓库的 Star 用户列表）"
        else:
            user_text = f"{delta} 位用户"

        text = self._render_star_message(
            full_name, html_url, stars, delta, user_text, len(users) or delta
        )
        avatars = [u["avatar"] for u in users if u.get("avatar")] if users else []
        await self._broadcast(lambda t=text, a=avatars: self._build_chain(t, a))

    async def _handle_forks(
        self,
        repo: str,
        record: dict,
        prev_forks: object,
        forks: int,
        full_name: str,
        html_url: str,
    ) -> None:
        if prev_forks is None:
            record["forks"] = forks
            return
        if forks <= int(prev_forks or 0):
            record["forks"] = forks
            return

        delta = forks - int(prev_forks or 0)
        record["forks"] = forks
        if not self._conf_bool("notify_fork"):
            return

        users = await self._fetch_latest_forks(repo, delta)
        user_text = self._format_users(users, delta, "个用户") or f"{delta} 个用户"
        text = self._render_fork_message(
            full_name, html_url, forks, delta, user_text, len(users) or delta
        )
        await self._broadcast(lambda t=text: self._build_chain(t))

    async def _handle_commits(
        self,
        repo: str,
        record: dict,
        default_branch: str,
        full_name: str,
        html_url: str,
    ) -> None:
        known_sha = record.get("sha")
        # 未开启提交推送时，仅在首次建立基线，避免后续开启时一次性刷屏。
        if not self._conf_bool("notify_commit") and known_sha is not None:
            return

        branch = self._conf_str("branch") or default_branch
        limit = max(1, min(30, self._conf_int("max_commits_per_push", 3)))
        commits = await self._gh_get(
            f"/repos/{repo}/commits",
            params={"sha": branch, "per_page": limit},
        )
        if not isinstance(commits, list) or not commits:
            return

        latest_sha = commits[0].get("sha")
        if not known_sha:
            record["sha"] = latest_sha
            record["branch"] = branch
            return
        if latest_sha == known_sha:
            return

        new_commits: list[dict] = []
        for item in commits:
            if item.get("sha") == known_sha:
                break
            new_commits.append(item)

        record["sha"] = latest_sha
        record["branch"] = branch
        if not new_commits or not self._conf_bool("notify_commit"):
            return

        text = self._render_commit_message(full_name, branch, html_url, new_commits[:limit])
        await self._broadcast(lambda t=text: self._build_chain(t))

    # ------------------------------------------------------------------ #
    # GitHub API
    # ------------------------------------------------------------------ #
    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25),
                headers={"User-Agent": f"AstrBot-{PLUGIN_NAME}/{PLUGIN_VERSION}"},
            )
        return self._session

    async def _request(
        self,
        path: str,
        params: dict | None = None,
        accept: str | None = None,
        bases: list[str] | None = None,
    ) -> tuple[int, object | None]:
        """发起 GitHub REST 请求，返回 ``(HTTP 状态码, JSON)``。

        依次尝试候选端点（自定义 → 直连 → 镜像），**遇到网络层失败（状态码 0）
        才切换到下一个端点**；只要能拿到 HTTP 响应就认定该端点可用并记住它。
        全部端点都连不上时返回 ``(0, None)``。
        """
        session = self._ensure_session()
        candidates = list(bases) if bases else self._ordered_bases()
        failed: list[str] = []
        last_status = 0

        for base in candidates:
            status, data = await self._request_once(session, base, path, params, accept)
            if 0 < status < 500:
                # 拿到有效的 HTTP 业务响应即认定该端点可用。
                self._remember_base(base)
                return status, data
            if status:
                # 5xx：端点自身故障（镜像返回 500/502/520 等），换下一个端点重试。
                last_status = status
                logger.warning(
                    f"[{PLUGIN_NAME}] 端点 {self._endpoint_label(base)} 返回 HTTP {status}，"
                    "自动尝试下一个端点。"
                )
            failed.append(self._endpoint_label(base))

        if failed:
            logger.warning(
                f"[{PLUGIN_NAME}] 所有 API 端点均不可用（{path}）：{'、'.join(failed)}。"
                "可使用 /ghstar ping 检测连通性，或在配置中更换镜像。"
            )
        return last_status, None

    async def _request_once(
        self,
        session: aiohttp.ClientSession,
        base: str,
        path: str,
        params: dict | None = None,
        accept: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, object | None]:
        """向单个端点发起请求。网络异常 / 响应非 JSON 时返回 ``(0, None)``。"""
        url, rest_params = self._build_url(base, path, params)
        headers = self._headers_for(base, accept)
        kwargs: dict = {"params": rest_params, "headers": headers}
        if timeout:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)

        try:
            async with session.get(url, **kwargs) as resp:
                status = resp.status
                if status == 403:
                    remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                    logger.warning(
                        f"[{PLUGIN_NAME}] GitHub 返回 403（{path}，剩余额度 {remaining}）。"
                    )
                elif status >= 400:
                    logger.warning(f"[{PLUGIN_NAME}] GitHub 返回 HTTP {status}：{path}")
                if status >= 400:
                    return status, None
                return status, await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[{PLUGIN_NAME}] 请求失败（{self._endpoint_label(base)}，{path}）：{exc}"
            )
            return 0, None

    # ------------------------------------------------------------------ #
    # 连通性检测
    # ------------------------------------------------------------------ #
    async def _probe(self, all_bases: bool = False, timeout: float = 8.0) -> tuple[list[dict], str]:
        """逐个探测候选端点，返回 ``(探测结果列表, 第一个可用端点)``。

        ``all_bases=False`` 时命中第一个可用端点即停止（用于 add 流程，避免等待）；
        ``all_bases=True`` 时探测全部端点（用于 /ghstar ping）。
        """
        session = self._ensure_session()
        results: list[dict] = []
        first_ok = ""
        for base in self._api_bases():
            start = time.perf_counter()
            status, _ = await self._request_once(session, base, PROBE_PATH, timeout=timeout)
            elapsed = max(1, int((time.perf_counter() - start) * 1000))
            ok = 0 < status < 500
            results.append({"base": base, "status": status, "ms": elapsed, "ok": ok})
            if ok:
                if not first_ok:
                    first_ok = base
                if not all_bases:
                    break
        if first_ok:
            self._remember_base(first_ok)
        return results, first_ok

    @staticmethod
    def _probe_lines(results: list[dict], first: int = 1) -> list[str]:
        lines: list[str] = []
        for index, item in enumerate(results, first):
            if item["ok"]:
                lines.append(
                    f"  {index}. ✅ {GithubStarNotifier._endpoint_label(item['base'])}"
                    f"　{item['ms']} ms"
                )
            else:
                lines.append(
                    f"  {index}. ❌ {GithubStarNotifier._endpoint_label(item['base'])}　连接失败"
                )
        return lines

    def _probe_headline(self, results: list[dict], ok_base: str) -> str:
        """把探测结果压成一行状态摘要。"""
        if not ok_base:
            return "🔍 网络检测：❌ GitHub API 在全部端点上均不可达"
        label = self._endpoint_label(ok_base)
        ms = next((r["ms"] for r in results if r["base"] == ok_base), 0)
        if ok_base == API_BASE:
            return f"🔍 网络检测：✅ 直连 api.github.com　{ms} ms"
        direct_failed = any(not r["ok"] for r in results if r["base"] != ok_base)
        if direct_failed:
            return f"🔍 网络检测：⚠️ 直连不可用，已自动切换镜像 ✅ {label}　{ms} ms"
        return f"🔍 网络检测：✅ {label}　{ms} ms"

    @staticmethod
    def _mirror_hint() -> str:
        return (
            "建议：\n"
            "  ① 检查服务器能否访问外网（可先 ping api.github.com）；\n"
            "  ② 在插件配置的「GitHub API 镜像」中增删镜像地址；\n"
            "  ③ 若有自建反向代理，填入「自定义 API 地址」。"
        )

    async def _ping_text(self) -> str:
        """探测全部端点并输出耗时，把可用的最快端点设为当前端点。"""
        results, ok = await self._probe(all_bases=True, timeout=8.0)
        lines = [f"🌐 GitHub API 连通性检测　{PLUGIN_VERSION}"]
        lines.extend(self._probe_lines(results))
        lines.append("")
        if ok:
            lines.append(f"当前使用：{self._endpoint_label(ok)}")
            if ok != API_BASE:
                lines.append("（直连不可用 → 已自动切换镜像加速；镜像为第三方服务，未发送 Token）")
        else:
            lines.append("当前使用：无可用端点")
            lines.append(self._mirror_hint())
        lines.append("")
        lines.append(
            f"自动镜像：{'开' if self._conf_bool('auto_mirror', True) else '关'}"
            f"　镜像数量：{len(self._mirror_list())}"
            f"　Token 透传给镜像：{'是（存在泄露风险）' if self._conf_bool('mirror_send_token') else '否'}"
        )
        return "\n".join(lines)

    async def _gh_get(
        self,
        path: str,
        params: dict | None = None,
        accept: str | None = None,
    ) -> object | None:
        """仅在成功时返回 JSON，失败统一返回 None。"""
        _, data = await self._request(path, params=params, accept=accept)
        return data

    @staticmethod
    def _describe_error(status: int, path: str = "") -> str:
        """把 HTTP 状态码翻译成用户可读的中文说明。"""
        if status == 0:
            return "网络请求失败（直连与镜像均无法连接 GitHub API，可用 /ghstar ping 检测）"
        if status == 401:
            return "Token 无效或已过期，请检查 github_token 配置"
        if status == 403:
            if "stargazers" in path or "subscribers" in path:
                return "GitHub 已限制该端点，仅仓库管理员/协作者可访问"
            return "触发 GitHub 速率限制或 Token 权限不足"
        if status == 404:
            if "stargazers" in path or "subscribers" in path:
                return "GitHub 已限制该端点，仅仓库管理员/协作者可访问"
            return "仓库不存在，或为私有仓库且 Token 无权访问"
        if status == 429:
            return "触发 GitHub 速率限制"
        return f"GitHub API 返回 HTTP {status}"

    async def _fetch_latest_stargazers(
        self,
        repo: str,
        record: dict,
        total: int,
        delta: int,
    ) -> list[dict]:
        """取最近新增的 Star 用户，作为推送消息的增强信息。

        ``/stargazers`` 自 2026 年 7 月起仅对仓库管理员与协作者开放，对他人仓库
        会返回 403/404。此处静默降级：只报「+N 位用户」，并打标记避免反复请求。
        **Star 增量本身由 ``stargazers_count`` 计算，不受该限制影响。**
        """
        if not self._conf_bool("fetch_stargazer_details", True):
            return []
        if record.get("star_detail_ok") is False:
            # 已知不可访问，不再浪费额度重复请求。
            return []

        page = max(1, (total + STARGAZER_PAGE_SIZE - 1) // STARGAZER_PAGE_SIZE)
        status, data = await self._request(
            f"/repos/{repo}/stargazers",
            params={"per_page": STARGAZER_PAGE_SIZE, "page": page},
            accept="application/vnd.github.star+json",
        )
        if status != 200 or not isinstance(data, list) or not data:
            if status in (401, 403, 404):
                record["star_detail_ok"] = False
                logger.info(
                    f"[{PLUGIN_NAME}] {repo} 的 Star 用户列表不可访问（HTTP {status}）："
                    "GitHub 自 2026-07 起将该端点限制为仓库管理员/协作者。"
                    "后续推送只显示新增人数。"
                )
            return []
        record["star_detail_ok"] = True

        items = [x for x in data if isinstance(x, dict)]
        # 接口按 starred_at 升序返回，最新在末尾；优先按时间排序，不依赖分页位置。
        if items and all(x.get("starred_at") for x in items):
            items = sorted(items, key=lambda x: str(x.get("starred_at")))

        take = min(delta, len(items), MAX_NAMED_USERS)
        users: list[dict] = []
        for item in items[-take:] if take else []:
            user = item.get("user") if isinstance(item.get("user"), dict) else item
            login = (user or {}).get("login")
            if login:
                users.append({"login": login, "avatar": user.get("avatar_url") or ""})
        users.reverse()  # 最新的排在前面
        return users

    async def _fetch_latest_forks(self, repo: str, delta: int) -> list[dict]:
        per_page = min(max(delta, 1), 100)
        data = await self._gh_get(
            f"/repos/{repo}/forks",
            params={"per_page": per_page, "sort": "newest"},
        )
        if not isinstance(data, list):
            return []
        users: list[dict] = []
        for item in data[: min(delta, MAX_NAMED_USERS)]:
            if not isinstance(item, dict):
                continue
            owner = item.get("owner") or {}
            name = owner.get("login") or item.get("full_name") or "unknown"
            users.append({"login": name, "url": item.get("html_url") or ""})
        return users

    # ------------------------------------------------------------------ #
    # 消息渲染与推送
    # ------------------------------------------------------------------ #
    @staticmethod
    def _now() -> str:
        return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    def _apply_template(self, key: str, fallback: str, **kwargs: object) -> str:
        template = self._conf_str(key) or fallback
        for candidate in (template, fallback):
            try:
                return candidate.format(**kwargs).strip()
            except (KeyError, IndexError, ValueError):
                continue
        return fallback

    def _render_star_message(
        self,
        repo: str,
        url: str,
        star_count: int,
        delta: int,
        user_text: str,
        user_count: int,
    ) -> str:
        return self._apply_template(
            "star_message_template",
            DEFAULT_STAR_TEMPLATE,
            repo=repo,
            user=user_text,
            star_count=star_count,
            delta=delta,
            user_count=user_count,
            time=self._now(),
            url=url,
        )

    def _render_fork_message(
        self,
        repo: str,
        url: str,
        fork_count: int,
        delta: int,
        user_text: str,
        user_count: int,
    ) -> str:
        return self._apply_template(
            "fork_message_template",
            DEFAULT_FORK_TEMPLATE,
            repo=repo,
            user=user_text,
            fork_count=fork_count,
            delta=delta,
            user_count=user_count,
            time=self._now(),
            url=url,
        )

    def _render_commit_message(self, repo: str, branch: str, url: str, commits: list[dict]) -> str:
        lines: list[str] = []
        for item in commits:
            commit = item.get("commit") or {}
            message = (commit.get("message") or "").strip().splitlines()
            subject = message[0] if message else "(无提交信息)"
            if len(subject) > 80:
                subject = subject[:77] + "..."
            author = (commit.get("author") or {}).get("name") or "unknown"
            short_sha = str(item.get("sha") or "")[:7]
            lines.append(f"· {short_sha} {subject}（{author}）")
        return self._apply_template(
            "commit_message_template",
            DEFAULT_COMMIT_TEMPLATE,
            repo=repo,
            branch=branch,
            count=len(commits),
            commits="\n".join(lines),
            time=self._now(),
            url=url,
        )

    @staticmethod
    def _format_users(users: list[dict], delta: int, unit: str) -> str:
        """拼接用户名列表；无可用用户名时返回空串，由调用方决定兜底文案。"""
        names = [str(u.get("login") or "").strip() for u in users]
        names = [n for n in names if n]
        if not names:
            return ""
        if delta > len(names):
            return "、".join(names) + f" 等 {delta} {unit}"
        return "、".join(names)

    def _build_chain(self, text: str, images: list[str] | None = None) -> MessageChain:
        components: list = []
        if self._conf_bool("mention_all"):
            components.append(AtAll())
            components.append(Plain("\n"))
        components.append(Plain(text))
        for url in images or []:
            if url:
                components.append(Image.fromURL(url))
        return MessageChain(chain=components)

    async def _broadcast(self, builder) -> int:
        """向所有订阅会话推送消息，返回成功推送的会话数。"""
        sessions = self._get_sessions()
        if not sessions:
            logger.warning(
                f"[{PLUGIN_NAME}] 尚未配置推送目标会话，本次消息未发送。"
                "可在目标群聊中发送 /ghstar sub 完成订阅。"
            )
            return 0

        sent = 0
        for umo in sessions:
            try:
                if await self.context.send_message(umo, builder()):
                    sent += 1
                else:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 未找到会话 {umo} 对应的平台实例，消息发送失败。"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 推送到 {umo} 失败：{exc}")
        return sent

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ghstar")
    async def ghstar(self, event: AstrMessageEvent):
        """GitHub 仓库动态推送管理指令（仅管理员）。

        用法：/ghstar [sub|unsub|add|del|list|check|ping|diag|test|help]
        """
        args = self._parse_args(event, "ghstar")
        sub = args[0].lower() if args else "help"
        rest = args[1:]

        if sub in ("help", "-h", "--help", "帮助"):
            yield event.plain_result(self._help_text())
            return

        if sub in ("sub", "订阅"):
            yield event.plain_result(self._subscribe(event.unified_msg_origin))
            return

        if sub in ("unsub", "退订"):
            yield event.plain_result(self._unsubscribe(event.unified_msg_origin))
            return

        if sub in ("add", "添加"):
            yield event.plain_result(await self._add_repo(rest))
            return

        if sub in ("del", "remove", "rm", "删除"):
            yield event.plain_result(self._del_repo(rest))
            return

        if sub in ("list", "ls", "列表"):
            yield event.plain_result(self._list_text())
            return

        if sub in ("diag", "诊断"):
            yield event.plain_result(self._diag_text())
            return

        if sub in ("ping", "net", "network", "网络", "连通性"):
            yield event.plain_result("⏳ 正在检测 GitHub API 连通性（直连与镜像）…")
            yield event.plain_result(await self._ping_text())
            return

        if sub in ("check", "检查", "刷新"):
            yield event.plain_result("⏳ 正在检查所有监控仓库，请稍候…")
            summaries = await self._check_all()
            if not summaries:
                yield event.plain_result("ℹ️ 尚未配置监控仓库，请先使用 /ghstar add owner/repo。")
                return
            ok = sum(1 for line in summaries if line.startswith("✅"))
            text = "\n".join(summaries) + f"\n\n检查完成：{len(summaries)} 个仓库，{ok} 个正常。"
            if ok == 0 and any("网络" in line for line in summaries):
                text += (
                    "\n\n⚠️ 存在网络故障，请用 /ghstar ping 检测连通性；"
                    "如直连不可用，插件会自动尝试镜像（可在配置中增删镜像地址）。"
                )
            yield event.plain_result(text)
            return

        if sub in ("test", "测试"):
            count = await self._broadcast(
                lambda: self._build_chain("🔔 这是一条来自 GitHub Star 推送插件的测试消息。")
            )
            if count:
                yield event.plain_result(f"已向 {count} 个会话发送测试消息。")
            else:
                yield event.plain_result(
                    "⚠️ 未发送成功：尚未配置推送目标会话。请在目标群聊中发送 /ghstar sub 完成订阅。"
                )
            return

        yield event.plain_result(f"未知子命令：{sub}\n\n{self._help_text()}")

    @filter.command("ghsub")
    async def ghsub(self, event: AstrMessageEvent):
        """订阅当前会话接收 GitHub 仓库动态推送。"""
        if not event.is_admin() and not self._conf_bool("allow_member_subscribe"):
            yield event.plain_result(
                "⚠️ 仅管理员可订阅。如需开放给群成员，请在插件配置中开启「允许群成员订阅」。"
            )
            return
        yield event.plain_result(self._subscribe(event.unified_msg_origin))

    @filter.command("ghunsub")
    async def ghunsub(self, event: AstrMessageEvent):
        """取消当前会话的 GitHub 仓库动态推送订阅。"""
        if not event.is_admin() and not self._conf_bool("allow_member_subscribe"):
            yield event.plain_result("⚠️ 仅管理员可退订。")
            return
        yield event.plain_result(self._unsubscribe(event.unified_msg_origin))

    # ------------------------------------------------------------------ #
    # 指令内部实现
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_args(event: AstrMessageEvent, command: str) -> list[str]:
        text = ""
        try:
            text = event.get_plain_text() or ""
        except Exception:  # noqa: BLE001
            text = getattr(event, "message_str", "") or ""
        if not text:
            text = getattr(event, "message_str", "") or ""

        tokens = text.strip().split()
        if tokens:
            head = tokens[0].lstrip("/!！.").lower()
            if head == command.lower():
                tokens = tokens[1:]
        return tokens

    def _subscribe(self, umo: str) -> str:
        if not umo:
            return "⚠️ 无法获取当前会话标识，订阅失败。"
        sessions: list[str] = self._state.setdefault("sessions", [])
        if umo in sessions or umo in self._get_sessions():
            return f"ℹ️ 当前会话已在推送列表中：{umo}"
        sessions.append(umo)
        self._save_state()
        return f"✅ 订阅成功，本会话将接收 GitHub 仓库动态推送。\n会话标识：{umo}"

    def _unsubscribe(self, umo: str) -> str:
        sessions: list[str] = self._state.setdefault("sessions", [])
        removed = False
        if umo in sessions:
            sessions.remove(umo)
            removed = True
        configured = self.config.get("target_sessions") or []
        if isinstance(configured, list) and umo in configured:
            configured.remove(umo)
            self.config["target_sessions"] = configured
            self._save_config()
            removed = True
        self._save_state()
        return "✅ 已取消订阅。" if removed else "ℹ️ 当前会话并未订阅。"

    async def _add_repo(self, rest: list[str]) -> str:
        """添加监控仓库：先检测网络连通性，再校验仓库是否存在，最后建立基线。"""
        repo, ignored, placeholder_hit = self._pick_repo(rest)
        if not repo:
            if placeholder_hit:
                return (
                    "⚠️ owner/repo 只是帮助里的示例占位符，不是真实仓库。\n"
                    "用法：/ghstar add 用户名/仓库名\n"
                    f"例如：/ghstar add akexin/{PLUGIN_NAME}\n"
                    "也支持直接粘贴链接：/ghstar add https://github.com/用户名/仓库名"
                )
            if ignored:
                return (
                    f"⚠️ 无法解析仓库名：{'、'.join(ignored)}\n"
                    "用法：/ghstar add owner/repo\n"
                    f"例如：/ghstar add akexin/{PLUGIN_NAME}"
                )
            return f"用法：/ghstar add owner/repo\n例如：/ghstar add akexin/{PLUGIN_NAME}"

        repos = self.config.get("repositories") or []
        if not isinstance(repos, list):
            repos = []
        if any(self._normalize_repo(str(item)) == repo for item in repos):
            return f"ℹ️ 该仓库已在监控列表中：{repo}"

        # ① 先检测网络：直连不可达时自动切换镜像，并把检测状态回报给用户。
        results, ok_base = await self._probe()
        head = self._probe_headline(results, ok_base)
        if not ok_base:
            return (
                f"{head}\n" + "\n".join(self._probe_lines(results)) + "\n\n" + self._mirror_hint()
            )

        # ② 再校验仓库是否存在，避免把占位符或拼错的仓库名写进配置后静默失效。
        status, info = await self._request(f"/repos/{repo}")
        if status != 200 or not isinstance(info, dict):
            return f"{head}\n❌ 无法添加 {repo}：{self._describe_error(status, f'/repos/{repo}')}"

        repos.append(repo)
        self.config["repositories"] = repos
        self._save_config()
        self._save_state()

        # ③ 立即建立基线（并默认推送一条「已开始监控」），让用户当场看到效果。
        summary = await self._check_repo(repo, info=info)
        self._save_state()

        note = f"\n（已忽略无法识别的片段：{'、'.join(ignored)}）" if ignored else ""
        return f"{head}\n✅ 已添加监控仓库：{repo}{note}\n{summary}"

    def _del_repo(self, rest: list[str]) -> str:
        repo, ignored, placeholder_hit = self._pick_repo(rest)
        if not repo:
            if placeholder_hit:
                return (
                    "⚠️ owner/repo 只是示例占位符，请填写真实仓库名。\n"
                    "用法：/ghstar del 用户名/仓库名"
                )
            if ignored:
                return f"⚠️ 无法解析仓库名：{'、'.join(ignored)}。\n用法：/ghstar del owner/repo"
            return "用法：/ghstar del owner/repo\n例如：/ghstar del akexin/astrbot_plugin_github_star_notifier"

        repos = self.config.get("repositories") or []
        if not isinstance(repos, list):
            repos = []
        kept = [item for item in repos if self._normalize_repo(str(item)) != repo]
        if len(kept) == len(repos):
            return f"ℹ️ 监控列表中不存在该仓库：{repo}"

        self.config["repositories"] = kept
        self._save_config()
        self._state["repos"].pop(repo, None)
        self._save_state()
        return f"✅ 已移除监控仓库：{repo}"

    def _repo_status_line(self, repo: str) -> str:
        """单行渲染某个仓库的进度状态。"""
        record = self._state["repos"].get(repo)
        if not record:
            return f"  · {repo}（⏳ 等待首次检查）"
        if record.get("last_error"):
            return f"  · {repo}（❌ {record['last_error']}）"
        if "stars" not in record:
            return f"  · {repo}（⏳ 等待首次检查）"
        baseline_at = str(record.get("baseline_at") or "")[5:16].replace("-", "/")
        suffix = f" · 基线 {baseline_at}" if baseline_at else ""
        return f"  · {repo}（Star {record['stars']}{suffix}）"

    def _list_text(self) -> str:
        repos = self._get_repos()
        sessions = self._get_sessions()
        lines = [f"📋 GitHub 推送插件状态　{PLUGIN_VERSION}"]
        lines.append(
            f"运行状态：{'✅ 已启用' if self._conf_bool('enabled', True) else '⛔ 已停用'}"
        )
        lines.append(f"轮询间隔：{self._poll_interval()} 秒")
        lines.append(
            f"推送开关：Star {'开' if self._conf_bool('notify_star', True) else '关'} / "
            f"Fork {'开' if self._conf_bool('notify_fork') else '关'} / "
            f"提交 {'开' if self._conf_bool('notify_commit') else '关'}"
        )
        lines.append(
            f"GitHub Token：{'已配置' if self._conf_str('github_token') else '未配置（速率受限）'}"
        )
        endpoint = self._current_endpoint()
        lines.append(
            "API 端点："
            + (self._endpoint_label(endpoint) if endpoint else "— 尚未探测（可执行 /ghstar ping）")
        )
        lines.append("")
        if repos:
            lines.append(f"监控仓库（{len(repos)}）：")
            lines.extend(self._repo_status_line(repo) for repo in repos)
        else:
            lines.append("监控仓库：无，请使用 /ghstar add owner/repo 添加。")
        lines.append("")
        if sessions:
            lines.append(f"推送目标（{len(sessions)}）：")
            lines.extend(f"  · {umo}" for umo in sessions)
        else:
            lines.append("推送目标：无，请使用 /ghstar sub 在当前会话订阅。")
        return "\n".join(lines)

    def _diag_text(self) -> str:
        """逐仓库输出推送进度诊断信息。"""
        repos = self._get_repos()
        sessions = self._get_sessions()
        lines = [f"🩺 GitHub 推送插件诊断　{PLUGIN_VERSION}"]
        lines.append(
            f"运行状态：{'✅ 已启用' if self._conf_bool('enabled', True) else '⛔ 已停用'}"
        )
        lines.append(f"轮询间隔：{self._poll_interval()} 秒")
        lines.append(
            f"GitHub Token：{'✅ 已配置' if self._conf_str('github_token') else '⛔ 未配置'}"
        )
        endpoint = self._current_endpoint()
        lines.append(
            "API 端点："
            + (self._endpoint_label(endpoint) if endpoint else "— 尚未探测（可执行 /ghstar ping）")
        )
        lines.append(
            f"自动镜像：{'开' if self._conf_bool('auto_mirror', True) else '关'}"
            f"（{len(self._mirror_list())} 个）"
            f"　Token 透传镜像：{'是' if self._conf_bool('mirror_send_token') else '否'}"
        )
        lines.append(f"推送目标：{len(sessions)} 个会话")
        lines.append(f"数据文件：{self.state_file}")
        lines.append("")

        if not repos:
            lines.append("监控仓库：无。请使用 /ghstar add owner/repo 添加。")
            return "\n".join(lines)

        for repo in repos:
            record = self._state["repos"].get(repo, {})
            lines.append(f"📦 {repo}")
            if "stars" in record:
                lines.append(
                    f"　基线时间：{record.get('baseline_at') or '未知'}（Star {record['stars']}）"
                )
            else:
                lines.append("　基线时间：—（尚未成功建立基线）")
            lines.append(
                f"　最近检查：{record.get('last_check_at') or '—'}"
                f"（累计 {record.get('checks') or 0} 次）"
            )
            lines.append(f"　最近成功：{record.get('last_success_at') or '—'}")
            if record.get("endpoint"):
                lines.append(f"　所用端点：{record['endpoint']}")
            if record.get("last_error"):
                lines.append(f"　最近错误：❌ {record['last_error']}")

            detail = record.get("star_detail_ok")
            if detail is True:
                lines.append("　Star 用户列表：✅ 可访问")
            elif detail is False:
                lines.append("　Star 用户列表：⛔ 不可访问（GitHub 2026-07 起限制为管理员/协作者）")
            else:
                lines.append("　Star 用户列表：— 尚未请求")
            lines.append("")

        lines.append("提示：如需重试 Star 用户列表，先 /ghstar del 再 /ghstar add 即可重置标记。")
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "📖 GitHub 仓库动态推送插件\n"
            "────────────────\n"
            "管理员指令：\n"
            "/ghstar sub　　　在当前会话订阅推送\n"
            "/ghstar unsub　　取消当前会话订阅\n"
            "/ghstar add owner/repo　添加监控仓库（检测网络 → 校验仓库 → 建立基线）\n"
            "/ghstar del owner/repo　移除监控仓库\n"
            "/ghstar list　　 查看当前状态\n"
            "/ghstar check　　立即检查一次\n"
            "/ghstar ping　　 检测 GitHub API 连通性（直连与镜像）\n"
            "/ghstar diag　　 查看推送进度诊断\n"
            "/ghstar test　　 发送测试消息\n"
            "/ghstar help　　 显示本帮助\n"
            "\n"
            "添加仓库时可只写 owner/repo，也可直接粘贴完整链接；\n"
            "若直连 api.github.com 不通，插件会自动切换到可用镜像加速。\n"
            "\n"
            "群成员指令：\n"
            "/ghsub　 订阅（需在配置中开启「允许群成员订阅」）\n"
            "/ghunsub 退订\n"
            "\n"
            "更多设置请在 AstrBot 管理面板的插件配置中调整。"
        )
