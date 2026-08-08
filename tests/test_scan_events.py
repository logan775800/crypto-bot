"""机会扫描评分 + 事件驱动预警判定。

两个模块的共同立场：
  • 扫描按**可交易性**排序而不是涨幅——涨幅第一名往往是最不该碰的；
  • 预警只报**状态切换**且必须有基线——没有上一轮就没有"变化"，
    第一次见到一个币就报警是纯噪音，会训练用户忽略提醒。
"""
import pytest

from handlers import scan
from handlers import events as ev


# ── 扫描评分 ─────────────────────────────────────────────────────
def _tf(a4, a1, a15, slope=0.0):
    return {"4h": {"align": a4, "slope": slope},
            "1h": {"align": a1, "slope": slope},
            "15m": {"align": a15, "slope": slope}}


def test_all_timeframes_agree_scores_high():
    s, d = scan.score_trend(_tf(1, 1, 1, slope=2.0))
    assert s > 70 and "多头" in d and "不一致" not in d


def test_conflicting_timeframes_score_low():
    """4h多/1h空/15m多——哪个方向进去都是猜，不是跟。"""
    s, d = scan.score_trend(_tf(1, -1, 1))
    assert s < 45 and d == "分歧"


def test_direction_needs_a_majority():
    """1 票多头 + 2 票缠绕不能叫「多头」。

    实测 ACE 24h 跌 22.6% 却被标成多头，就是因为只要 sum>0 就命名方向。
    过半才敢叫，否则叫「分歧」。
    """
    assert scan.score_trend(_tf(1, 0, 0))[1] == "分歧"
    assert scan.score_trend(_tf(-1, 0, 0))[1] == "分歧"
    assert scan.score_trend(_tf(1, 1, 0))[1] == "多头(周期不一致)"
    assert scan.score_trend(_tf(1, 1, 1))[1] == "多头"


def test_depth_uses_a_price_band_not_a_level_count():
    """按「200 档」汇总深度不可比：实测 BTC 的 200 档只覆盖 0.1%，
    SNXX 却覆盖 44%——薄币被高估、厚币被低估，方向正好反了。"""
    assert 0 < scan.DEPTH_BAND <= 1.0


def test_no_kline_data_scores_zero_not_guess():
    s, d = scan.score_trend({})
    assert s == 0 and d == "无数据"


def test_liquidity_needs_both_turnover_and_depth():
    """成交额大但盘口薄的币照样进不去——不能只看成交额。"""
    only_turnover = scan.score_liquidity(500_000_000, 0)
    both = scan.score_liquidity(500_000_000, 800_000)
    assert only_turnover < both


def test_passing_the_turnover_floor_is_not_auto_vetoed():
    """粗筛门槛和评分不能自相矛盾。

    第一版线性刻度下，刚过 2000 万门槛的币流动性只有 6 分、直接被否——
    于是扫描器对几乎所有候选都说「不建议」，等于没有输出。
    """
    at_floor = scan.score_liquidity(scan.MIN_TURNOVER, 60_000)
    assert at_floor >= 25, f"刚过门槛就只有 {at_floor:.0f} 分，评分和门槛对不上"


def test_liquidity_is_log_scaled_across_magnitudes():
    """流动性跨两个数量级，线性刻度会把中小市值全压成 0。"""
    small = scan.score_liquidity(20_000_000, 30_000)     # 刚过门槛
    mid = scan.score_liquidity(200_000_000, 300_000)
    huge = scan.score_liquidity(2_500_000_000, 11_000_000)
    assert small < mid < huge
    assert mid - small > 15 and huge - mid > 15          # 每档都拉得开


def test_real_world_ordering_matches_intuition():
    """按 2026-08-08 实测数据标定：BTC ≫ SPCX > SNXX > BICO ≈ ACE > BLUAI。"""
    s = scan.score_liquidity
    btc, spcx, snxx = s(2510e6, 11647e3), s(231e6, 749e3), s(48e6, 245e3)
    bico, bluai = s(68e6, 38e3), s(22e6, 29e3)
    assert btc > spcx > snxx > bico > bluai
    assert btc >= 95 and bluai < 30      # 两端要分得开


def test_crowding_is_a_penalty_score():
    """拥挤分越高越危险。"""
    calm = scan.score_crowding(0.00001, 1)
    hot = scan.score_crowding(0.002, 40)
    assert hot > calm and hot > 80


def test_partial_fill_halves_execution_score():
    full = scan.score_execution(0.02, 0.05, False)
    part = scan.score_execution(0.02, 0.05, True)
    assert part == pytest.approx(full / 2)


