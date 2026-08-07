"""P3：回测口径 / 风控参数生效 / 周报行为画像。

回测部分的测试重点不是"策略赚不赚"，而是**口径有没有作弊**——
不许前视、同根止损止盈按最坏算、成本照扣。口径错了，再漂亮的曲线都是假的。
"""
import pytest

from handlers import backtest as bt
from handlers import riskprofile as rp
from handlers import weekly as wk


# ── 回测口径 ─────────────────────────────────────────────────────
def _bars(closes, spread=0.0):
    """(ts,o,h,l,c)。默认 o=c，h/l 按 spread 撑开。"""
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        hi = max(o, c) * (1 + spread)
        lo = min(o, c) * (1 - spread)
        out.append((i * 3600_000, o, hi, lo, c))
    return out


def test_entry_uses_next_bar_open_not_signal_close():
    """用信号那根的收盘价进场是回测最常见的作弊——实盘成交不了。"""
    closes = [100 + i * 0.5 for i in range(260)]
    bars = _bars(closes, spread=0.01)
    trades = bt.simulate(bars, "trend")
    for t in trades:
        i = t["i"]
        assert t["entry"] == pytest.approx(bars[i + 1][1])     # 下一根的开盘


def test_stop_wins_when_both_hit_in_same_bar():
    """同一根里止损止盈都碰到：不知先后，必须按最坏算。
    假设有利的先到，会把每个策略都美化成圣杯。

    确定性构造：先用极窄的 K 线把 ATR 压小（止损止盈都很近），
    触发信号后紧跟一根巨幅 K 线，必然同时穿过两边。
    """
    bars = []
    # 210 根几乎不动的窄K线 → ATR 很小
    for i in range(210):
        px = 100 + (0.01 if i % 2 else 0)
        bars.append((i * 3600_000, px, px + 0.01, px - 0.01, px))
    # 20 根稳步上行，制造 EMA20 上穿 EMA50
    for i in range(210, 230):
        px = 100 + (i - 209) * 0.05
        bars.append((i * 3600_000, px - 0.02, px + 0.02, px - 0.04, px))
    # 一根上下各穿透的巨幅K线
    last = bars[-1][4]
    bars.append((230 * 3600_000, last, last * 2, last * 0.5, last))
    bars.append((231 * 3600_000, last, last * 2, last * 0.5, last))

    trades = bt.simulate(bars, "trend")
    assert trades, "构造的数据应当触发至少一次信号"
    assert all(t["gross_r"] == -1.0 for t in trades), "同根双触必须判为止损"


def test_cost_is_deducted_in_r_terms():
    """成本换算成 R：止损越窄，同样的成本吃掉的 R 越多。"""
    closes = [100 + i * 0.3 for i in range(260)]
    bars = _bars(closes, spread=0.005)
    t_cheap = bt.simulate(bars, "trend", cost_pct=0.0)
    t_pricey = bt.simulate(bars, "trend", cost_pct=0.5)
    if t_cheap and t_pricey:
        assert t_pricey[0]["net_r"] < t_cheap[0]["net_r"]
        assert t_cheap[0]["cost_r"] == 0


def test_no_overlapping_trades():
    """上一单没结束不重复进场——否则同一段行情被重复计分。"""
    closes = [100 + (i % 20) for i in range(400)]
    bars = _bars(closes, spread=0.01)
    trades = bt.simulate(bars, "trend")
    for a, b in zip(trades, trades[1:]):
        assert b["i"] > a["i"]


def test_too_few_bars_refuses_rather_than_guessing():
    assert bt.signals(_bars([100] * 50), "trend") == []


def test_unknown_rule_rejected():
    import asyncio
    s, n, err = asyncio.run(bt.run("BTC", "1h", "nonsense"))
    assert s is None and "规则只能是" in err


def test_stats_report_gross_and_net_separately():
    trades = [{"gross_r": 2.0, "net_r": 1.8, "cost_r": 0.2, "bars": 5, "stop_pct": 1.0},
              {"gross_r": -1.0, "net_r": -1.2, "cost_r": 0.2, "bars": 3, "stop_pct": 1.0}]
    s = bt.stats(trades)
    assert s["gross_exp"] == pytest.approx(0.5)
    assert s["net_exp"] == pytest.approx(0.3)
    assert s["net_exp"] < s["gross_exp"]


def test_render_warns_on_small_sample():
    s = bt.stats([{"gross_r": 1.0, "net_r": 0.9, "cost_r": 0.1,
                   "bars": 2, "stop_pct": 1.0}] * 5)
    txt = bt.render(s, "BTC", "1h", "trend", 300, 2.0, 0.11)
    assert "样本" in txt and "不稳定" in txt


