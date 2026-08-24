"""极端拉升告警 /pump3 —— 15m 暴拉 **且** 多日累计已涨一大截。

他的需求：「15m 放量巨幅拉升 40%。3日累计涨幅达到 50%」。

**做成订阅不做成命令，是量出来的**：探针实测过去 24 小时、112 个成交额≥500万
的永续里，符合这个组合的是 0 个。稀有的东西做成命令 = 点开永远空白。

护栏：
  1. 两个条件是 **AND**，差一个都不推；
  2. **先闸便宜的**（15m 白拿），只对幸存者去拉日线和量——反过来做是白烧接口；
  3. 量比只用**已收盘**的 K 线（v1.33.1 的教训）；
  4. 只订了 pump3 的人，那个 60 秒任务也必须跑到（v1.35.0 踩过的同一个坑）；
  5. **必须有自检**：一个月响几次的告警，"没响"和"坏了"看起来一模一样。
"""
import time

import pytest

import storage
from handlers import pump3 as P


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(storage, "save_data", lambda *a, **k: None)
    monkeypatch.setattr(P, "save_data", lambda *a, **k: None)
    storage.data["pump3"] = {}
    storage.data["pump3_alerted"] = {}
    yield
    storage.data["pump3"] = {}
    storage.data["pump3_alerted"] = {}


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class Ctx:
    def __init__(self):
        self.bot = Bot()


def run(c):
    import asyncio
    return asyncio.run(c)


def _arm(monkeypatch, ctx_map, cfg=None):
    """打桩 _context_for：{sym: (3日涨幅, 量比)}"""
    async def _c(client, sym):
        return ctx_map.get(sym)
    monkeypatch.setattr(P, "_context_for", _c)
    storage.data["pump3"] = {"77": cfg or P._defaults()}


# ── AND：差一个都不推 ────────────────────────────────────────
def test_fires_when_both_conditions_hold(monkeypatch):
    _arm(monkeypatch, {"TUT": (58.0, 3.7)})
    c = Ctx()
    run(P.check(c, {"TUT": (42.0, 0.066)}))
    assert c.bot.sent, "15m 42% + 3日 58% + 量比 3.7 应该推"
    assert "TUT" in c.bot.sent[0][1]


def test_no_alert_when_the_three_day_leg_is_missing(monkeypatch):
    """真机那天唯一 15m 涨过 40% 的是 VELVET，3日 -79%——那是超跌反弹，
    不是他要的形态。这条盯着别把它放进来。"""
    _arm(monkeypatch, {"VELVET": (-79.2, 3.2)})
    c = Ctx()
    run(P.check(c, {"VELVET": (54.9, 0.01)}))
    assert not c.bot.sent


def test_no_alert_when_the_15m_leg_is_missing(monkeypatch):
    _arm(monkeypatch, {"PORTAL": (60.0, 5.0)})
    c = Ctx()
    run(P.check(c, {"PORTAL": (9.0, 0.013)}))
    assert not c.bot.sent


def test_no_alert_without_volume(monkeypatch):
    """他说的是"放量"拉升。没量的拉升是另一回事。"""
    _arm(monkeypatch, {"X": (60.0, 1.1)})
    c = Ctx()
    run(P.check(c, {"X": (45.0, 1.0)}))
    assert not c.bot.sent


def test_volume_gate_can_be_switched_off(monkeypatch):
    cfg = P._defaults()
    cfg["vol"] = 0
    _arm(monkeypatch, {"X": (60.0, 1.1)}, cfg)
    c = Ctx()
    run(P.check(c, {"X": (45.0, 1.0)}))
    assert c.bot.sent


def test_thresholds_are_per_subscriber(monkeypatch):
    async def _c(client, sym):
        return (35.0, 3.0)
    monkeypatch.setattr(P, "_context_for", _c)
    loose = dict(P._defaults(), m15=20, d3=30)
    storage.data["pump3"] = {"1": P._defaults(), "2": loose}
    c = Ctx()
    run(P.check(c, {"X": (25.0, 1.0)}))
    got = {cid for cid, _t in c.bot.sent}
    assert got == {2}, "只有放宽门槛的那个订阅者该收到"


# ── 先闸便宜的，别为不会命中的币打接口 ────────────────────────
def test_expensive_lookup_only_runs_for_survivors(monkeypatch):
    calls = []

    async def _c(client, sym):
        calls.append(sym)
        return (60.0, 3.0)
    monkeypatch.setattr(P, "_context_for", _c)
    storage.data["pump3"] = {"77": P._defaults()}
    run(P.check(Ctx(), {"A": (45.0, 1), "B": (2.0, 1), "C": (-3.0, 1)}))
    assert calls == ["A"], "只有过了 15m 闸的才该去拉日线和量"


def test_nothing_fetched_when_no_candidate(monkeypatch):
    calls = []

    async def _c(client, sym):
        calls.append(sym)
        return (60.0, 3.0)
    monkeypatch.setattr(P, "_context_for", _c)
    storage.data["pump3"] = {"77": P._defaults()}
    run(P.check(Ctx(), {"A": (5.0, 1), "B": (2.0, 1)}))
    assert calls == [], "一个都过不了闸时，一个接口都不该打"


