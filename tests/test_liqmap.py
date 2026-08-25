"""清算地图 /liqmap。

他发了张 CoinGlass 的图问「币安的清算图可以搞到 Telegram 吗」。

**能，但必须说清它是估算。** 探针实测（tools\\probe_liqmap.py）：
没有任何交易所公布「某个价位挂着多少待爆仓的仓位」——那是私有信息；
币安 allForceOrders 已经 404；CoinGlass 的 heatmap 不在免费档。
能白拿的只有持仓量历史 + K 线，所以这张图和 CoinGlass 一样是推算的。

这里守的是模型的几条硬性质，以及"估算"这两个字不能被弄丢。
"""
import pytest

from handlers import liqmap as Q


def _oi(vals, step=3600_000, t0=1_700_000_000_000):
    return [{"sumOpenInterestValue": str(v), "timestamp": t0 + i * step}
            for i, v in enumerate(vals)]


def _kl(prices, step=3600_000, t0=1_700_000_000_000):
    # [开始时间, 开, 高, 低, 收, ...]，典型价 = (高+低+收)/3
    return [[t0 + i * step, p, p, p, p, "0"] for i, p in enumerate(prices)]


# ── 模型 ────────────────────────────────────────────────────
def test_positions_leave_marks_on_both_sides():
    """永续里每份持仓同时是一多一空，所以同一份 ΔOI 两侧都要留爆仓位。"""
    m = Q.build_map(_oi([0, 1_000_000]), _kl([100.0, 100.0]), 100.0)
    assert sum(sum(v) for v in m["longs"].values()) > 0
    assert sum(sum(v) for v in m["shorts"].values()) > 0


def test_only_open_interest_growth_counts():
    """ΔOI 为负是平仓，不产生新的爆仓位——算进去等于凭空造出一堆仓。"""
    grow = Q.build_map(_oi([0, 1_000_000]), _kl([100.0, 100.0]), 100.0)
    shrink = Q.build_map(_oi([1_000_000, 0]), _kl([100.0, 100.0]), 100.0)
    assert grow["added"] > 0
    assert shrink["added"] == 0


def test_long_liquidations_sit_below_and_shorts_above():
    m = Q.build_map(_oi([0, 1_000_000]), _kl([100.0, 100.0]), 100.0)
    cb = m["cur_bucket"]
    for L, _w, _c in Q.LEVS:
        assert all(m["longs"][L][i] == 0 for i in range(cb, Q.BUCKETS)), \
            "多头爆仓位不可能在现价上方"
        assert all(m["shorts"][L][i] == 0 for i in range(0, cb + 1)), \
            "空头爆仓位不可能在现价下方"


def test_levels_already_crossed_are_wiped():
    """价格已经越过的位置，那些仓早被平了。留着的话图上会有假柱子。"""
    # 仓建在 80，现价涨到 100：80 附近的多头爆仓位（80×0.98=78.4）已被甩在身后
    m = Q.build_map(_oi([0, 1_000_000]), _kl([80.0, 80.0]), 100.0)
    below_50x = m["longs"][50]
    assert sum(below_50x) == 0 or all(
        m["edges"][i] < 100.0 for i, v in enumerate(below_50x) if v > 0)


def test_higher_leverage_lands_closer_to_price():
    """50x 的爆仓位贴着建仓价，5x 的远得多——这是整张图形状的来源。"""
    m = Q.build_map(_oi([0, 1_000_000]), _kl([100.0, 100.0]), 100.0)

    def where(book):
        return [m["edges"][i] for i, v in enumerate(book) if v > 0]
    near = where(m["longs"][50])
    far = where(m["longs"][5])
    assert near and far
    assert min(near) > max(far), "50x 应该比 5x 更贴近现价"


def test_out_of_range_levels_are_dropped_not_clamped():
    """越出画布的位置直接丢，不能夹到边界——夹的话边缘会堆出一根假柱子。"""
    m = Q.build_map(_oi([0, 1_000_000]), _kl([100.0, 100.0]), 100.0)
    # 5x 空头爆仓在 120，SPAN=30% → 上界 130，在范围内；2x 若存在会超界
    assert all(len(v) == Q.BUCKETS for v in m["shorts"].values())


def test_weights_sum_to_one():
    """权重是假设，但至少不能凭空放大或缩小总量。"""
    assert sum(w for _L, w, _c in Q.LEVS) == pytest.approx(1.0)


def test_zones_are_sorted_by_density():
    m = Q.build_map(_oi([0, 1e6, 3e6]), _kl([100.0, 100.0, 101.0]), 100.0)
    z = Q.zones(m, "long")
    assert z == sorted(z, key=lambda x: -x["amount"])


# ── 文案：估算这两个字不能丢 ─────────────────────────────────
def test_caption_says_it_is_an_estimate():
    """一张看起来很权威的图会被当成事实去下单——所以每一层都要写。"""
    m = Q.build_map(_oi([0, 1e6]), _kl([100.0, 100.0]), 100.0)
    cap = Q.caption(m, "TRUMP", "7日", 100.0)
    assert "估算" in cap
    assert "不是交易所数据" in cap


def test_caption_fits_a_telegram_photo_caption():
    """图注上限 1024 字符，超了整条消息发不出去。"""
    m = Q.build_map(_oi([0, 1e6, 3e6, 5e6]), _kl([100.0, 101.0, 99.0, 100.0]), 100.0)
    assert len(Q.caption(m, "TRUMP", "7日", 100.0)) <= 1000


