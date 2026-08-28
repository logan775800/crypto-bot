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
# ── 数据源：币安优先，Bybit 兜底 ─────────────────────────────
def test_binance_bad_symbol_does_not_leak_a_keyerror():
    """真机踩到：点【💣 AGI】回了一句「画不出来：'price'」。

    币安对不存在的币回 400 + {"code":-1121}，而代码直接取了 j["price"]，
    于是把一个光秃秃的 KeyError('price') 甩给了用户。
    **报错必须说人话**，内部异常不能原样漏出去。
    """
    import asyncio

    class _R:
        status_code = 200

        def __init__(self, j):
            self._j = j

        def json(self):
            return self._j

    class _C:
        async def get(self, url, **k):
            if "openInterestHist" in url:
                return _R([{"sumOpenInterestValue": "1", "timestamp": 1}])
            if "klines" in url:
                return _R([[1, 1, 1, 1, 1]] * 5)
            return _R({"code": -1121, "msg": "Invalid symbol."})   # ticker/price

    got = asyncio.run(Q._fetch_binance(_C(), "AGIUSDT", "1h", 10))
    assert got is None, "取不到价就该让位给 Bybit，不能抛 KeyError"


def test_error_message_names_both_venues():
    """两家都没有时，要说清是"这两家都没有"，而不是一个内部异常。"""
    import inspect
    src = inspect.getsource(Q._fetch)
    assert "币安和 Bybit" in src and "RuntimeError" in src


def test_bybit_open_interest_is_converted_to_notional():
    """Bybit 的持仓量是**币的个数**，币安直接给金额。
    不乘价格的话两条路口径不一致，图的量级会差几个数量级。"""
    import inspect
    src = inspect.getsource(Q._fetch_bybit)
    assert "oi_coins * px" in src


def test_bybit_rows_come_out_oldest_first():
    """币安旧→新，Bybit 新→旧。不翻的话 ΔOI 正负全反，
    "加仓"被当成"减仓"，整张图直接空掉。

    **这条原来断言的是 `reversed(rows)` 这个字符串在不在 `_fetch_bybit` 里。**
    加分页之后翻转搬到了 `_bybit_paged`，行为一点没变，这条却红了——
    源码字符串锁的是实现细节，重构就会误报。改成验**行为**：
    真跑一次分页，看出来的是不是时间正序。
    """
    import asyncio

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    class FakeClient:
        """按 Bybit 的真实习惯回：**新→旧**。"""

        async def get(self, url, params=None):
            end = int(params["endTime"])
            rows = [{"timestamp": str(end - i * 86_400_000), "openInterest": "1"}
                    for i in range(5)]
            return FakeResp({"retCode": 0, "result": {"list": rows}})

    out = asyncio.run(Q._bybit_paged(FakeClient(), "oi", "BTCUSDT", "1d", 5, "1d"))
    ts = [int(r["timestamp"]) for r in out]
    assert ts == sorted(ts), "分页出来必须是时间正序，否则 ΔOI 正负全反"


def test_fetch_bybit_does_not_reverse_again():
    """翻转已经在 `_bybit_paged` 里做了，这里再翻一次等于没翻——
    而且图会安静地空掉，看不出哪儿错。"""
    import inspect
    assert "reversed(rows)" not in inspect.getsource(Q._fetch_bybit)


def test_bybit_is_tried_when_binance_has_nothing():
    import inspect
    src = inspect.getsource(Q._fetch)
    assert "_fetch_binance" in src and "_fetch_bybit" in src
    assert src.index("_fetch_binance") < src.index("_fetch_bybit"), "币安优先"


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
    # 现在这排键盘有两类按钮（💣 清算地图 / 📊 谁推的），各自单独限量。
    # 原来这条断言的是总数 == LIQMAP_BUTTONS，加第二类按钮时就红了——
    # 锁总数锁的是"现在只有一类按钮"这个实现细节，不是"别挤爆屏幕"这个意图。
    assert len([c for c in cbs if c.startswith("lq:")]) == CA.LIQMAP_BUTTONS
    assert len([c for c in cbs if c.startswith("pf:")]) <= 2
    assert len(kb.inline_keyboard) <= 3, "按钮排数多了会把正文挤出屏幕"
    assert "lq:w:C9:7日" in cbs, "幅度最大的那个必须在"
    assert "pf:r:C9" in cbs, "幅度最大的那个要能直接看「谁推的」"


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


