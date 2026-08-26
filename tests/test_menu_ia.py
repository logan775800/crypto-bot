"""首页信息架构：合并同类，但**一个功能都不能丢**。

他的原话（2026-08-20）：「做到现在功能按钮越来越多，又杂又乱」。
首页当时 23 个入口，混着三套分类逻辑：
  · 按数据类型（市场看板/行情查询/技术分析/资讯快讯）
  · 按交易所（OKX/币安/Bybit/Gate 四个专区，是同一批功能的四份拷贝）
  · 按场景（机会扫描/风险中心/复盘中心/虚拟合约）
外加名字打架的：实用工具 vs 交易工具、订阅推送 vs 价格预警。

**「合并」和「藏起来」的分界线就是这个文件**：他明确否决过折叠到「更多」
（v1.8.0 试过、v1.10.1 撤回）。所以每一个原入口都必须仍然点得到，
只是不再占首页的位置。这里逐个验。
"""
import inspect

import pytest

from handlers import menu


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _panel_cbs(key):
    """某个面板里所有按钮的 callback_data —— 面板可能来自 CATS，也可能在 _dispatch 里手写。"""
    if key in menu.CATS:
        _text, rows = menu.CATS[key]
        return [b.callback_data for row in rows for b in row]
    return None


HOME = None


def setup_module():
    global HOME
    HOME = _cbs(menu.main_menu_kb())


# ── 首页确实变清爽了 ────────────────────────────────────────
def test_home_is_short_enough_to_see_at_once():
    """一屏看不看得完，取决于**行数**不是按钮数——两个并排只占一行。
    原来 23 个入口铺了 14 行，手机上要滑。现在 10 个功能入口 + AI + 底部工具行。"""
    rows = menu.main_menu_kb().inline_keyboard
    assert len(rows) <= 8, f"首页又铺长了：{len(rows)} 行"
    feature = [c for c in HOME
               if c not in ("cmd:home", "do:datacheck", "cat_help", "ask_start")]
    assert len(feature) <= 10, f"功能入口涨回去了：{len(feature)} 个\n{feature}"


def test_home_no_longer_lists_four_exchange_zones():
    """四个专区是同一批功能的四份拷贝，占了 4/23 的首页位置。"""
    for zone in ("cat_okx", "cat_binance", "cat_bybit", "cat_gate"):
        assert zone not in HOME, f"{zone} 不该再占首页"
    assert "cat_venues" in HOME, "要有一个合并后的交易所入口"


# ── 但一个都不能丢 ──────────────────────────────────────────
# 原首页 23 个入口 → 现在应该在哪个面板里找得到
MOVED = {
    "dash_refresh": "cat_market",      # 市场看板
    "cat_price": "cat_market",         # 行情查询
    "cat_news": "cat_market",          # 资讯快讯
    "cat_okx": "cat_venues",
    "cat_binance": "cat_venues",
    "cat_bybit": "cat_venues",
    "cat_gate": "cat_venues",
    "cat_alert": "cat_notify",         # 价格预警
    "cat_subs": "cat_notify",          # 订阅推送
}


@pytest.mark.parametrize("gone,where", MOVED.items())
def test_every_moved_entry_is_still_reachable(gone, where):
    """合并 ≠ 藏起来。他否决过折叠，这条线不能越。"""
    assert gone not in HOME
    src = inspect.getsource(menu._dispatch)
    # 合并面板是在 _dispatch 里手写的，验它确实把原入口列了出来
    seg = src.split(f'elif d == "{where}":')[1].split("elif d ==")[0]
    assert f'"{gone}"' in seg, f"{gone} 在 {where} 面板里找不到了"


@pytest.mark.parametrize("gone,where", [
    ("cat_strategy", "cat_analysis"),   # 策略回测 → 分析与图表
    ("cat_tools", "cat_calc"),          # 实用工具 → 交易工具
    ("cat_vtrade", "cat_calc"),         # 虚拟盘说明
    ("cat_holding", "cat_risk"),        # 我的持仓 → 风险中心
])
def test_merged_entries_have_a_new_home(gone, where):
    assert gone not in HOME
    cbs = _panel_cbs(where)
    if cbs is not None:
        assert gone in cbs, f"{gone} 在 {where} 面板里找不到了"
    else:
        src = inspect.getsource(menu._dispatch)
        seg = src.split(f'elif d == "{where}":')[1].split("elif d ==")[0]
        assert f'"{gone}"' in seg


def test_the_things_he_actually_uses_are_on_the_home_screen():
    """链上查币、虚拟盘、扫描是他这几天真正在用的，不该被降级。"""
    for must in ("cat_onchain", "vg:home", "cat_scan"):
        assert must in HOME, f"{must} 应该留在首页"