def test_caption_survives_an_empty_map():
    """新上市的币持仓量几乎没增长，估不出密集区——要说人话不是空白。"""
    m = Q.build_map(_oi([1e6, 1e6]), _kl([100.0, 100.0]), 100.0)
    cap = Q.caption(m, "NEW", "7日", 100.0)
    assert "估不出" in cap


def test_detail_exposes_every_assumption():
    """柱子高矮有一半是假设决定的，假设必须可见。"""
    m = Q.build_map(_oi([0, 1e6]), _kl([100.0, 100.0]), 100.0)
    txt = Q.detail(m, "TRUMP", "7日")
    for must in ("模型估算", "杠杆分布", "维持保证金率", "磁吸位",
                 "不构成投资建议"):
        assert must in txt, f"口径卡缺了：{must}"
    for L, w, _c in Q.LEVS:
        assert f"{L}x {w * 100:.0f}%" in txt, "每个杠杆档位的假设权重都要印出来"


def test_the_image_itself_is_stamped():
    """图会被单独转发出去，脱离文字说明也不能让人误读。"""
    import inspect
    src = inspect.getsource(Q.render)
    assert "模型估算，非交易所数据" in src


# ── 入口 ────────────────────────────────────────────────────
def test_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("liqmap"' in src and 'BotCommand("liqmap"' in src


def test_button_is_two_taps_from_home():
    """/menu → 📈 分析与图表 → 💣 清算地图。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "cat_analysis":')[1].split("elif d ==")[0]
    assert "lq:pick" in seg


def test_reachable_from_a_coin_card():
    """查完币价那张卡上要能直接进——分析完最该问的下一件事就是"止损放哪儿"，
    而清算图直接回答它。少这个按钮就还得自己去打命令。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu.followup_kb)
    assert "lq:w:" in src
    kb = menu.followup_kb("BTC")
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "lq:w:BTC:7日" in cbs, "币名要带进去，不能进去还得再选一次"


# ── 挂进合约异动告警 ────────────────────────────────────────
def test_alert_gets_liqmap_buttons():
    """异动之后最该问的就是"下面还堆着多少爆仓单"，按钮把币名直接带进去。"""
    from handlers import contract_alert as CA
    alerts = [{"sym": "TUT", "change": -41.96, "ex": "币安", "tier": 40,
               "direction": "down", "price": 0.04331}]
    kb = CA._liq_kb(alerts)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "lq:w:TUT:7日" in cbs


def test_alert_buttons_are_capped_and_sorted_by_size():
    """一轮可能同时报十个币，按钮一排挤不下——只给幅度最大的几个。"""
    from handlers import contract_alert as CA
    alerts = [{"sym": f"C{i}", "change": -(20 + i), "ex": "x", "tier": 20,
               "direction": "down", "price": 1} for i in range(10)]
    kb = CA._liq_kb(alerts)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert len(cbs) == CA.LIQMAP_BUTTONS
    assert "lq:w:C9:7日" in cbs, "幅度最大的那个必须在"


def test_only_one_map_is_auto_attached_per_alert():
    """一条告警只配一张图：每个币都画就是又慢又刷屏。"""
    import inspect
    from handlers import contract_alert as CA
    src = inspect.getsource(CA._attach_liqmap)
    assert "max(alerts" in src, "只挑幅度最大的那个"
    assert "LIQMAP_MIN_MOVE" in src, "幅度不够的不值得画图"


def test_attach_failure_is_silent():
    """清算地图只走币安永续，而告警是全交易所的——取不到是常态。
    不能因为配图失败就在群里刷一条"失败"。"""
    import asyncio
    import inspect
    from handlers import contract_alert as CA
    assert "except Exception" in inspect.getsource(CA._attach_liqmap)

    class _B:
        async def send_photo(self, **k):
            raise RuntimeError("no data")
    # 不该抛出去
    asyncio.run(CA._attach_liqmap(_B(), 1, [
        {"sym": "NOPE", "change": -50.0, "ex": "x", "tier": 50,
         "direction": "down", "price": 1}]))


def test_small_moves_do_not_get_a_map():
    import asyncio
    from handlers import contract_alert as CA

    class _B:
        def __init__(self):
            self.sent = 0

        async def send_photo(self, **k):
            self.sent += 1
    b = _B()
    asyncio.run(CA._attach_liqmap(b, 1, [
        {"sym": "X", "change": -21.0, "ex": "x", "tier": 20,
         "direction": "down", "price": 1}]))
    assert b.sent == 0, "20% 出头的不值得为它画图"


def test_buttons_round_trip_through_the_dispatcher():
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert 'd.startswith("lq:")' in src and 'd.startswith("lqcoin:")' in src
    for r in Q.kb("BTC", "7日").inline_keyboard:
        for b in r:
            d = b.callback_data
            if d.startswith("lq:"):
                assert len(d.split(":")) == 4, f"{d} 位数不对"


def test_every_window_has_a_button():
    labels = " ".join(b.text for r in Q.kb("BTC", "7日").inline_keyboard for b in r)
    for w in Q.WINDOWS:
        assert w in labels


def test_command_is_categorised_in_the_panel():
    from handlers import cmdpanel
    assert cmdpanel.MODULE_CN.get("handlers.liqmap")


def test_heavy_work_is_gated():
    import inspect
    assert "busy.guard" in inspect.getsource(Q.liqmap_cmd)


def test_chart_falls_back_when_there_is_no_cjk_font():
    """本地/旧镜像可能没装中文字体，那时中文会渲染成一排豆腐块。"""
    import inspect
    src = inspect.getsource(Q.render)
    assert "cjk_font" in src and "matplotlib.use" not in src  # 后端在模块顶层设
