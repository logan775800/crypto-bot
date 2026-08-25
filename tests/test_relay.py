"""频道搬运 /relay —— 把订阅的频道新帖转到自己群里。

他问「怎么把其它小飞机的信息接过来发送到我群组」。

**Bot API 读不到机器人自己不是管理员的频道**，别人的频道不可能给你加管理员，
所以只能走 MTProto（用他自己的账号）。我把"个人账号自动化可能被 Telegram 限制"
讲清楚之后，2026-08-24 他明确选了这条。

所以这里的护栏全都围着一件事：**没配就必须完全不存在**，
不能因为多了个可选功能，让机器人本体多一分挂掉的可能。
"""
import time

import pytest

import storage
from handlers import relay as R


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(storage, "save_data", lambda *a, **k: None)
    monkeypatch.setattr(R, "save_data", lambda *a, **k: None)
    storage.data["relay"] = {"on": False, "target": None, "sources": [],
                             "include": [], "exclude": []}
    R._sent.clear()
    yield
    storage.data["relay"] = {"on": False, "target": None, "sources": [],
                             "include": [], "exclude": []}
    R._sent.clear()


# ── 没配就等于不存在 ─────────────────────────────────────────
def test_not_configured_when_env_is_missing(monkeypatch):
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.delenv(k, raising=False)
    assert R.configured() is False


@pytest.mark.parametrize("missing", ["TG_API_ID", "TG_API_HASH", "TG_SESSION"])
def test_any_missing_var_means_not_configured(monkeypatch, missing):
    """三个缺一个都不算配好——半配状态最危险，会在运行时才炸。"""
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv(missing)
    assert R.configured() is False


def test_start_is_a_noop_when_not_configured(monkeypatch):
    """没配时必须安静返回，不能抛异常——它挂了整个机器人就起不来。"""
    import asyncio
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.delenv(k, raising=False)
    asyncio.run(R.start(None))
    assert R._client is None


def test_panel_tells_you_how_to_configure(monkeypatch):
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.delenv(k, raising=False)
    txt, _kb = R.panel()
    assert "还没配账号" in txt
    assert "my.telegram.org" in txt
    assert "限制账号" in txt, "风险必须写在面板上，不能只在文档里"


def test_compose_whitelists_the_new_vars():
    """docker-compose 的 environment 是**白名单**：变量不在里面，
    就算 .env 里有，容器也读不到。BYBIT_* 上踩过这个坑。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    yml = (root / "docker-compose.yml").read_text(encoding="utf-8")
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        assert k in yml, f"{k} 没进 docker-compose 白名单，容器读不到"


def test_session_secret_is_not_committed():
    """session 等于账号凭证。.env 必须在 gitignore 里，示例里不能有真值。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    assert ".env" in (root / ".gitignore").read_text(encoding="utf-8")
    ex = (root / ".env.example").read_text(encoding="utf-8")
    assert "TG_SESSION=\n" in ex or ex.rstrip().endswith("TG_SESSION=")


# ── 关键词过滤 ──────────────────────────────────────────────
def test_empty_include_means_everything():
    assert R.wanted("随便什么", {})[0] is True


def test_include_is_a_whitelist():
    conf = {"include": ["解锁", "上币"]}
    assert R.wanted("HUMA 解锁 4.59 亿枚代币", conf)[0] is True
    assert R.wanted("今天天气不错", conf)[0] is False


def test_exclude_beats_include():
    """「我要解锁消息，但广告一律不要」——只有 exclude 优先才表达得出这个意图。"""
    conf = {"include": ["解锁"], "exclude": ["广告"]}
    assert R.wanted("解锁消息", conf)[0] is True
    assert R.wanted("解锁相关广告", conf)[0] is False


def test_filter_is_case_insensitive():
    assert R.wanted("BTC unlock", {"include": ["unlock"]})[0] is True
    assert R.wanted("btc UNLOCK", {"include": ["Unlock"]})[0] is True


def test_filter_survives_an_empty_message():
    """纯图片帖没有正文。include 非空时它进不来，但不能崩。"""
    assert R.wanted(None, {})[0] is True
    assert R.wanted(None, {"include": ["解锁"]})[0] is False