def test_no_subscribers_means_no_work(monkeypatch):
    calls = []

    async def _c(client, sym):
        calls.append(sym)
        return (60.0, 3.0)
    monkeypatch.setattr(P, "_context_for", _c)
    run(P.check(Ctx(), {"A": (99.0, 1)}))
    assert calls == []


# ── 冷却 ────────────────────────────────────────────────────
def test_same_coin_is_not_reported_twice(monkeypatch):
    _arm(monkeypatch, {"TUT": (58.0, 3.7)})
    c = Ctx()
    run(P.check(c, {"TUT": (42.0, 0.066)}))
    run(P.check(c, {"TUT": (43.0, 0.067)}))
    assert len(c.bot.sent) == 1, "6 小时内同一个币重复推没有信息量"


def test_cooldown_expires(monkeypatch):
    _arm(monkeypatch, {"TUT": (58.0, 3.7)})
    c = Ctx()
    run(P.check(c, {"TUT": (42.0, 0.066)}))
    storage.data["pump3_alerted"]["77"]["TUT"] = time.time() - P.COOLDOWN - 10
    run(P.check(c, {"TUT": (42.0, 0.066)}))
    assert len(c.bot.sent) == 2


# ── 位置标签：同一根暴涨 K 线，位置不同意思完全不同 ───────────
@pytest.mark.parametrize("d3,tag", [
    (-79.2, "超跌反弹"), (-40, "超跌反弹"),
    (-5, "横盘启动"), (10, "横盘启动"),
    (20, "顺势加速"), (49, "顺势加速"),
    (50, "末端逼空"), (120, "末端逼空"),
])
def test_position_tag(d3, tag):
    assert P.position_tag(d3)[0] == tag


def test_alert_shows_both_numbers_and_the_tag(monkeypatch):
    """只甩一个百分比读不出该不该碰。"""
    _arm(monkeypatch, {"TUT": (58.0, 3.7)})
    c = Ctx()
    run(P.check(c, {"TUT": (42.0, 0.066)}))
    msg = c.bot.sent[0][1]
    assert "15m +42.0%" in msg
    assert "3日累计 +58.0%" in msg
    assert "量比 3.7×" in msg
    assert "末端逼空" in msg
    assert "不构成投资建议" in msg
    assert "你的门槛" in msg, "要写清是按什么门槛报的"


# ── 后台任务必须跑到只订了 pump3 的人 ────────────────────────
def test_the_sixty_second_job_runs_for_pump3_only_subscribers():
    """只看 pump_watch 的话，只订了 pump3 的人这个任务一次都不会跑，
    告警永远不触发而且日志干净——v1.35.0 踩过的同一个坑。"""
    import inspect
    from handlers import pumpalert as PA
    src = inspect.getsource(PA.scan_pump)
    assert 'data.get("pump3")' in src
    assert "if not watch and not p3" in src
    assert "pump3.check" in src


def test_it_reuses_the_existing_market_fetch():
    """15m 涨幅是白拿的，别为它再打一轮全市场行情。"""
    import inspect
    from handlers import pumpalert as PA
    src = inspect.getsource(PA.scan_pump)
    assert src.count("_fetch_bybit_perps") == 1


# ── 自检：稀有告警必须能自证还活着 ───────────────────────────
def test_selftest_exists_and_is_wired():
    """一个月响几次的告警，"没响"和"坏了"看起来一模一样。"""
    import inspect
    assert inspect.iscoroutinefunction(P.selftest)
    src = inspect.getsource(P.on_button)
    assert "selftest" in src
    _txt, kb = P.panel(1)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "p3:test" in cbs


def test_panel_says_it_is_meant_to_be_quiet():
    """不说的话，安静会被当成坏了。"""
    txt, _kb = P.panel(1)
    assert "稀有" in txt and "安静" in txt


def test_panel_shows_current_thresholds():
    storage.data["pump3"] = {"1": dict(P._defaults(), m15=30, d3=80, vol=3)}
    txt, _kb = P.panel(1)
    assert "30%" in txt and "80%" in txt and "3×" in txt
    assert "已开启" in txt


def test_panel_reflects_off_state():
    txt, _kb = P.panel(999)
    assert "未开启" in txt


# ── 入口 ────────────────────────────────────────────────────
def test_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("pump3"' in src and 'BotCommand("pump3"' in src


def test_button_is_two_taps_from_home():
    """/menu → 🔔 提醒与订阅 → 🔔 价格/条件提醒 里有入口。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "cat_alert":')[1].split("elif d ==")[0]
    assert "p3:panel" in seg
    assert 'd.startswith("p3:")' in src


def test_command_is_categorised_in_the_panel():
    from handlers import cmdpanel
    assert cmdpanel.MODULE_CN.get("handlers.pump3")


def test_off_switch_works():
    import types
    storage.data["pump3"] = {"5": P._defaults()}

    class M:
        chat = types.SimpleNamespace(id=5)

        async def reply_text(self, *a, **k):
            return None
    upd = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=5),
                                message=M())
    ctx = types.SimpleNamespace(args=["off"])
    run(P.pump3_cmd(upd, ctx))
    assert "5" not in storage.data["pump3"]
