"""计划的环境取消条件 —— 价格没碰失效位，但进场前提已经没了。

永续特有的两条：BTC 一破位山寨跟着走；资金费拥挤到一定程度，
进场成本本身就把这单变成负期望。纯函数部分必须锁死方向语义：
BTC 砸盘只该杀山寨**多单**，空单反而是受益的，误杀一份好计划比漏杀更贵。
"""
import pytest

from handlers import plan as pl


def _p(symbol="BANKUSDT", side="long", status="waiting", guards=None):
    return {"id": "p1", "symbol": symbol, "side": side, "status": status,
            "chat_id": 1, "guards": guards if guards is not None else {}}


# ── 方向语义 ─────────────────────────────────────────────────────
def test_btc_dump_cancels_alt_longs():
    aborts = {"BANKUSDT": ("btc", "BTC 15m -2.4%", "long")}
    why = pl.env_abort_reason(_p(side="long"), aborts)
    assert why and "大盘破位" in why


def test_btc_dump_does_not_cancel_shorts():
    """砸盘时空单是受益方——杀掉它是纯粹的误伤。"""
    aborts = {"BANKUSDT": ("btc", "BTC 15m -2.4%", "long")}
    assert pl.env_abort_reason(_p(side="short"), aborts) is None


def test_btc_pump_cancels_alt_shorts():
    aborts = {"BANKUSDT": ("btc", "BTC 15m +2.6%", "short")}
    assert pl.env_abort_reason(_p(side="short"), aborts)


def test_positive_funding_cancels_longs():
    """费率为正=多头拥挤，此时该被劝退的是多单。"""
    aborts = {"BANKUSDT": ("funding", "资金费率 +0.120%/期", "long")}
    why = pl.env_abort_reason(_p(side="long"), aborts)
    assert why and "资金费拥挤" in why


def test_negative_funding_cancels_shorts():
    aborts = {"BANKUSDT": ("funding", "资金费率 -0.150%/期", "short")}
    assert pl.env_abort_reason(_p(side="short"), aborts)


def test_other_symbol_untouched():
    aborts = {"OTHERUSDT": ("btc", "x", "long")}
    assert pl.env_abort_reason(_p(), aborts) is None


def test_no_aborts_means_no_cancel():
    assert pl.env_abort_reason(_p(), {}) is None


# ── 守卫开关 ─────────────────────────────────────────────────────
def test_guard_can_be_turned_off():
    aborts = {"BANKUSDT": ("btc", "BTC 15m -3%", "long")}
    assert pl.env_abort_reason(_p(guards={"btc_abort": False}), aborts) is None


def test_guard_defaults_to_on_when_absent():
    """老计划没有 guards 字段，也该受保护——默认开。"""
    aborts = {"BANKUSDT": ("btc", "BTC 15m -3%", "long")}
    p = _p()
    p.pop("guards")
    assert pl.env_abort_reason(p, aborts)


def test_funding_guard_independent_of_btc_guard():
    aborts = {"BANKUSDT": ("funding", "资金费率 +0.2%/期", "long")}
    assert pl.env_abort_reason(_p(guards={"btc_abort": False}), aborts)
    assert pl.env_abort_reason(_p(guards={"funding_abort": False}), aborts) is None


# ── 追价上限 ─────────────────────────────────────────────────────
def test_chase_limit_scales_with_stop_distance():
    """止损 1% 的单追 0.5% 等于送掉半个 R，所以追价上限必须跟止损距离挂钩。"""
    assert pl.MAX_CHASE_OF_STOP < 0.5
    tight = 1.0 * pl.MAX_CHASE_OF_STOP
    wide = 5.0 * pl.MAX_CHASE_OF_STOP
    assert tight < wide


def test_guards_rendered_on_card():
    p = {"id": "p1", "symbol": "BANKUSDT", "side": "long", "status": "waiting",
         "entry": [1.0, 1.1], "stop": 0.9, "tps": [{"price": 1.4}],
         "trigger": {"desc": "x"}, "invalid": {"desc": "y"},
         "guards": {"max_chase_pct": 0.25, "max_slip_pct": 0.3,
                    "btc_abort": True, "funding_abort": True}}
    txt = pl.card(p, with_meta=False)
    assert "执行守卫" in txt and "追价" in txt
    assert "BTC破位取消" in txt and "费率拥挤取消" in txt