def test_drops_and_pumps_are_both_covered():
    """他专门问了"大跌也带上"——判据用的是幅度绝对值，涨跌一视同仁。"""
    import inspect
    from handlers import contract_alert as CA
    src = inspect.getsource(CA._attach_liqmap)
    assert 'abs(a["change"])' in src and 'abs(top["change"])' in src


def test_caption_points_at_the_side_that_matters():
    """涨和跌该盯的不是同一侧：砸下来看下方多单（继续往下的燃料），
    拉上去看上方空单（继续往上的燃料）。文案一样等于把最该说的省掉了。"""
    import inspect
    from handlers import contract_alert as CA
    src = inspect.getsource(CA._attach_liqmap)
    assert 'zones(m, "long")' in src and 'zones(m, "short")' in src
    assert "下方还有多少多单" in src and "上方还有多少空单" in src
    assert 'top["change"] < 0' in src, "要按方向分，不能两边一个文案"


def test_alertnow_looks_exactly_like_a_real_alert():
    """`/alertnow` 的用途就是自查"告警到底工不工作"。

    真告警带图带按钮，而补推是光秃秃的文字的话，等于那一半根本验不到——
    他问"这个怎么测试"时，这条路径就是答案，所以它必须一模一样。
    """
    import inspect
    from handlers import contract_alert as CA
    src = inspect.getsource(CA._do_alert_now)
    assert "_liq_kb(movers)" in src, "补推也要带按钮"
    assert "_attach_liqmap(bot, chat_id, movers)" in src, "补推也要配图"
    assert "bot=None" in src.splitlines()[0], "要能把 bot 传进来"


def test_alertnow_callers_pass_the_bot():
    """不传 bot 的话上面那条等于没接。"""
    import inspect
    from handlers import contract_alert as CA
    src = inspect.getsource(CA)
    assert "context.bot" in src.split("async def alert_now")[1][:400]


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


# ── 90/180 日窗口（2026-08-26 他要求加）────────────────────────
# 他问「可以加 90日 180日 和一年吗」。**一年做不到**，这是数据源的硬限制，
# 探过了就别再试：
#   币安 openInterestHist —— 只保留 30 天。换 period、limit 提到 500 都一样。
#   Bybit open-interest   —— 单次硬卡 200 根，`1d` 粒度就是 199 天上限，
#                            而它没有比 1d 更粗的 intervalTime。
# 所以 90/180 能做（只能走 Bybit），365 天做不到。

def test_long_windows_exist():
    from handlers import liqmap as L
    assert "90日" in L.WINDOWS and "180日" in L.WINDOWS
    assert L.LONG_WINDOWS == {"90日", "180日", "1年"}


def test_a_year_is_offered_via_pagination():
    """**这条判据我推翻过一次，记在这儿免得反复。**

    最初结论是「一年做不到」，理由是 Bybit 单次硬卡 200 根。那只对了一半——
    单次是 200 根，但换 startTime/endTime 能拿到更老的（实测 500~700 天前
    照样有数据）。所以分段拉就能到一年。
    **接口的"单次上限"不等于"历史上限"，下结论前先试一次翻页。**
    """
    from handlers import liqmap as L
    assert L.WINDOWS["1年"] == ("1d", 365)
    assert "1年" in L.LONG_WINDOWS


def test_paging_returns_ascending_time():
    """build_map 要旧→新，翻错方向 ΔOI 正负全反、整张图空掉。
    翻转统一在 _bybit_paged 里做，所以 _fetch_bybit 里**不能再 reversed 一次**
    ——翻两次等于没翻，而且看不出来。"""
    import inspect
    from handlers import liqmap as L
    assert "out.sort(key=key)" in inspect.getsource(L._bybit_paged)
    assert "for row in reversed(rows)" not in inspect.getsource(L._fetch_bybit)


