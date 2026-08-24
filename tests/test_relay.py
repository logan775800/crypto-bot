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


def test_forwarding_keeps_attribution():
    """搬别人的东西，出处不能弄丢。原生转发自带「转发自 XXX」的头。"""
    import inspect
    src = inspect.getsource(R.handle)
    assert "forward_messages" in src, "要用原生转发，不是复制内容重发"
