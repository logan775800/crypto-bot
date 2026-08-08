"""首页精简 + 分析结果闭环。

闭环这条是这次改动里价值最高的：以前分析完是死胡同，用户要自己把币名和
价位搬到 /net、/risk、/plan 去。中间那段手工搬运正是「看完就算了」的原因。
"""
import pytest

from handlers import menu


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ── 首页精简 ─────────────────────────────────────────────────────
def test_home_is_compact():
    """20 个按钮视觉权重全一样等于没有主次。"""
    n = len(_cbs(menu.main_menu_kb()))
    assert n <= 13, f"首页按钮 {n} 个，太多了"


def test_daily_use_features_are_on_home():
    """每天要看的必须在一级：持仓、风险、机会、复盘。"""
    cbs = _cbs(menu.main_menu_kb())
    for must in ("cat_holding", "cat_risk", "cat_scan", "cat_review"):
        assert must in cbs, f"{must} 不在首页"


def test_low_frequency_moved_to_more():
    """行情查询、三个交易所专区、资讯这些低频的收进「更多」。"""
    home = _cbs(menu.main_menu_kb())
    more = _cbs(menu.more_menu_kb())
    for k in ("cat_okx", "cat_binance", "cat_bybit", "cat_news", "cat_price"):
        assert k not in home and k in more, f"{k} 应该在「更多」里"


def test_more_has_a_way_back():
    assert "menu_main" in _cbs(menu.more_menu_kb())


def test_nothing_lost_between_home_and_more():
    """精简不等于砍功能——原来的入口必须都还在。"""
    all_cbs = set(_cbs(menu.main_menu_kb())) | set(_cbs(menu.more_menu_kb()))
    for old in ("cat_price", "cat_analysis", "cat_strategy", "cat_okx",
                "cat_binance", "cat_bybit", "cat_news", "cat_subs",
                "cat_alert", "cat_tools", "cat_holding", "cat_vtrade",
                "cat_help", "ask_start", "dash_refresh"):
        assert old in all_cbs, f"{old} 精简后不见了"


# ── 分析结果闭环 ─────────────────────────────────────────────────
def test_followup_carries_the_symbol():
    """带上币名，下一步就不用重新输——这正是闭环省掉的那段手工搬运。"""
    for cb in _cbs(menu.followup_kb("BANKUSDT")):
        if cb.startswith("fu:"):
            assert cb.endswith(":BANK")


def test_followup_covers_the_whole_loop():
    """分析 → 计划 / 算成本 / 算仓位 / 设预警 / 模拟 —— 一步都不缺。"""
    cbs = _cbs(menu.followup_kb("BTC"))
    for key in ("plan", "net", "risk", "alert", "vopen"):
        assert f"fu:{key}:BTC" in cbs


def test_every_followup_maps_to_a_real_command():
    """按钮点了报错比没按钮更糟。"""
    import importlib
    table = {
        "plan": ("handlers.plan", "plan_cmd"),
        "net": ("handlers.econ", "net_cmd"),
        "risk": ("handlers.riskguard", "risk"),
        "watchpct": ("handlers.watchpct", "watchpct"),
        "vopen": ("handlers.vtrade", "vopen"),
        "datacheck": ("handlers.datameta", "datacheck"),
    }
    for key, (_tip, cmd) in menu.FOLLOWUP.items():
        assert cmd in table, f"{key} → {cmd} 没有映射"
        mod, fn = table[cmd]
        assert hasattr(importlib.import_module(mod), fn), f"{mod}.{fn} 不存在"


def test_followup_tips_interpolate_symbol():
    """提示里要把币名填好，用户只补价位。"""
    for key, (tip, _cmd) in menu.FOLLOWUP.items():
        rendered = tip.format(s="BANK")
        assert "{s}" not in rendered
        assert "BANK" in rendered


def test_followup_has_back_button():
    assert "cat_analysis" in _cbs(menu.followup_kb("BTC", back="cat_analysis"))


def test_followup_handles_missing_symbol():
    """没币名时不能崩，也不能生成空的 callback。"""
    cbs = _cbs(menu.followup_kb(None))
    assert all(len(c) < 64 for c in cbs)          # Telegram 限制 64 字节
    assert any(c.startswith("fu:plan:") for c in cbs)
