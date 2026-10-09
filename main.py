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

数据持久化
----------
状态文件保存在 ``data/plugin_data/astrbot_plugin_github_star_notifier/state.json``，
存放订阅会话列表与各仓库的 Star/Fork/最新提交基线，插件更新或重装不会丢失。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import AtAll, Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_github_star_notifier"
API_BASE = "https://api.github.com"
CST = timezone(timedelta(hours=8))

STARGAZER_PAGE_SIZE = 100
"""stargazers 接口单页容量，用于定位「最新一页」。"""

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
    "v1.0.0",
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
            f"[{PLUGIN_NAME}] 已启动：轮询间隔 {self._poll_interval()} 秒，"
            f"监控 {len(self._get_repos())} 个仓库，推送目标 {len(self._get_sessions())} 个会话。"
        )

    async def terminate(self) -> None:
        """插件被禁用或重载时清理后台任务与网络连接。"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
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
        return max(30, self._conf_int("poll_interval", 300))

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
        text = str(raw).strip().strip("\"'").strip()
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
        parts = [p for p in text.strip("/").split("/") if p]
        if len(parts) < 2:
            return ""
        return f"{parts[0]}/{parts[1]}"

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
        await asyncio.sleep(10)
        while True:
            try:
                if self._conf_bool("enabled", True):
                    await self._check_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 轮询过程中出现异常：{exc}", exc_info=True)
            await asyncio.sleep(self._poll_interval())

    async def _check_all(self) -> None:
        repos = self._get_repos()
        if not repos:
            return
        async with self._check_lock:
            for repo in repos:
                try:
                    await self._check_repo(repo)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[{PLUGIN_NAME}] 检查仓库 {repo} 失败：{exc}")
                self._save_state()
                # 轻微间隔，避免短时间内打满 GitHub 速率限制。
                await asyncio.sleep(1.2)

    async def _check_repo(self, repo: str) -> None:
        info = await self._gh_get(f"/repos/{repo}")
        if not isinstance(info, dict):
            return

        stars = int(info.get("stargazers_count") or 0)
        forks = int(info.get("forks_count") or 0)
        default_branch = info.get("default_branch") or "main"
        full_name = info.get("full_name") or repo
        html_url = info.get("html_url") or f"https://github.com/{repo}"

        record: dict = self._state["repos"].setdefault(repo, {})
        prev_stars = record.get("stars")
        prev_forks = record.get("forks")
        first_run = prev_stars is None

        record["full_name"] = full_name
        record["url"] = html_url

        await self._handle_stars(repo, record, first_run, prev_stars, stars, full_name, html_url)
        await self._handle_forks(repo, record, prev_forks, forks, full_name, html_url)
        await self._handle_commits(repo, record, first_run, default_branch, full_name, html_url)

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
            if self._conf_bool("notify_on_first_run") and stars > 0:
                text = (
                    f"👀 已开始监控 {full_name}\n"
                    f"当前 Star 数：{stars}　Fork 数：{record.get('forks', 0)}\n"
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

        users = await self._fetch_latest_stargazers(repo, stars, delta)
        text = self._render_star_message(full_name, html_url, stars, delta, users)
        avatars = (
            [u["avatar"] for u in users if u.get("avatar")]
            if self._conf_bool("send_avatar")
            else []
        )
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
        text = self._render_fork_message(full_name, html_url, forks, delta, users)
        await self._broadcast(lambda t=text: self._build_chain(t))

    async def _handle_commits(
        self,
        repo: str,
        record: dict,
        first_run: bool,
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

        text = self._render_commit_message(
            full_name, branch, html_url, new_commits[:limit]
        )
        await self._broadcast(lambda t=text: self._build_chain(t))

    # ------------------------------------------------------------------ #
    # GitHub API
    # ------------------------------------------------------------------ #
    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25),
                headers={"User-Agent": f"AstrBot-{PLUGIN_NAME}/1.0"},
            )
        return self._session

    async def _gh_get(
        self,
        path: str,
        params: dict | None = None,
        accept: str | None = None,
    ) -> object | None:
        """发起 GitHub REST 请求，失败时返回 None（不抛出异常）。"""
        session = self._ensure_session()
        headers = {
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._conf_str("github_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with session.get(
                f"{API_BASE}{path}", params=params, headers=headers
            ) as resp:
                if resp.status == 404:
                    logger.warning(f"[{PLUGIN_NAME}] 资源不存在（404）：{path}")
                    return None
                if resp.status in (403, 429):
                    remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                    logger.warning(
                        f"[{PLUGIN_NAME}] 触发 GitHub API 限流（HTTP {resp.status}，剩余额度 {remaining}）。"
                        "建议在插件配置中填写 github_token。"
                    )
                    return None
                if resp.status >= 400:
                    logger.warning(
                        f"[{PLUGIN_NAME}] GitHub API 返回 HTTP {resp.status}：{path}"
                    )
                    return None
                return await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 请求 GitHub 失败（{path}）：{exc}")
            return None

    async def _fetch_latest_stargazers(
        self, repo: str, total: int, delta: int
    ) -> list[dict]:
        """取「最新一页」stargazers，从中截取最近新增的用户。

        GitHub 的 stargazers 列表按时间升序返回，因此最新加入的用户位于最后一页。
        """
        page = max(1, (total + STARGAZER_PAGE_SIZE - 1) // STARGAZER_PAGE_SIZE)
        data = await self._gh_get(
            f"/repos/{repo}/stargazers",
            params={"per_page": STARGAZER_PAGE_SIZE, "page": page},
            accept="application/vnd.github.star+json",
        )
        if not isinstance(data, list) or not data:
            return []

        take = min(delta, len(data), MAX_NAMED_USERS)
        users: list[dict] = []
        for item in data[-take:] if take else []:
            if isinstance(item, dict) and isinstance(item.get("user"), dict):
                user = item["user"]
            elif isinstance(item, dict):
                user = item
            else:
                continue
            login = user.get("login")
            if login:
                users.append(
                    {"login": login, "avatar": user.get("avatar_url") or ""}
                )
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
        users: list[dict],
    ) -> str:
        return self._apply_template(
            "star_message_template",
            DEFAULT_STAR_TEMPLATE,
            repo=repo,
            user=self._format_users(users, delta, "gazers"),
            star_count=star_count,
            delta=delta,
            user_count=len(users) or delta,
            time=self._now(),
            url=url,
        )

    def _render_fork_message(
        self,
        repo: str,
        url: str,
        fork_count: int,
        delta: int,
        users: list[dict],
    ) -> str:
        return self._apply_template(
            "fork_message_template",
            DEFAULT_FORK_TEMPLATE,
            repo=repo,
            user=self._format_users(users, delta, "forks"),
            fork_count=fork_count,
            delta=delta,
            user_count=len(users) or delta,
            time=self._now(),
            url=url,
        )

    def _render_commit_message(
        self, repo: str, branch: str, url: str, commits: list[dict]
    ) -> str:
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
    def _format_users(users: list[dict], delta: int, suffix: str) -> str:
        names = [str(u.get("login") or "").strip() for u in users]
        names = [n for n in names if n]
        if not names:
            unit = "位用户" if suffix == "gazers" else "个用户"
            return f"{delta}{unit}" if delta > 1 else "未知用户"
        if delta > len(names):
            return "、".join(names) + f" 等 {delta} 位用户"
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

        用法：/ghstar [sub|unsub|add|del|list|check|test|help]
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
            yield event.plain_result(self._add_repo(rest))
            return

        if sub in ("del", "remove", "rm", "删除"):
            yield event.plain_result(self._del_repo(rest))
            return

        if sub in ("list", "ls", "列表"):
            yield event.plain_result(self._list_text())
            return

        if sub in ("check", "检查", "刷新"):
            yield event.plain_result("⏳ 正在检查所有监控仓库，请稍候…")
            await self._check_all()
            yield event.plain_result("✅ 检查完成。")
            return

        if sub in ("test", "测试"):
            count = await self._broadcast(
                lambda: self._build_chain(
                    "🔔 这是一条来自 GitHub Star 推送插件的测试消息。"
                )
            )
            yield event.plain_result(f"已向 {count} 个会话发送测试消息。")
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

    def _add_repo(self, rest: list[str]) -> str:
        if not rest:
            return "用法：/ghstar add owner/repo"
        repo = self._normalize_repo(rest[0])
        if not repo:
            return f"⚠️ 无法解析仓库名：{rest[0]}，请使用 owner/repo 格式。"

        repos = self.config.get("repositories") or []
        if not isinstance(repos, list):
            repos = []
        if any(self._normalize_repo(str(item)) == repo for item in repos):
            return f"ℹ️ 该仓库已在监控列表中：{repo}"

        repos.append(repo)
        self.config["repositories"] = repos
        self._save_config()
        return f"✅ 已添加监控仓库：{repo}\n使用 /ghstar check 可立即检查一次。"

    def _del_repo(self, rest: list[str]) -> str:
        if not rest:
            return "用法：/ghstar del owner/repo"
        repo = self._normalize_repo(rest[0])
        if not repo:
            return f"⚠️ 无法解析仓库名：{rest[0]}。"

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

    def _list_text(self) -> str:
        repos = self._get_repos()
        sessions = self._get_sessions()
        lines = ["📋 GitHub 推送插件状态"]
        lines.append(f"运行状态：{'✅ 已启用' if self._conf_bool('enabled', True) else '⛔ 已停用'}")
        lines.append(f"轮询间隔：{self._poll_interval()} 秒")
        lines.append(f"推送开关：Star {'开' if self._conf_bool('notify_star', True) else '关'} / "
                     f"Fork {'开' if self._conf_bool('notify_fork') else '关'} / "
                     f"提交 {'开' if self._conf_bool('notify_commit') else '关'}")
        lines.append(f"GitHub Token：{'已配置' if self._conf_str('github_token') else '未配置（速率受限）'}")
        lines.append("")
        if repos:
            lines.append(f"监控仓库（{len(repos)}）：")
            for repo in repos:
                record = self._state["repos"].get(repo, {})
                star_text = (
                    f"Star {record['stars']}" if "stars" in record else "尚未建立基线"
                )
                lines.append(f"  · {repo}（{star_text}）")
        else:
            lines.append("监控仓库：无，请使用 /ghstar add owner/repo 添加。")
        lines.append("")
        if sessions:
            lines.append(f"推送目标（{len(sessions)}）：")
            for umo in sessions:
                lines.append(f"  · {umo}")
        else:
            lines.append("推送目标：无，请使用 /ghstar sub 在当前会话订阅。")
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "📖 GitHub 仓库动态推送插件\n"
            "────────────────\n"
            "管理员指令：\n"
            "/ghstar sub　　　在当前会话订阅推送\n"
            "/ghstar unsub　　取消当前会话订阅\n"
            "/ghstar add owner/repo　添加监控仓库\n"
            "/ghstar del owner/repo　移除监控仓库\n"
            "/ghstar list　　 查看当前状态\n"
            "/ghstar check　　立即检查一次\n"
            "/ghstar test　　 发送测试消息\n"
            "/ghstar help　　 显示本帮助\n"
            "\n"
            "群成员指令：\n"
            "/ghsub　 订阅（需在配置中开启「允许群成员订阅」）\n"
            "/ghunsub 退订\n"
            "\n"
            "更多设置请在 AstrBot 管理面板的插件配置中调整。"
        )
