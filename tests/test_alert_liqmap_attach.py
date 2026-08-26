"""合约异动告警自动配清算地图 —— **真执行**，不是源码字符串检查。

2026-08-26 他问「怎么不展示清算图了」。查下来是我 v1.52.1 改坏的：
`liqmap._get` 的返回值加过两次字段（来源、覆盖天数），而这里写死了
`m, last, _inst = await liqmap._get(...)` 三个，每次都 ValueError。

**藏了好几个版本没被发现，因为配图失败是「安静跳过」的**——
对用户静默是对的（告警已送到，不能再刷一条"配图失败"），
但对我们也静默就成了这个项目最贵的那类 bug：不报错、日志干净、
只是某个东西永远不发生。

所以这里守两条：图真的发得出去；以及改返回值时按位置解包会红。
"""
import asyncio
import types

import pytest

from handlers import contract_alert as CA


class FakeBot:
    def __init__(self):
        self.photos = []

    async def send_photo(self, chat_id=None, photo=None, caption=None, **kw):
        self.photos.append(caption or "")


def _run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


@pytest.fixture
def stub_liqmap(monkeypatch):
    """打桩 liqmap，避免联网；**返回值保持和真函数一样多的字段**。"""
    from handlers import liqmap as L

    fake_map = {"longs": {lv: [0.0] * L.BUCKETS for lv, _w, _c in L.LEVS},
                "shorts": {lv: [0.0] * L.BUCKETS for lv, _w, _c in L.LEVS},
                "edges": [1.0] * L.BUCKETS, "width": 0.1,
                "added": 0, "cur_bucket": 30}

    async def _get(sym, win, force=False):
        return fake_map, 1.0, sym + "USDT", "币安", 7.0

    monkeypatch.setattr(L, "_get", _get)
    monkeypatch.setattr(L, "render", lambda *a, **k: b"png")
    return L


def test_chart_is_actually_sent(stub_liqmap):
    """**这条就是线上坏掉的那件事。** 只做字符串检查的话它永远绿。"""
    bot = FakeBot()
    _run(CA._attach_liqmap(bot, -100, [{"sym": "BTR", "change": 354.0}]))
    assert len(bot.photos) == 1, "幅度够大却没配图——多半又是解包对不上"


def test_pump_and_dump_get_different_wording(stub_liqmap):
    """砸下来看下方多单、拉上去看上方空单。文案一样等于把最该说的省掉了。"""
    up, dn = FakeBot(), FakeBot()
    _run(CA._attach_liqmap(up, -100, [{"sym": "X", "change": 60.0}]))
    _run(CA._attach_liqmap(dn, -100, [{"sym": "X", "change": -60.0}]))
    assert "上方" in up.photos[0] and "空单" in up.photos[0]
    assert "下方" in dn.photos[0] and "多单" in dn.photos[0]


def test_small_moves_get_no_chart(stub_liqmap):
    """告警是批量推的，一条消息可能十个币。只给幅度最大且够大的那个配图。"""
    bot = FakeBot()
    _run(CA._attach_liqmap(bot, -100, [{"sym": "X", "change": 5.0}]))
    assert bot.photos == []


def test_only_one_chart_per_alert(stub_liqmap):
    """十个币配十张图会把群刷爆，其余给按钮。"""
    bot = FakeBot()
    alerts = [{"sym": f"C{i}", "change": 100.0 + i} for i in range(10)]
    _run(CA._attach_liqmap(bot, -100, alerts))
    assert len(bot.photos) == 1
    assert "C9" in bot.photos[0], "该配幅度最大的那个"


def test_failure_never_breaks_the_alert(stub_liqmap, monkeypatch):
    """配图挂了不能连累告警本身——告警已经送到了。"""
    async def _boom(*a, **k):
        raise RuntimeError("接口挂了")
    monkeypatch.setattr(stub_liqmap, "_get", _boom)
    bot = FakeBot()
    _run(CA._attach_liqmap(bot, -100, [{"sym": "X", "change": 99.0}]))   # 不该抛
    assert bot.photos == []


def test_code_errors_are_reported_to_the_heartbeat(stub_liqmap, monkeypatch):
    """**取不到这个币的数据**是常态（告警是全交易所的），不该惊动人；
    **代码错**（解包对不上这种）必须报，否则又是一次"图静静地消失几个版本"。"""
    from handlers import monitor as M

    async def _bad(*a, **k):
        raise ValueError("too many values to unpack")
    monkeypatch.setattr(stub_liqmap, "_get", _bad)
    M._BEATS.pop("告警配清算图", None)
    _run(CA._attach_liqmap(FakeBot(), -100, [{"sym": "X", "change": 99.0}]))
    assert "告警配清算图" in M._BEATS, "代码错没进心跳，下次还是没人知道"
    M._BEATS.pop("告警配清算图", None)


def test_data_gaps_stay_quiet(stub_liqmap, monkeypatch):
    """这个币两家都没有持仓量历史 —— 是常态，别往心跳里塞。"""
    from handlers import monitor as M

    async def _missing(*a, **k):
        raise RuntimeError("币安和 Bybit 的永续上都取不到它的持仓量历史")
    monkeypatch.setattr(stub_liqmap, "_get", _missing)
    M._BEATS.pop("告警配清算图", None)
    _run(CA._attach_liqmap(FakeBot(), -100, [{"sym": "X", "change": 99.0}]))
    assert "告警配清算图" not in M._BEATS


def test_caller_does_not_unpack_by_fixed_arity():
    """`liqmap._get` 的返回值已经加过两次字段。按固定个数解包的写法
    每加一次就坏一次，而且坏得没有声音。"""
    import inspect
    src = inspect.getsource(CA._attach_liqmap)
    assert "m, last, _inst = await liqmap._get" not in src
