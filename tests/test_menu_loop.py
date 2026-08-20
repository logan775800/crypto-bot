"""首页精简 + 分析结果闭环。

闭环这条是这次改动里价值最高的：以前分析完是死胡同，用户要自己把币名和
价位搬到 /net、/risk、/plan 去。中间那段手工搬运正是「看完就算了」的原因。
"""
import pytest

from handlers import menu


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ── 入口一个都不藏 ─────────────────────────────────────────────
# 判据在 2026-08-20 变过一次，这里记下来免得下次误以为是放松了要求：
#   原来：**每个入口都必须在首页**（v1.8.0 精简到 12 个 + 「更多」被否，
#         v1.10.1 恢复全平铺）。
#   现在：他说「功能按钮越来越多，又杂又乱」，选了「重组分类 23→10」。
#         所以要求变成 **必须仍然点得到**，而不是必须在首页第一层。
# 「更多」那种收纳仍然是禁止的——见 test_no_more_layer 和
# tests/test_menu_ia.py::test_no_more_bucket_button。
def _reachable(cb):
    """从首页出发点得到吗：在首页上，或在某个分类面板里列着。"""
    import inspect
    if cb in _cbs(menu.main_menu_kb()):
        return True
    for _text, rows in menu.CATS.values():
        if cb in [b.callback_data for r in rows for b in r]:
            return True
    return f'"{cb}"' in inspect.getsource(menu._dispatch)


def test_every_entry_is_still_reachable():
    for must in ("dash_refresh", "cat_price", "cat_analysis", "cat_strategy",
                 "cat_okx", "cat_binance", "cat_bybit", "cat_news", "cat_subs",
                 "cat_alert", "cat_tools", "ask_start", "cat_scan", "cat_calc",
                 "cat_risk", "cat_review", "cat_holding", "cat_vtrade",
                 "cat_help"):
        assert _reachable(must), f"{must} 点不到了"


def test_no_more_layer():
    """不该再有「更多」这一层——它是被否掉的方案。"""
    assert "cat_more" not in _cbs(menu.main_menu_kb())
    assert not hasattr(menu, "more_menu_kb")


def test_system_health_reachable_by_button():
    """系统体检不该只能靠打命令。"""
    assert "do:datacheck" in _cbs(menu.main_menu_kb())


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