def test_short_history_is_disclosed():
    """新上市的币点「1年」只拿得到它上市以来那几个月，
    标题却写着 1 年——不说的话没人看得出来（AKE 实测 333 天）。"""
    from handlers import liqmap as L
    m = _fake_map([(98.0, 100)], [])
    txt = L.caption(m, "AKE", "1年", 100.0, "Bybit", days=333)
    assert "实际只有 333 天" in txt
    full = L.caption(m, "BTC", "1年", 100.0, "Bybit", days=364)
    assert "实际只有" not in full


def test_long_windows_use_daily_bars():
    from handlers import liqmap as L
    assert L.WINDOWS["90日"] == ("1d", 90)
    assert L.WINDOWS["180日"] == ("1d", 180)


def test_bybit_knows_the_daily_interval():
    """两个 Bybit 接口的粒度写法不一样（"1d" vs "D"）。
    漏了映射不会报错，只会拿另一个周期的 K 线去对齐 OI，图安静地画歪。"""
    from handlers import liqmap as L
    assert L.BYBIT_IV["1d"] == ("1d", "D")


def test_long_windows_skip_binance():
    """币安只有 30 天。拿它取 90 天会**安静地只回 30 天**，
    画出来的图看着正常、实际窗口对不上标题——比画不出来更糟。"""
    import inspect
    from handlers import liqmap as L
    src = inspect.getsource(L._fetch)
    assert "LONG_WINDOWS" in src, "长窗口没跳过币安，会拿到 30 天的数据画 90 天的图"


def test_long_windows_carry_their_own_caveat():
    """**窗口越长这张图越是虚构**——模型假设那些仓还没平，
    而永续里几个月不动的仓极少。不说的话，长窗口会被当成更准的图看。"""
    from handlers import liqmap as L
    for w in L.LONG_WINDOWS:
        txt = "\n".join(L._long_caveat(w))
        assert "Bybit" in txt, "要说清这张图是 Bybit 的不是币安的"
        assert "一根一天" in txt, "要说清颗粒变粗了"
        assert "还没平" in txt, "最要紧的那条假设必须写出来"


def test_short_windows_have_no_extra_caveat():
    from handlers import liqmap as L
    assert L._long_caveat("7日") == []


def test_buttons_split_short_and_long():
    """五个窗口挤一行会变成一排点不准的小方块；
    分两行顺带把「这两个是另一类」用排版说出来。"""
    from handlers import liqmap as L
    rows = L.kb("BTC", "7日").inline_keyboard
    assert [b.text.lstrip("✅") for b in rows[0]] == ["1日", "7日", "30日"]
    assert [b.text.lstrip("✅") for b in rows[1]] == ["90日", "180日", "1年"]


# ── 合计待爆 + 按距离累计（2026-08-26 他要的）──────────────────
# 他的原话：「我还想知道选择的时间周期不同对应还有多少空/多会被清算」。
# 只列前三个密集区回答不了这个——密集区说的是「堆在哪儿」，
# 合计和累计说的是「一共有多少、扫过去 5% 会引爆多少」。
# 而且这几个数正是**切换窗口时真正会变**的东西。

def _fake_map(long_at, short_at, last=100.0):
    """造一张只有指定价位有量的图。"""
    from handlers import liqmap as L
    m = {"edges": [last * 0.7 + i * (last * 0.6 / L.BUCKETS) for i in range(L.BUCKETS)],
         "width": last * 0.6 / L.BUCKETS,
         "longs": {lv: [0.0] * L.BUCKETS for lv, _w, _c in L.LEVS},
         "shorts": {lv: [0.0] * L.BUCKETS for lv, _w, _c in L.LEVS}}
    lv0 = L.LEVS[0][0]
    for px, amt in long_at:
        i = min(L.BUCKETS - 1, max(0, int((px - m["edges"][0]) / m["width"])))
        m["longs"][lv0][i] += amt
    for px, amt in short_at:
        i = min(L.BUCKETS - 1, max(0, int((px - m["edges"][0]) / m["width"])))
        m["shorts"][lv0][i] += amt
    return m


