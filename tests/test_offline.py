"""v1.0.1 离线回归测试：桩掉 astrbot / aiohttp，直接验证核心逻辑。

运行方式（无需安装 AstrBot，仅需 Python 3.10+）：

    python tests/test_offline.py

退出码为 0 表示全部通过。

覆盖：仓库名解析、HTTP 错误翻译、首次基线推送、stargazers 受限降级、
已知受限后不再重复请求、按 starred_at 排序、add 校验、Fork/提交推送、
list/diag 文案、状态持久化、生命周期、轮询间隔下限。
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import types

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(tempfile.gettempdir(), "ghstar_test_data")
shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT, exist_ok=True)
DATA_ROOT = {"dir": os.path.join(ROOT, "default")}

PASSED = 0
FAILED: list[str] = []


def check(cond, label):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


# --------------------------------------------------------------------- #
# 桩模块
# --------------------------------------------------------------------- #
def make_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class FakeConfig(dict):
    def save_config(self):
        pass


class FakeLogger:
    def __init__(self):
        self.records = []

    def _log(self, *args, **kwargs):
        self.records.append(" ".join(str(a) for a in args))

    info = warning = error = debug = _log

    def __getattr__(self, item):
        return self._log


class MessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


class Plain:
    def __init__(self, text=""):
        self.text = text


class AtAll:
    pass


class Image:
    def __init__(self, url=""):
        self.url = url

    @staticmethod
    def fromURL(url):
        return Image(url)


class PermissionType:
    ADMIN = "admin"


class Filter:
    PermissionType = PermissionType

    def _deco(self, *args, **kwargs):
        def wrap(fn):
            return fn

        return wrap

    command = _deco
    permission_type = _deco
    event_message_type = _deco


class Star:
    def __init__(self, context=None):
        self.context = context


def register(*args, **kwargs):
    def wrap(cls):
        return cls

    return wrap


class ClientTimeout:
    def __init__(self, **kwargs):
        pass


class ClientSession:
    def __init__(self, **kwargs):
        self.closed = False

    async def close(self):
        self.closed = True


LOGGER = FakeLogger()
make_module("aiohttp", ClientSession=ClientSession, ClientTimeout=ClientTimeout)
api_mod = make_module("astrbot.api", AstrBotConfig=FakeConfig, logger=LOGGER)
make_module("astrbot", api=api_mod)
make_module(
    "astrbot.api.event", AstrMessageEvent=object, MessageChain=MessageChain, filter=Filter()
)
make_module("astrbot.api.message_components", AtAll=AtAll, Image=Image, Plain=Plain)
make_module("astrbot.api.star", Context=object, Star=Star, register=register)
make_module("astrbot.core")
make_module("astrbot.core.utils")
make_module(
    "astrbot.core.utils.astrbot_path",
    get_astrbot_plugin_data_path=lambda: DATA_ROOT["dir"],
)

sys.path.insert(0, PLUGIN_DIR)
import main as P  # noqa: E402

REPO = "owner/repo"
DEFAULT_INFO = {
    "full_name": REPO,
    "html_url": "https://github.com/owner/repo",
    "default_branch": "main",
    "stargazers_count": 10,
    "forks_count": 1,
}

ROUTES: dict[str, tuple[int, object]] = {}
CALLS: list[str] = []


def set_route(path, status=200, data=None):
    ROUTES[path] = (status, data)


async def fake_request(path, params=None, accept=None):
    CALLS.append(path)
    if path in ROUTES:
        status, data = ROUTES[path]
        return status, json.loads(json.dumps(data)) if data is not None else None
    return 404, None


class FakeContext:
    def __init__(self):
        self.sent: list[tuple[str, MessageChain]] = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


def texts(ctx):
    out = []
    for _, chain in ctx.sent:
        out.append("".join(c.text for c in chain.chain if isinstance(c, Plain)))
    return out


def load_default_config():
    with open(os.path.join(PLUGIN_DIR, "_conf_schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    cfg = FakeConfig()
    for key, meta in schema.items():
        cfg[key] = meta.get("default")
    return cfg


def new_plugin(config=None, group="default"):
    """新建插件实例。group 用于隔离数据目录，避免用例之间互相污染。"""
    DATA_ROOT["dir"] = os.path.join(ROOT, group)
    os.makedirs(DATA_ROOT["dir"], exist_ok=True)
    ctx = FakeContext()
    plugin = P.GithubStarNotifier(ctx, config if config is not None else load_default_config())
    plugin._request = fake_request
    return plugin, ctx


async def main():
    print("== T1 仓库名解析 ==")
    cases = [
        ("owner/repo", "owner/repo"),
        ("https://github.com/a/b", "a/b"),
        ("http://github.com/a/b.git", "a/b"),
        ("git@github.com:a/b.git", "a/b"),
        ("github.com/a/b/", "a/b"),
        (" a / b ", "a/b"),
        ("justone", ""),
        ("", ""),
        (None, ""),
        ("owner/repo/tree/main", "owner/repo"),
    ]
    for raw, exp in cases:
        got = P.GithubStarNotifier._normalize_repo(raw)
        check(got == exp, f"normalize({raw!r}) -> {got!r} != {exp!r}")

    print("== T2 HTTP 错误翻译 ==")
    d = P.GithubStarNotifier._describe_error
    check("管理员" in d(404, f"/repos/{REPO}/stargazers"), "404 stargazers 说明受限")
    check("仓库不存在" in d(404, f"/repos/{REPO}"), "404 repo 说明不存在")
    check("网络" in d(0), "status 0 说明网络失败")
    check("Token" in d(401), "401 说明 Token 无效")
    check("速率" in d(403, f"/repos/{REPO}"), "403 repo 说明限流")
    check("管理员" in d(403, f"/repos/{REPO}/stargazers"), "403 stargazers 说明受限")

    print("== T3 默认配置与轮询间隔 ==")
    plugin, ctx = new_plugin(group="t3")
    check(plugin.config["notify_on_first_run"] is True, "notify_on_first_run 默认 True")
    check(plugin.config["fetch_stargazer_details"] is True, "fetch_stargazer_details 默认 True")
    check(plugin._poll_interval() == 120, f"默认轮询间隔 120，实际 {plugin._poll_interval()}")
    plugin.config["poll_interval"] = 5
    check(plugin._poll_interval() == 30, "轮询间隔下限 30")
    plugin.config["poll_interval"] = 120

    print("== T4 订阅 + add 立即校验并建立基线 + 首次推送 ==")
    plugin._subscribe("test:GroupMessage:1000")
    set_route(f"/repos/{REPO}", 200, DEFAULT_INFO)
    before = len(ctx.sent)
    reply = await plugin._add_repo([REPO])
    check(reply.startswith("✅"), f"add 成功回执：{reply!r}")
    check("Star 10" in reply, "add 回执包含当前 Star 数")
    check("已建立基线" in reply, "add 回执标明已建立基线")
    check(plugin.config["repositories"] == [REPO], "仓库已写入配置")
    check(len(ctx.sent) == before + 1, "首次基线推送恰好 1 条")
    joined = "\n".join(texts(ctx))
    check("已开始监控" in joined, "首次推送为「已开始监控」文案")
    check("新增 Star 会即时推送" in joined, "首次推送说明了后续行为")
    rec = plugin._state["repos"][REPO]
    check(rec["stars"] == 10, "基线 Star 数记录正确")
    check(bool(rec.get("baseline_at")), "baseline_at 已写入")
    check(rec.get("checks") == 1, "累计检查次数为 1")
    check(rec.get("last_error") == "", "last_error 已清空")

    print("== T5 stargazers 受限(404) 时降级但仍推送 ==")
    set_route(f"/repos/{REPO}", 200, {**DEFAULT_INFO, "stargazers_count": 11})
    set_route(f"/repos/{REPO}/stargazers", 404, None)
    CALLS.clear()
    before = len(ctx.sent)
    summary = await plugin._check_repo(REPO)
    check(summary.startswith("✅"), f"检查摘要正常：{summary!r}")
    check("增量检查" in summary, "摘要标明增量检查")
    check(len(ctx.sent) == before + 1, "受限时仍然成功推送 1 条")
    msg = texts(ctx)[-1]
    check("新的 Star" in msg, "推送为 Star 模板")
    check("+1" in msg, "推送包含增量 +1")
    check("1 位用户" in msg, "降级为「1 位用户」")
    check("GitHub 已限制" in msg, "消息中说明了 GitHub 的限制")
    check(plugin._state["repos"][REPO]["star_detail_ok"] is False, "star_detail_ok 标记为 False")

    print("== T6 已知受限后不再重复请求 stargazers ==")
    set_route(f"/repos/{REPO}", 200, {**DEFAULT_INFO, "stargazers_count": 12})
    CALLS.clear()
    before = len(ctx.sent)
    await plugin._check_repo(REPO)
    check(f"/repos/{REPO}/stargazers" not in CALLS, "未再请求 stargazers 端点")
    check(len(ctx.sent) == before + 1, "推送仍然成功（+1）")

    print("== T7 stargazers 可用时按 starred_at 取最新 ==")
    plugin2, ctx2 = new_plugin(group="t7")
    items = [
        {"starred_at": "2026-10-09T10:00:00Z", "user": {"login": "u1", "avatar_url": "a1"}},
        {"starred_at": "2026-10-09T10:05:00Z", "user": {"login": "u2", "avatar_url": "a2"}},
        {"starred_at": "2026-10-09T10:10:00Z", "user": {"login": "u3", "avatar_url": "a3"}},
        {"starred_at": "2026-10-09T10:15:00Z", "user": {"login": "u4", "avatar_url": "a4"}},
    ]
    set_route(f"/repos/{REPO}/stargazers", 200, items)
    users = await plugin2._fetch_latest_stargazers(REPO, {}, 4, 2)
    check([u["login"] for u in users] == ["u4", "u3"], f"取最新两位且最新在前：{users}")
    probe = {}
    await plugin2._fetch_latest_stargazers(REPO, probe, 4, 2)
    check(probe.get("star_detail_ok") is True, "可访问时在记录上标记 star_detail_ok=True")

    print("== T8 stargazers 可用时消息含用户名 ==")
    plugin3, ctx3 = new_plugin(group="t8")
    plugin3._subscribe("t:GroupMessage:1")
    set_route(f"/repos/{REPO}", 200, {**DEFAULT_INFO, "stargazers_count": 4})
    await plugin3._add_repo([REPO])
    set_route(f"/repos/{REPO}", 200, {**DEFAULT_INFO, "stargazers_count": 6})
    set_route(f"/repos/{REPO}/stargazers", 200, items)
    before = len(ctx3.sent)
    await plugin3._check_repo(REPO)
    msg = texts(ctx3)[-1]
    check(len(ctx3.sent) == before + 1, "可用时推送 1 条")
    check("u4" in msg and "u3" in msg, f"消息列出用户名：{msg!r}")
    check("已限制" not in msg, "可用时不应出现限制说明")
    check(plugin3._state["repos"][REPO]["star_detail_ok"] is True, "star_detail_ok 标记为 True")

    print("== T9 add 校验失败的仓库 ==")
    plugin4, ctx4 = new_plugin(group="t9")
    set_route("/repos/nope/missing", 404, None)
    reply = await plugin4._add_repo(["nope/missing"])
    check(reply.startswith("❌"), f"拒绝并给出错误：{reply!r}")
    check("仓库不存在" in reply, "错误说明为仓库不存在")
    check(plugin4.config["repositories"] == [], "失败时未写入配置")
    check(ctx4.sent == [], "失败时未推送")
    check(
        plugin4._state["repos"].get("nope/missing") is None,
        "失败时不创建状态记录（避免污染 list）",
    )

    print("== T10 占位符 owner/repo 提示 ==")
    reply = await plugin4._add_repo(["justone"])
    check(reply.startswith("⚠️"), f"无法解析时给出格式提示：{reply!r}")
    check("owner/repo" in reply, "提示含格式示例")

    print("== T11 检查失败时写入 last_error 并在 list 中体现 ==")
    plugin5, ctx5 = new_plugin(group="t11")
    plugin5.config["repositories"] = ["nope/missing"]
    set_route("/repos/nope/missing", 404, None)
    summaries = await plugin5._check_all()
    check(summaries and summaries[0].startswith("❌"), f"检查摘要为错误：{summaries}")
    rec = plugin5._state["repos"]["nope/missing"]
    check("仓库不存在" in rec["last_error"], "last_error 已记录")
    check(rec.get("last_check_at"), "last_check_at 已记录")
    check("baseline_at" not in rec, "未建立基线时不写 baseline_at")
    listing = plugin5._list_text()
    check("❌" in listing and "仓库不存在" in listing, f"list 体现错误：{listing!r}")
    check("等待首次检查" not in listing, "有错误时不显示「等待首次检查」")

    print("== T12 diag 输出 ==")
    diag = plugin5._diag_text()
    check("诊断" in diag, "diag 标题")
    check("尚未成功建立基线" in diag, "diag 标明未建立基线")
    check("最近错误" in diag, "diag 包含最近错误")
    check("⛔" not in diag or "Star 用户列表" in diag, "diag 含 Star 用户列表状态行")

    plugin6, _ = new_plugin(group="t12")
    plugin6._subscribe("t:GroupMessage:2")
    set_route(f"/repos/{REPO}", 200, DEFAULT_INFO)
    await plugin6._add_repo([REPO])
    set_route(f"/repos/{REPO}", 200, {**DEFAULT_INFO, "stargazers_count": 11})
    set_route(f"/repos/{REPO}/stargazers", 403, None)
    await plugin6._check_repo(REPO)
    diag2 = plugin6._diag_text()
    check("不可访问" in diag2, f"diag 标记 Star 用户列表不可访问：{diag2!r}")
    check("累计 2 次" in diag2, "diag 统计检查次数")
    check("2026-07" in diag2, "diag 说明 GitHub 限制时间")

    print("== T13 Fork 与提交推送 ==")
    plugin7, ctx7 = new_plugin(group="t13")
    plugin7.config["notify_fork"] = True
    plugin7.config["notify_commit"] = True
    plugin7._subscribe("t:GroupMessage:3")
    set_route(f"/repos/{REPO}", 200, DEFAULT_INFO)
    set_route(f"/repos/{REPO}/commits", 200, [{"sha": "oldsha", "commit": {"message": "init"}}])
    await plugin7._add_repo([REPO])
    base = len(ctx7.sent)
    set_route(
        f"/repos/{REPO}",
        200,
        {**DEFAULT_INFO, "stargazers_count": 10, "forks_count": 3},
    )
    set_route(
        f"/repos/{REPO}/forks",
        200,
        [{"owner": {"login": "forker"}, "full_name": "forker/repo", "html_url": "u"}],
    )
    set_route(
        f"/repos/{REPO}/commits",
        200,
        [
            {"sha": "newsha", "commit": {"message": "feat: 新功能", "author": {"name": "ak"}}},
            {"sha": "oldsha", "commit": {"message": "init", "author": {"name": "ak"}}},
        ],
    )
    await plugin7._check_repo(REPO)
    msgs = texts(ctx7)[base:]
    check(any("新的 Fork" in m for m in msgs), "Fork 推送已发出")
    check(any("forker" in m for m in msgs), "Fork 用户已列出")
    check(any("仓库更新" in m for m in msgs), "提交推送已发出")
    check(any("feat: 新功能" in m for m in msgs), "新提交内容正确")
    check(plugin7._state["repos"][REPO]["sha"] == "newsha", "最新 sha 已推进")
    check(plugin7._state["repos"][REPO]["forks"] == 3, "Fork 数已推进")

    print("== T14 Star 数下降不推送 ==")
    set_route(f"/repos/{REPO}", 200, {**DEFAULT_INFO, "stargazers_count": 5, "forks_count": 3})
    before = len(ctx7.sent)
    await plugin7._check_repo(REPO)
    check(len(ctx7.sent) == before, "Star 下降时不推送")
    check(plugin7._state["repos"][REPO]["stars"] == 5, "基线随实际值下修")

    print("== T15 订阅/退订与推送目标 ==")
    p8, ctx8 = new_plugin(group="t15")
    check("订阅成功" in p8._subscribe("t:GroupMessage:9"), "订阅成功")
    check("已在推送列表" in p8._subscribe("t:GroupMessage:9"), "重复订阅被识别")
    check(p8._get_sessions() == ["t:GroupMessage:9"], "推送目标正确")
    check("已取消订阅" in p8._unsubscribe("t:GroupMessage:9"), "退订成功")
    check(p8._get_sessions() == [], "退订后无目标")
    p8.config["target_sessions"] = []
    count = await p8._broadcast(lambda: MessageChain(chain=[Plain("x")]))
    check(count == 0, "无目标时推送返回 0")
    check(any("尚未配置推送目标会话" in r for r in LOGGER.records), "有对应告警日志")

    print("== T16 状态持久化往返 ==")
    p9, _ = new_plugin(group="t16")
    p9._subscribe("t:GroupMessage:77")
    set_route(f"/repos/{REPO}", 200, DEFAULT_INFO)
    await p9._add_repo([REPO])
    p9._save_state()
    p10, _ = new_plugin(group="t16")
    check(p10._get_sessions() == ["t:GroupMessage:77"], "会话持久化")
    check(REPO in p10._state["repos"], "仓库进度持久化")
    check(p10._state["repos"][REPO]["stars"] == 10, "Star 基线持久化")

    print("== T17 生命周期 ==")
    p11, _ = new_plugin(group="t17a")
    await p11.initialize()
    check(p11._task is not None and not p11._task.done(), "initialize 启动轮询任务")
    check(p11._session is not None, "initialize 建立会话")
    await p11.terminate()
    check(p11._task is None, "terminate 清理任务引用")
    check(p11._session is None, "terminate 关闭会话")
    await p11.terminate()
    check(p11._task is None, "重复 terminate 安全")
    p12, _ = new_plugin(group="t17b")
    await p12.terminate()
    check(p12._task is None, "未 initialize 直接 terminate 安全")

    print("== T18 消息模板回退 ==")
    p13, _ = new_plugin(group="t18")
    p13.config["star_message_template"] = "非法模板 {not_exist}"
    set_route(f"/repos/{REPO}", 200, DEFAULT_INFO)
    out = p13._render_star_message(REPO, "u", 3, 1, "某人", 1)
    check("新的 Star" in out, "非法模板回退到内置模板")
    p13.config["star_message_template"] = "🌟 {repo} +{delta} {user}"
    out2 = p13._render_star_message(REPO, "u", 3, 1, "某人", 1)
    check(out2 == f"🌟 {REPO} +1 某人", f"自定义模板生效：{out2!r}")

    print("== T19 空仓库列表与指令回执 ==")
    p14, _ = new_plugin(group="t19")
    check(await p14._check_all() == [], "无仓库时检查返回空列表")
    check("无，请使用 /ghstar add" in p14._list_text(), "list 提示如何添加")
    check("diag" in p14._help_text(), "帮助文本包含 diag 指令")
    check("add owner/repo" in (await p14._add_repo([])), "add 无参数时给出用法")

    print()
    print("=" * 56)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print("  ✗", item)
    return 1 if FAILED else 0


sys.exit(asyncio.run(main()))