def test_no_more_bucket_button():
    """他明确否决过「更多」这一层（v1.8.0 试过、v1.10.1 撤回）。"""
    texts = [b.text for row in menu.main_menu_kb().inline_keyboard for b in row]
    for t in texts:
        assert "更多" not in t, f"别再加折叠入口：{t}"


def test_merged_panels_all_have_a_way_back():
    """新面板忘了返回键的话，人就困在里面了。"""
    src = inspect.getsource(menu._dispatch)
    for key in ("cat_market", "cat_venues", "cat_notify"):
        seg = src.split(f'elif d == "{key}":')[1].split("elif d ==")[0]
        assert "_back()" in seg, f"{key} 面板没有返回键"


# ── 告警入口不许再沉到第三层 ──────────────────────────────────
# 2026-08-25：写使用指南时数出来的——三个告警分别埋在「价格/条件提醒」和
# 「定期订阅推送」里面，从 /menu 点下去要**三下**。
# 他的规矩是「按钮超过两层就当成 bug 去修入口，别在说明里绕」，所以提到了
# cat_notify 这一层（原位置一个没删，那两个面板里照样点得到）。
# 这条护栏防的是下次有人整理菜单时又把它们收回去。
ALERT_ENTRIES = {
    "pump:panel": "⚡ 急涨急跌",
    "ctr:panel": "📊 合约异动",
    "p3:panel": "🚨 极端拉升",
}


@pytest.mark.parametrize("cb,name", ALERT_ENTRIES.items())
def test_alert_entries_are_two_clicks_from_home(cb, name):
    """/menu → 🔔 提醒与订阅 → 它本身，两下点得到。"""
    assert "cat_notify" in HOME, "提醒与订阅不在首页了，下面这条就无从谈起"
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "cat_notify":')[1].split("elif d ==")[0]
    assert f'"{cb}"' in seg, f"{name} 又沉回第三层了（cat_notify 面板里没有它）"


def test_alert_entries_show_whether_you_are_subscribed():
    """设了看不见等于没设——这一层也要有 ✅/⬜，不能只在下级面板里有。"""
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "cat_notify":')[1].split("elif d ==")[0]
    assert "✅" in seg and "⬜" in seg


@pytest.mark.parametrize("cb", ["pump:panel", "ctr:panel"])
def test_subscription_panel_still_has_the_alert_entries(cb):
    """提级不是搬家。他否决过「藏起来」，原来的位置得照样点得到。

    **这条原来是扫 `_dispatch` 的源码段**。订阅面板的键盘后来抽成了
    `subs_kb()`（因为 cat_subs 和 tog_* 各抄了一份，改一处会分叉），
    行为一点没变，这条却红了——又一次证明源码字符串锁的是实现细节。
    改成**构造真实键盘**来验。
    """
    cbs = [b.callback_data for row in menu.subs_kb(-100).inline_keyboard for b in row]
    assert cb in cbs, f"{cb} 从订阅面板里消失了——这是搬家不是提级"


def test_p3_entry_stays_in_the_alert_panel():
    """极端拉升那条仍然是在 _dispatch 里手写的，还没抽出来。"""
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "cat_alert":')[1].split("elif d ==")[0]
    assert '"p3:panel"' in seg


def test_market_alert_kinds_can_be_toggled_separately():
    """「新币上线」和「放量异动」以前捆在一个订阅里，只想要放量的人
    被迫连新币一起收。他问「放量异动开关在哪」时才发现粒度不对。"""
    cbs = [b.callback_data for row in menu.subs_kb(-100).inline_keyboard for b in row]
    assert "mk:newcoin" in cbs and "mk:surge" in cbs
    assert "tog_market" in cbs, "总开关也要留着"


def test_breakout_toggle_is_reachable_from_the_alert_panel():
    """`/breakout on|off` 一直存在，但开关只挂在破位结果卡自己的键盘上——
    没人会为了关一个告警先去跑一次扫描。"""
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "cat_notify":')[1].split("elif d ==")[0]
    assert "bo:" in seg, "箱体破位的开关还是找不到"


def test_subscription_keyboard_has_exactly_one_definition():
    """cat_subs 和 tog_* 以前各抄了一份一模一样的键盘：
    改了 A 忘了 B，点一下总开关新加的按钮就会从屏幕上消失。
    这类"同一段 UI 两处维护"的地方迟早分叉。"""
    src = inspect.getsource(menu._dispatch)
    assert src.count("InlineKeyboardButton(f\"{status(") == 0, \
        "订阅键盘又被抄了一份，用 subs_kb()"