def test_totals_sum_the_whole_side():
    from handlers import liqmap as L
    m = _fake_map([(98.0, 100), (90.0, 200)], [])
    t = L.totals(m, "long", 100.0)
    assert t["all"] == pytest.approx(300)


def test_cumulative_buckets_respect_distance():
    """跌3%内只该算进 -2% 那一档，不该把 -10% 的也算进去。"""
    from handlers import liqmap as L
    m = _fake_map([(98.0, 100), (90.0, 200)], [])
    t = L.totals(m, "long", 100.0)
    assert t["d3"] == pytest.approx(100)
    assert t["d5"] == pytest.approx(100)
    assert t["d10"] == pytest.approx(300)


def test_nearest_cluster_distance_is_reported():
    """三个累计全是 0 时要能解释为什么——不然一行三个 0 看着就是坏了。"""
    from handlers import liqmap as L
    m = _fake_map([(80.0, 500)], [])
    t = L.totals(m, "long", 100.0)
    assert t["d10"] == 0 and t["all"] > 0
    assert 19 <= t["near"] <= 21
    assert "最近一档" in L._near_note(t, "跌")


def test_near_note_is_silent_when_there_is_something_close():
    from handlers import liqmap as L
    m = _fake_map([(98.0, 100)], [])
    assert L._near_note(L.totals(m, "long", 100.0), "跌") == ""


def test_fuel_line_only_speaks_when_the_gap_is_real():
    """差得不明显就别下结论——硬解读噪音比不解读更糟。"""
    from handlers import liqmap as L
    close = L._fuel_line({"all": 100}, {"all": 110})
    assert "接近" in close
    assert "下方是上方" in L._fuel_line({"all": 300}, {"all": 100})
    assert "上方是下方" in L._fuel_line({"all": 100}, {"all": 300})


def test_fuel_line_needs_both_sides():
    from handlers import liqmap as L
    assert L._fuel_line({"all": 0}, {"all": 100}) == ""


# ── 来源要写在脸上 ────────────────────────────────────────────
def test_source_reaches_the_caption_and_the_chart():
    """标题以前写死「币安永续」，而 90/180 日的数据其实来自 Bybit。
    一张 Bybit 的图挂着币安的抬头，没人看得出来——**口径写错比不写更糟**。
    图会被单独转发出去，所以图里也要有。"""
    import inspect
    from handlers import liqmap as L
    assert "src" in inspect.signature(L.caption).parameters
    assert "src" in inspect.signature(L.render).parameters
    assert "币安永续" not in inspect.getsource(L.render), "标题还写死着币安"


def test_caption_shows_the_given_source():
    from handlers import liqmap as L
    m = _fake_map([(98.0, 100)], [(102.0, 50)])
    assert "Bybit永续" in L.caption(m, "BTC", "180日", 100.0, "Bybit")
    assert "币安永续" in L.caption(m, "BTC", "7日", 100.0, "币安")


# ── 按杠杆分档的未触发清算簇（2026-08-27 他给的口径）──────────
# 「按 5x/10x/20x 分档，反推多空两侧仍未被触发的清算价位与金额，
#   仅列出 ≥10 万美元的清算簇」。

def test_tier_levels_match_the_requested_spec():
    from handlers import liqmap as L
    assert [lv for lv, _w, _c in L.TIER_LEVS] == [5, 10, 20]
    assert sum(w for _lv, w, _c in L.TIER_LEVS) == pytest.approx(1.0)
    assert L.TIER_FLOOR == 100_000


def test_chart_tiers_are_left_alone():
    """**刻意不改 LEVS**：图上是 5/10/25/50，改它等于让所有人
    已经在看的那张图整个变形。两套并存，同一批 ΔOI 重新分配。"""
    from handlers import liqmap as L
    assert [lv for lv, _w, _c in L.LEVS] == [5, 10, 25, 50]


def test_build_map_accepts_custom_tiers():
    from handlers import liqmap as L
    m = L.build_map(_oi([0, 1_000_000]), _kl([100.0, 100.0]), 100.0,
                    levs=L.TIER_LEVS)
    assert set(m["longs"]) == {5, 10, 20}
    assert m["levs"] == L.TIER_LEVS