# ── 限流：别把群淹了 ────────────────────────────────────────
def test_rate_limit_per_source():
    now = time.time()
    for _ in range(R.MAX_PER_HOUR):
        assert R.rate_ok("A", now) is True
        R.mark("A", now)
    assert R.rate_ok("A", now) is False, "超过上限要拦住"
    assert R.rate_ok("B", now) is True, "另一个频道不该被连累"


def test_rate_window_rolls_off():
    old = time.time() - 3700
    for _ in range(R.MAX_PER_HOUR):
        R.mark("A", old)
    assert R.rate_ok("A") is True, "一小时前的不该再占额度"


# ── 频道名归一化 ────────────────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    ("@BlockBeatsAsia", "BlockBeatsAsia"),
    ("BlockBeatsAsia", "BlockBeatsAsia"),
    ("https://t.me/BlockBeatsAsia", "BlockBeatsAsia"),
    ("https://t.me/s/BlockBeatsAsia", "BlockBeatsAsia"),
    ("-1001234567890", -1001234567890),
    ("", None),
])
def test_normalise_channel(raw, want):
    """他会用各种形式给我：@名字、t.me 链接、私密频道的数字 id。都要认。"""
    assert R._norm(raw) == want


# ── 面板/入口 ───────────────────────────────────────────────
def test_panel_shows_state_when_configured(monkeypatch):
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    c = R.cfg()
    c["sources"] = ["BlockBeatsAsia"]
    c["target"] = -100123
    c["on"] = True
    txt, _kb = R.panel()
    assert "开启中" in txt and "BlockBeatsAsia" in txt
    assert str(R.MAX_PER_HOUR) in txt


def test_channel_names_with_underscores_are_shown_verbatim(monkeypatch):
    """频道名带下划线是常态（他要接的第一个就是 blockbeats_chart）。

    旧版 Markdown 在反引号里**不处理转义**，所以代码块里再 escape_md 的话，
    屏幕上会多出一个反斜杠——他照着复制去 /relay add 就是错的。
    比"难看"严重的是：给出的东西照抄必错。
    """
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    c = R.cfg()
    c["sources"] = ["blockbeats_chart"]
    txt, _kb = R.panel()
    assert "blockbeats_chart" in txt
    assert r"\_" not in txt, "反引号里不该出现转义反斜杠"


def test_selfcheck_shows_underscore_names_verbatim(monkeypatch):
    import asyncio
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(R, "_client", None)
    txt = asyncio.run(R.selfcheck())
    assert r"\_" not in txt


def test_selfcheck_tells_a_private_chat_target_apart_from_a_group(monkeypatch):
    """真机踩到：他在**私聊**里发了 /relay here，目标成了他自己的 user id。

    Telegram 的 id 有符号约定：正数是用户，负数才是群。原来的提示一律说
    "把搬运号拉进那个群"——可根本没有群，指错方向比不提示更浪费时间。
    """
    import asyncio

    class _C:
        async def get_me(self):
            return type("M", (), {"first_name": "大", "username": None,
                                  "id": 8764435268})()

        async def get_entity(self, x):
            raise RuntimeError("Could not find the input entity for PeerUser")

    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(R, "_client", _C())
    c = R.cfg()
    c["target"] = 7774574457          # 正数 = 用户
    txt = asyncio.run(R.selfcheck())
    assert "私聊" in txt
    assert "去你要收消息的那个群里" in txt
    assert "拉进那个群" not in txt, "没有群的时候不能让他去拉群"


def test_hint_knows_he_is_already_standing_in_the_group(monkeypatch):
    """真机第二次踩到：他人已经在群里了，提示还在说"去你要收消息的那个群里"——
    读起来像还要再去别的地方。知道他在哪儿，下一步就能压成一句直接照做的话。

    顺带点破 `/relay check` 不改目标——他就是把 check 当成 here 用了。
    """
    import asyncio

    class _C:
        async def get_me(self):
            return type("M", (), {"first_name": "大", "username": None, "id": 1})()

        async def get_entity(self, x):
            raise RuntimeError("nope")

    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(R, "_client", _C())
    R.cfg()["target"] = 7774574457
    txt = asyncio.run(R.selfcheck(here=-1003950673952, in_group=True))
    assert "你现在就在群里" in txt
    assert "只自检、不会改目标" in txt
    assert "去你要收消息的那个群里" not in txt, "他就在群里，别再让他去别处"


