"""收尾：菜单引导式入口 / 扫描新否决 / 虚拟盘止盈损。
（计划vs成交、密钥闸门两组随实盘在 v1.70.0 删除。）"""
import asyncio

from handlers import menu, scan, vtrade


# ── 菜单：新功能必须有按钮入口 ───────────────────────────────────
def test_every_ask_entry_maps_to_a_real_handler():
    """引导式按钮指向的命令必须真的存在——按钮点了报错比没按钮更糟。"""
    import importlib
    table = {
        "net": ("handlers.econ", "net_cmd"),
        "sym": ("handlers.symbols", "sym_cmd"),
        "backtest": ("handlers.backtest", "backtest_cmd"),
        "datacheck": ("handlers.datameta", "datacheck"),
        "achart": ("handlers.annotchart", "achart"),
        "events": ("handlers.events", "events_cmd"),
    }
    for key, (_tip, cmd) in menu.ASK.items():
        assert cmd in table, f"{key} 没有对应的命令映射"
        mod, fn = table[cmd]
        assert hasattr(importlib.import_module(mod), fn), f"{mod}.{fn} 不存在"


def test_categories_have_back_button_targets():
    for _cat, (text, rows) in menu.CATS.items():
        assert text and rows
        for row in rows:
            for btn in row:
                assert btn.callback_data


def test_new_commands_are_reachable_from_menu():
    """P1~P3 新增的功能不能只能靠打命令。"""
    blob = "".join(str(b.callback_data) for _t, rows in menu.CATS.values()
                   for r in rows for b in r)
    for must in ("do:scan", "ask:net", "ask:backtest",
                 "do:weekly", "ask:events", "ask:sym"):
        assert must in blob, f"{must} 在菜单里找不到入口"


# ── 扫描器新增否决 ───────────────────────────────────────────────
def test_atr_too_small_vetoed():
    _t, v = scan.overall(90, 90, 10, 90, atr_pct=0.1, net_rr=3.0)
    assert "波动太小" in v


def test_atr_too_large_vetoed():
    _t, v = scan.overall(90, 90, 10, 90, atr_pct=20.0, net_rr=3.0)
    assert "波动过大" in v


def test_low_net_rr_vetoed():
    """前四维看着都行、净盈亏比不够——低流动性小币的典型现形方式。"""
    _t, v = scan.overall(90, 90, 10, 90, atr_pct=2.0, net_rr=1.2)
    assert "净盈亏比仅" in v


def test_btc_conflict_vetoed():
    _t, v = scan.overall(90, 90, 10, 90, atr_pct=2.0, net_rr=3.0, btc_conflict=True)
    assert "与BTC方向冲突" in v


def test_all_filters_pass_keeps_high_score():
    t, v = scan.overall(85, 80, 20, 80, atr_pct=2.0, net_rr=3.0, direction="多头")
    assert t >= 70 and "✅" in v


def test_missing_optional_metrics_do_not_veto():
    """ATR/净RR 取不到时不该误杀——缺数据不是坏数据。"""
    _t, v = scan.overall(85, 80, 20, 80, atr_pct=None, net_rr=None, direction="多头")
    assert "✅" in v


# ── 「有流动性」≠「有机会」──────────────────────────────────────
def test_no_direction_never_gets_a_green_light():
    """综合分把「能不能做」（流动性/执行）和「该不该做」（趋势/拥挤）加权
    平均了，于是盘口厚但周期打架的币能排到前面。可没方向 = 那里没有机会，
    只有流动性。实测 KORU 趋势 53(分歧) 排到第二并标 ✅，就是这么来的。"""
    total, verdict = scan.overall(53, 90, 2, 100, atr_pct=2.0, net_rr=3.0,
                                  direction="分歧")
    assert "✅" not in verdict and "没有方向" in verdict
    assert total <= 55


def test_extreme_funding_alone_vetoes():
    """费率项封顶 60、OI 项封顶 40 —— 单靠费率永远够不到 80 的拥挤否决线，
    最该拦的「极端费率」反而拦不住。实测 KAITO -0.445%/期 就漏过去了。"""
    _t, v = scan.overall(90, 90, 66, 95, atr_pct=2.0, net_rr=3.0,
                         direction="空头(周期不一致)", funding=-0.00445)
    assert "费率极端" in v


def test_going_with_the_crowded_side_is_flagged():
    """费率符号说明哪边挤。顺势方向恰好是挤满人的那一边，该拦。"""
    _t, v = scan.overall(90, 90, 40, 95, atr_pct=2.0, net_rr=3.0,
                         direction="空头", funding=-0.0008)
    assert "拥挤方" in v


def test_against_the_crowd_is_not_flagged():
    """做多而空头拥挤——那是被挤的另一边，不该拦。"""
    _t, v = scan.overall(90, 90, 40, 95, atr_pct=2.0, net_rr=3.0,
                         direction="多头", funding=-0.0008)
    assert "拥挤方" not in v


def test_normal_funding_is_not_crowding():
    """+0.005%/期 是常态噪音。不设阈值的话几乎每个币都会被判成拥挤方，
    这条否决当场作废。"""
    assert scan.crowded_side(0.00005) is None
    assert scan.crowded_side(-0.00048) is None
    assert scan.crowded_side(0.0008) == "多头"
    assert scan.crowded_side(-0.00445) == "空头"


# ── 虚拟盘止盈止损 ───────────────────────────────────────────────
def _pos(side="long", entry=100.0, sl=None, tp=None):
    p = {"side": side, "entry": entry, "margin": 100, "lev": 10, "qty": 10}
    if sl:
        p["sl"] = sl
    if tp:
        p["tp"] = tp
    return p


def test_long_stop_triggers_below():
    assert vtrade._check_tpsl(_pos(sl=95), 94) == ("止损", 95)


def test_long_tp_triggers_above():
    assert vtrade._check_tpsl(_pos(tp=110), 111) == ("止盈", 110)


def test_short_directions_mirrored():
    assert vtrade._check_tpsl(_pos(side="short", sl=105), 106)[0] == "止损"
    assert vtrade._check_tpsl(_pos(side="short", tp=90), 89)[0] == "止盈"


def test_no_trigger_inside_range():
    assert vtrade._check_tpsl(_pos(sl=95, tp=110), 102) is None


def test_stop_checked_before_tp():
    """两边同时满足时按止损算——和回测口径一致，取最坏。"""
    assert vtrade._check_tpsl(_pos(sl=95, tp=110), 94)[0] == "止损"


def test_fill_price_is_the_order_price_not_market():
    """条件单在触发价成交，用当前价会让模拟盘系统性优于实盘。"""
    _kind, px = vtrade._check_tpsl(_pos(sl=95), 90)
    assert px == 95