def test_render_states_no_lookahead():
    s = bt.stats([{"gross_r": 1.0, "net_r": 0.9, "cost_r": 0.1,
                   "bars": 2, "stop_pct": 1.0}] * 30)
    txt = bt.render(s, "BTC", "1h", "trend", 900, 2.0, 0.11)
    assert "不许前视" in txt and "按止损算" in txt


# ── 风控参数 ─────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_profile():
    from storage import data
    data["risk_profile"] = {}
    yield
    data["risk_profile"] = {}


def test_defaults_when_unset():
    assert rp.profile(1)["risk_pct"] == rp.DEFAULTS["risk_pct"]


def test_set_and_read_back():
    ok, _ = rp.set_param(1, "risk_pct", "0.3")
    assert ok and rp.profile(1)["risk_pct"] == 0.3


def test_reject_unknown_param():
    ok, msg = rp.set_param(1, "nonsense", "1")
    assert not ok and "没有这个参数" in msg


def test_reject_factor_above_one():
    """降险系数是「降到原来的多少」，>1 就成了加险。"""
    ok, msg = rp.set_param(1, "streak_factor", "1.5")
    assert not ok and "≤1" in msg


def test_streak_cuts_risk_automatically():
    """连亏后加倍捞回来是账户归零的标准路径——这条不需要用户判断力就生效。"""
    eff, why = rp.effective_risk(1, streak=3)
    assert eff == pytest.approx(rp.DEFAULTS["risk_pct"] * rp.DEFAULTS["streak_factor"])
    assert "连亏" in why and "归零" in why


def test_no_streak_no_cut():
    eff, why = rp.effective_risk(1, streak=1)
    assert eff == rp.DEFAULTS["risk_pct"] and why == ""


def test_max_risk_caps_the_base():
    rp.set_param(1, "risk_pct", "5")
    rp.set_param(1, "max_risk_pct", "1")
    eff, _ = rp.effective_risk(1, 0)
    assert eff == 1.0


def test_limits_block_too_many_positions():
    poss = [{"side": "Buy", "positionValue": "100"}] * 5
    hits = rp.check_limits(1, None, poss, 10_000)
    assert any(hard for hard, _ in hits)


def test_limits_block_same_side_overexposure():
    poss = [{"side": "Buy", "positionValue": "19000"}]
    plan = {"side": "long", "notional": 5000, "risk_pct": 0.5, "lev": 5}
    hits = rp.check_limits(1, plan, poss, 10_000)
    assert any("同向名义" in msg for _h, msg in hits)


def test_leverage_over_limit_is_warning_not_block():
    """护栏不是牢笼——全部硬拦会让人干脆关掉它。"""
    plan = {"side": "long", "notional": 100, "risk_pct": 0.5, "lev": 50}
    hits = rp.check_limits(1, plan, [], 10_000)
    lev_hits = [(h, m) for h, m in hits if "杠杆" in m]
    assert lev_hits and not lev_hits[0][0]


# ── 周报行为画像 ─────────────────────────────────────────────────
def _t(pnl, dur=3600, lev=10, value=1000, sym="BANKUSDT", side="long"):
    return {"pnl": pnl, "dur": dur, "lev": lev, "value": value,
            "symbol": sym, "side": side, "ts": 0}


def test_behavior_measures_controllables():
    b = wk.behavior([_t(10), _t(-5, dur=7200, lev=20)])
    assert b["avg_dur"] == pytest.approx(5400)
    assert b["avg_lev"] == pytest.approx(15)
    assert b["alt_ratio"] == 100


def test_loss_dispersion_low_when_disciplined():
    """止损守纪律的人，每笔亏损金额接近；忽大忽小=在扛单。"""
    tight = wk.behavior([_t(-10), _t(-10), _t(-11), _t(-9)])
    loose = wk.behavior([_t(-5), _t(-50), _t(-8), _t(-200)])
    assert tight["loss_cv"] < loose["loss_cv"]


def test_small_change_reads_as_flat():
    """把噪音读成趋势比不看还糟。"""
    mark, _d = wk._arrow(103, 100)
    assert mark == "→"


def test_meaningful_change_is_flagged():
    mark, _d = wk._arrow(150, 100, lower_better=True)
    assert mark == "⚠️"
    mark2, _d2 = wk._arrow(50, 100, lower_better=True)
    assert mark2 == "✅"


def test_first_week_says_baseline_only():
    txt, _cur = wk.build([_t(10)], None)
    assert "第一周" in txt and "只有基线" in txt


def test_pnl_is_placed_last_and_caveated():
    txt, _cur = wk.build([_t(10)] * 3, {"n": 3})
    assert txt.index("行为画像") < txt.index("结果")
    assert "样本 <20" in txt


def test_no_trades_is_not_an_error():
    txt, cur = wk.build([], None)
    assert cur is None and "没有已平仓交易" in txt