def test_check_passes_the_chat_context_in():
    """上下文不传进去的话，上面那条改进等于没有。"""
    import inspect
    assert "in_group" in inspect.signature(R.selfcheck).parameters
    src = inspect.getsource(R.relay_cmd)
    assert "effective_chat" in src and "supergroup" in src


def test_selfcheck_still_says_pull_me_in_for_a_real_group(monkeypatch):
    import asyncio

    class _C:
        async def get_me(self):
            return type("M", (), {"first_name": "大", "username": None, "id": 1})()

        async def get_entity(self, x):
            raise RuntimeError("nope")

    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(R, "_client", _C())
    c = R.cfg()
    c["target"] = -1003950673952      # 负数 = 群
    txt = asyncio.run(R.selfcheck())
    assert "拉进那个群" in txt and "私聊" not in txt


def test_panel_flags_a_private_chat_target(monkeypatch):
    """别等他跑自检才发现——面板上就该看得见。"""
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    c = R.cfg()
    c["target"] = 7774574457
    txt, _kb = R.panel()
    assert "这是私聊不是群" in txt


def test_default_is_off():
    """默认必须是关的——搬运会往群里发东西，不能装上就开始刷。"""
    assert R.cfg()["on"] is False


def test_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("relay"' in src
    assert "relay.start" in src or "_relay.start" in src, "要在 post_init 里启动"


def test_admin_only():
    import inspect
    assert "is_admin" in inspect.getsource(R.relay_cmd)
    assert "is_admin" in inspect.getsource(R.on_button)


def test_buttons_are_dispatched():
    import inspect
    from handlers import menu
    assert 'd.startswith("rl:")' in inspect.getsource(menu._dispatch)


def test_command_is_categorised_in_the_panel():
    from handlers import cmdpanel
    assert cmdpanel.MODULE_CN.get("handlers.relay")


# ── 干活的账号可以不是他本人 ─────────────────────────────────
def test_session_is_account_agnostic():
    """TG_SESSION 只是"某个账号的凭证"，代码不该关心是谁的。
    2026-08-25 他问能不能用小号——能，而且主号不暴露在自动化风险里更好。"""
    import inspect
    src = inspect.getsource(R.start)
    assert "TG_SESSION" in src
    # 不能出现任何"必须是管理员本人"之类的绑定
    assert "is_admin" not in src, "读频道的账号和配置的管理员是两回事，别绑死"


def test_selfcheck_covers_the_cross_account_failure_mode():
    """配置的是他、干活的是小号——"我设好了"不代表"那个号进得去"。
    转发失败只写日志，他那边看到的是"没反应"。"""
    import inspect
    src = inspect.getsource(R.selfcheck)
    assert "get_me" in src, "要报清楚干活的是哪个号"
    assert "get_entity" in src, "要实测目标群和每个源频道够不够得着"
    assert "noforwards" in src, "频道开了禁止转发的话，订阅了也搬不出来"


def test_selfcheck_is_reachable():
    import inspect
    src = inspect.getsource(R.relay_cmd)
    assert "selfcheck" in src
    assert "selfcheck" in inspect.getsource(R.on_button)
    _txt, kb = R.panel()
    # 未配置时面板只有返回键；配置后才有自检按钮，这里只验命令路径
    assert "check" in src


def test_selfcheck_says_so_when_client_is_down(monkeypatch):
    """session 失效是最常见的故障，要直接说"重新换一串"，别只说失败。"""
    import asyncio
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(R, "_client", None)
    txt = asyncio.run(R.selfcheck())
    assert "没连上" in txt and "tools_tg_login" in txt


def test_panel_warns_about_the_cross_account_trap(monkeypatch):
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION"):
        monkeypatch.setenv(k, "x")
    txt, kb = R.panel()
    assert "干活的是上面那个号" in txt
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "rl:check" in cbs


def test_forwarding_keeps_attribution():
    """搬别人的东西，出处不能弄丢。原生转发自带「转发自 XXX」的头。"""
    import inspect
    src = inspect.getsource(R.handle)
    assert "forward_messages" in src, "要用原生转发，不是复制内容重发"
