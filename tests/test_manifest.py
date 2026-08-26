"""数据清单硬约束 —— 「看起来完整、实际有字段是猜的」是这个 bot 最危险的失败模式。

这些用例锁死三件事：没调过的工具算没数据、缺维度时相关结论必须被拦下、
拦下之后要给模型可执行的重写指令。全部是纯函数，不联网。
"""
import pytest

from handlers.manifest import Manifest


def _mf(*calls):
    """calls: (工具名, 结果文本) —— 结果里带 ⚠️/暂不可用 就算降级。"""
    m = Manifest()
    for name, res in calls:
        m.record(name, {}, res)
    return m


# ── 记账 ─────────────────────────────────────────────────────────
def test_degraded_result_counts_as_missing():
    """工具调了但返回的是「⚠️ 暂不可用」，等于没这份数据。"""
    m = _mf(("get_orderbook", "⚠️ BANKUSDT: Bybit 未返回订单簿。"))
    assert "book" in m.unavailable
    assert not m.any_success


def test_good_result_counts_as_available():
    m = _mf(("get_orderbook", "【BANKUSDT 订单簿 前200档】买一 0.081｜卖一 0.0811"))
    assert "book" in m.available
    assert m.any_success


def test_kline_intervals_tracked_per_interval():
    m = Manifest()
    m.record("get_klines", {"interval": "4h"}, "【X 4h】EMA20 ...")
    m.record("get_klines", {"interval": "15m"}, "⚠️ X 15m: Bybit 返回空K线")
    assert m.ivs == {"4h": True, "15m": False}


def test_same_interval_succeeds_once_counts_as_available():
    """同一周期重试成功过一次就算有——别因为第一次失败就永久判死。"""
    m = Manifest()
    m.record("get_klines", {"interval": "1h"}, "⚠️ 返回空K线")
    m.record("get_klines", {"interval": "1h"}, "【X 1h】EMA20 ...")
    assert m.ivs["1h"] is True


# ── 违规校验（核心）───────────────────────────────────────────────
def test_talking_about_orderbook_without_calling_it_is_a_violation():
    """从没调过 get_orderbook 却说「卖墙承接」——这正是最危险的编造。"""
    m = _mf(("get_klines", "【X 15m】EMA20 ..."))
    bad = m.violations("上方 0.0850 有明显卖墙，建议在此减仓")
    assert bad and any("订单簿" in b for b in bad)


def test_talking_about_oi_without_data_is_a_violation():
    m = _mf(("get_klines", "【X 15m】..."))
    assert m.violations("价涨OI涨，新多进场，趋势延续")


def test_no_violation_when_dimension_was_actually_fetched():
    m = _mf(("get_orderbook", "【X 订单簿】买一 1｜卖一 2"),
            ("get_oi_history", "【X OI历史】当前OI 100 X"))
    assert m.violations("卖墙在上方，OI 同步增加，新多进场") == []


def test_missing_interval_position_is_a_violation():
    m = Manifest()
    m.record("get_klines", {"interval": "4h"}, "【X 4h】ok")
    m.record("get_klines", {"interval": "15m"}, "⚠️ 空K线")
    assert m.violations("15m 关键位在 0.0812 附近")
    assert m.violations("4h 关键位在 0.0812 附近") == []


def test_asof_claim_without_any_success_is_a_violation():
    """一次都没取到数还写「截至 17:55」——伪造实时性，必须拦。"""
    m = _mf(("get_klines", "⚠️ 返回空K线"))
    assert any("实时性" in b for b in m.violations("截至 17:55，价格在 0.081"))


def test_empty_text_no_violation():
    assert _mf(("get_klines", "ok")).violations("") == []


# ── 给模型的指令 ─────────────────────────────────────────────────
def test_ledger_lists_bans_for_missing_dimensions():
    m = _mf(("get_klines", "【X 15m】ok"))
    led = m.ledger()
    assert "订单簿" in led and "OI" in led
    assert "没调过的工具" in led


def test_ledger_with_no_calls_forbids_everything():
    """一个工具都没调时，不许给任何价位——这条比什么都重要。"""
    led = Manifest().ledger()
    assert "一个数据工具都没调" in led and "不得给出任何" in led


def test_fix_prompt_enumerates_violations():
    m = _mf(("get_klines", "ok"))
    bad = m.violations("有卖墙")
    fix = m.fix_prompt(bad)
    assert "1." in fix and "重写" in fix
    assert "宁可短" in fix        # 别为了凑完整而编造


def test_header_separates_layers_and_lists_missing():
    """header 改成分层显示（市场/账户/缺失）——「N/M 项可用」这个总分
    把「结论没地基」和「仓位不能按真实权益算」混成了一个数。"""
    m = _mf(("get_klines", "ok"), ("get_orderbook", "⚠️ 未返回订单簿"))
    h = m.header()
    assert "`市场数据`" in h and "K线" in h, "K线成功了就该显示，别管有没有带周期名"
    assert "缺失维度" in h and "订单簿" in h


def test_header_empty_when_no_tool_calls():
    assert Manifest().header() == ""


# ── 缺失维度别罗列成"系统全挂了"（2026-08-26 真机）────────────
# 群友问「BTR 有多少空头被清算」，回答开头是
# 「本次缺少 OI、订单簿、逐笔成交、清算、资金费率、市场联动和真实账户数据」。
# 订单簿跟这个问题毫无关系——但列出来之后整条消息看起来就是系统全挂了，
# 他因此问「是不是 token 不够，怎么啥都不会说了」。
#
# 头部那行本来就只列"调过但失败"的（_group 的写法是对的）；
# 出问题的是**模型**：它把 ledger 里那张完整禁令表整个背进了正文。

def test_fix_prompt_forbids_listing_everything():
    from handlers.manifest import Manifest
    p = Manifest().fix_prompt(["你提到了「清算」，但本轮没有清算数据。"])
    assert "只点名" in p and "相关" in p
    assert "不要罗列所有没取到的维度" in p


def test_fix_prompt_pushes_the_model_to_fetch_instead_of_excusing():
    """"没去调"和"取不到"是两回事。前者应该现在去调，不是写一句"缺"就完事。"""
    from handlers.manifest import Manifest
    p = Manifest().fix_prompt(["x"])
    assert "现在就去调" in p


def test_header_only_lists_dimensions_that_were_actually_tried():
    """没调用 ≠ 缺失。问一个窄问题不该在头部列出一堆无关维度。"""
    import inspect
    from handlers.manifest import Manifest
    src = inspect.getsource(Manifest._group)
    assert "self._called(k) and not self._got(k)" in src