def test_clusters_only_return_untriggered_side():
    """现价越过的一侧 build_map 已经抹零了，所以非零的就是还没被扫到的。"""
    from handlers import liqmap as L
    m = L.build_map(_oi([0, 1_000_000]), _kl([100.0, 100.0]), 100.0,
                    levs=L.TIER_LEVS)
    for z in L.clusters(m, "long", 20, floor=0):
        assert z["hi"] <= 100.0 * 1.001, "多头爆仓位不可能在现价上方"
    for z in L.clusters(m, "short", 20, floor=0):
        assert z["lo"] >= 100.0 * 0.999, "空头爆仓位不可能在现价下方"


def test_adjacent_buckets_merge_into_one_cluster():
    """不合并的话，一个宽区间会被切成几十根小柱，每根都不到 10 万门槛，
    于是**明明有一大堆待爆仓位却一条都列不出来**。"""
    from handlers import liqmap as L
    m = L.build_map(_oi([0, 5e6, 1e7]), _kl([100.0, 100.5, 101.0]), 100.0,
                    levs=L.TIER_LEVS)
    zs = L.clusters(m, "long", 20, floor=0)
    assert zs and zs[0]["hi"] > zs[0]["lo"]


def test_low_density_buckets_do_not_glue_everything_together():
    """**第一版就栽在这**：按"非零就合并"写，7 日 168 根数据下几乎每个桶
    都有值，相邻非零全连成一条横跨整个价格区间的假「簇」。
    以峰值的一定比例为界，簇才切得开。"""
    from handlers import liqmap as L
    assert 0 < L.DENSITY_FRAC < 1
    import inspect
    assert "peak * DENSITY_FRAC" in inspect.getsource(L.clusters)


def test_floor_filters_small_clusters():
    from handlers import liqmap as L
    m = L.build_map(_oi([0, 1_000]), _kl([100.0, 100.0]), 100.0, levs=L.TIER_LEVS)
    assert L.clusters(m, "long", 20, floor=100_000) == []


def test_report_lists_every_tier_and_both_sides():
    from handlers import liqmap as L
    m = L.build_map(_oi([0, 5e8, 1e9]), _kl([100.0, 100.5, 101.0]), 100.0,
                    levs=L.TIER_LEVS)
    txt = L.tier_report(m, "BTC", "7日", 100.0)
    for lev in ("5x", "10x", "20x"):
        assert lev in txt
    assert "多头爆仓" in txt and "空头爆仓" in txt
    assert "距现价" in txt


def test_report_says_why_when_nothing_qualifies():
    """一条都没有时要说清是哪种没有——门槛太高还是这个币本来就没量。"""
    from handlers import liqmap as L
    m = L.build_map(_oi([0, 100]), _kl([100.0, 100.0]), 100.0, levs=L.TIER_LEVS)
    txt = L.tier_report(m, "TINY", "7日", 100.0)
    assert "没有任何一侧的簇达到" in txt
    assert "换 30日" in txt


def test_report_keeps_the_estimate_disclaimer():
    """这张明细看起来比图更"精确"，估算两个字更不能丢。"""
    from handlers import liqmap as L
    m = L.build_map(_oi([0, 1e9]), _kl([100.0, 100.0]), 100.0, levs=L.TIER_LEVS)
    txt = L.tier_report(m, "BTC", "7日", 100.0)
    assert "模型估算" in txt and "假设" in txt


def test_tier_button_exists():
    from handlers import liqmap as L
    cbs = [b.callback_data for row in L.kb("BTC", "7日").inline_keyboard for b in row]
    assert "lq:t:BTC:7日" in cbs


def test_tier_view_does_not_reuse_the_chart_cache():
    """缓存里那份是按图上的 5/10/25/50 算的，档位不同整份数据都不同。"""
    import inspect
    from handlers import liqmap as L
    src = inspect.getsource(L.from_btn)
    seg = src.split('if action == "t":')[1].split("uid =")[0]
    assert "_fetch(" in seg and "_get(" not in seg