def test_illiquid_veto_overrides_great_trend():
    """流动性不及格时趋势再漂亮也不算——小币最容易在这项上把人埋了。"""
    total, verdict = scan.overall(trend=95, liq=10, crowd=10, exec_q=90)
    assert total <= 35 and "❌" in verdict and "流动性不足" in verdict


def test_high_execution_cost_also_vetoes():
    total, verdict = scan.overall(trend=90, liq=90, crowd=10, exec_q=15)
    assert "执行成本过高" in verdict


def test_extreme_crowding_vetoes():
    _t, verdict = scan.overall(trend=90, liq=90, crowd=90, exec_q=90)
    assert "拥挤度极高" in verdict


def test_good_setup_passes():
    total, verdict = scan.overall(trend=85, liq=80, crowd=20, exec_q=80)
    assert total >= 70 and "✅" in verdict


def test_render_states_it_is_not_a_rank_by_gain():
    rows = [{"symbol": "AUSDT", "price": 1.0, "chg": 5.0, "turnover": 1e8,
             "trend": 80, "direction": "多头", "liq": 70, "crowd": 20, "exec": 75,
             "total": 76, "verdict": "✅ 可交易性好", "funding": 0.0001,
             "oi_change": 5.0, "slip": 0.05, "partial": False, "missing": []}]
    txt = scan.render(rows)
    assert "可交易性" in txt and "不是按涨幅" in txt


def test_render_flags_missing_dimensions():
    rows = [{"symbol": "AUSDT", "price": 1.0, "chg": 1.0, "turnover": 1e8,
             "trend": 0, "direction": "无数据", "liq": 50, "crowd": 0, "exec": 50,
             "total": 40, "verdict": "🟡", "funding": 0, "oi_change": None,
             "slip": None, "partial": False, "missing": ["K线", "OI"]}]
    assert "缺 K线、OI" in scan.render(rows)


# ── 事件判定 ─────────────────────────────────────────────────────
def test_no_baseline_no_alert():
    """第一次见到这个币不该报警——没有上一轮就没有"变化"。"""
    assert ev.detect("XUSDT", {"oi": 1000, "funding": 0.01}, {}) == []


def test_oi_jump_detected_with_context():
    out = ev.detect("XUSDT", {"oi": 1100, "chg_15m": 3.0}, {"oi": 1000})
    assert out and out[0][0] == "oi_jump"
    ctx = " ".join(out[0][2])
    assert "新多进场" in ctx          # 价涨+OI涨 的四象限解读要带上


def test_small_oi_move_ignored():
    assert ev.detect("XUSDT", {"oi": 1020, "chg_15m": 1}, {"oi": 1000}) == []


def test_quadrant_flip_reported():
    out = ev.detect("XUSDT", {"quad": "价跌+OI跌｜多头在平仓"},
                    {"quad": "价涨+OI涨｜新多进场"})
    assert out and out[0][0] == "quad_flip"
    assert "推动这段行情的人换了" in " ".join(out[0][2])


def test_same_quadrant_not_reported():
    q = "价涨+OI涨｜新多进场"
    assert ev.detect("XUSDT", {"quad": q}, {"quad": q}) == []


def test_funding_crossing_into_crowded_zone():
    out = ev.detect("XUSDT", {"funding": 0.0006}, {"funding": 0.0002})
    assert out and out[0][0] == "funding_cross"
    assert "多头" in " ".join(out[0][2])


def test_negative_funding_crossing_flags_shorts():
    out = ev.detect("XUSDT", {"funding": -0.0006}, {"funding": -0.0002})
    assert out and "空头" in " ".join(out[0][2])


def test_funding_inside_zone_not_re_reported():
    """已经在拥挤区里波动不该反复报——反复报等于噪音。"""
    assert ev.detect("XUSDT", {"funding": 0.0008}, {"funding": 0.0007}) == []


def test_imbalance_flip_requires_both_sides_extreme():
    assert ev.detect("XUSDT", {"imb": -50}, {"imb": 50})
    assert ev.detect("XUSDT", {"imb": -10}, {"imb": 50}) == []


def test_imbalance_context_warns_orders_are_cancellable():
    out = ev.detect("XUSDT", {"imb": -50}, {"imb": 50})
    assert "可撤" in " ".join(out[0][2])


def test_render_carries_context_lines():
    txt = ev.render("BTCUSDT", "⚡ OI 跳升", ["持仓量 1 → 2", "同期价格 +3%"], price=64000)
    assert "BTC" in txt and "持仓量" in txt and "同期价格" in txt


def test_quadrant_mapping_is_exhaustive():
    for dpx in (1, -1):
        for doi in (1, -1):
            assert ev.quadrant(dpx, doi)
