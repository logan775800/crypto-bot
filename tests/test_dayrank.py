"""多日涨跌榜（/rank 3、/rank 7）—— 币安/Bybit/OKX × 永续+现货。

群里有人问「来个 3 日跌涨幅榜」，机器人答不了：所有的榜都是 24h，
而 `/upstreak` 是"连续 N 天同向"，那是另一件事（3 天累计涨 30% 但中间回调过
一天的币，在连涨榜里根本不存在）。

这里守的每一条都是真机冒烟当场抓到的，没有一条是想出来的：
  1. 涨幅榜和跌幅榜**按正负切开**——"前 N + 后 N" 会把 -30% 的币列进涨幅榜；
  2. `1000PEPE` 和 `PEPE` 是同一个币，但**只合并纯面值前缀**
     （PUMP / PUMPFUN 必须分开，合错就是把两个币的行情算到一起）；
  3. 代币化美股要剔（SNDK、SPCXB）；
  4. 币安日线是**旧→新**，和另外两家相反，不反转算出来的是另一段时间；
  5. 日线取失败要**退避重试并报数**——OKX 单独扫时曾经 94 个候选只回来 40 个，
     54 个被静默丢掉，榜还照排；
  6. 卡片**不能太长**：Telegram 的按钮永远在消息末尾，消息一长按钮就被挤出屏幕，
     他直接问「有做功能按钮吗」。
"""
import pytest

from handlers import dayrank as D


def row(sym, venue="bybit", market="perp", closes=None, turnover=1e7):
    return {"sym": sym, "key": D.norm_base(sym), "venue": venue, "market": market,
            "inst": sym + "USDT", "turnover": turnover,
            "closes": closes if closes is not None else [110.0] + [100.0] * 14}


# ── 口径 ────────────────────────────────────────────────────
def test_pct_is_now_versus_the_close_n_days_ago():
    """终点用现价（今天那根未收盘 K 线的最新价），不是昨天的收盘价。"""
    r = row("A", closes=[130.0, 120.0, 110.0, 100.0, 90.0])
    assert D.pct(r, 3) == pytest.approx(30.0)     # 130 vs closes[3]=100
    assert D.pct(r, 1) == pytest.approx(8.3333, rel=1e-3)


def test_pct_is_none_when_there_are_not_enough_candles():
    """不够根数返回 None，不是 0——0 会假装这个币没涨没跌，混进榜里当中位数。"""
    assert D.pct(row("NEW", closes=[200.0, 100.0]), 7) is None


def test_zero_base_does_not_explode():
    assert D.pct(row("Z", closes=[10.0, 0.0]), 1) is None


# ── 分组：按正负切，不是取头尾 ────────────────────────────────
def test_gainers_and_losers_never_overlap():
    """真机冒烟抓到的：币少时 -30% 的币会同时出现在涨幅榜第三名。"""
    rows = [row("UP", closes=[130.0] + [100.0] * 14),
            row("DOWN", closes=[70.0] + [100.0] * 14),
            row("FLATUP", closes=[101.0] + [100.0] * 14)]
    up, down, _st = D.ranked(rows, 3)
    up_syms = {r["sym"] for _p, r in up}
    down_syms = {r["sym"] for _p, r in down}
    assert not (up_syms & down_syms)
    assert up_syms == {"UP", "FLATUP"} and down_syms == {"DOWN"}


def test_losers_are_sorted_worst_first():
    rows = [row("A", closes=[90.0] + [100.0] * 14),
            row("B", closes=[70.0] + [100.0] * 14)]
    _up, down, _st = D.ranked(rows, 3)
    assert [r["sym"] for _p, r in down] == ["B", "A"]


def test_all_red_market_still_prints_the_gainers_group():
    """空的分组也要印：整段消失读起来像"根本没扫这一边"，
    而"这 3 天一个上涨的都没有"本身就是最重要的信息。"""
    rows = [row("A", closes=[99.0] + [100.0] * 14),
            row("B", closes=[97.0] + [100.0] * 14)]
    txt = D.build_text(rows, 3, "bybit", "perp", {})
    assert "涨幅榜*（0）" in txt and "没有一个币是涨的" in txt
    assert "跌得最少" in txt


def test_all_green_market_still_prints_the_losers_group():
    txt = D.build_text([row("A", closes=[130.0] + [100.0] * 14)], 3, "bybit", "perp", {})
    assert "跌幅榜*（0）" in txt and "没有一个币是跌的" in txt


# ── 🔥 热榜：只在成交额前 N 名里排 ───────────────────────────
def _pool(n):
    """n 个币，成交额递减，涨幅也递减——成交额最小的那个涨得最少。"""
    return [row(f"C{i}", turnover=(n - i) * 1e6,
                closes=[100.0 + (n - i)] + [100.0] * 14) for i in range(n)]


def test_hot_only_ranks_the_top_by_turnover():
    """群里的原话是「最好只看热榜的」——全量榜一屏全是没听过的代号。"""
    rows = _pool(D.HOT_N + 20)
    up_all, _d, st_all = D.ranked(rows, 3, hot=False)
    up_hot, _d2, st_hot = D.ranked(rows, 3, hot=True)
    assert st_all["n"] == D.HOT_N + 20
    assert st_hot["n"] == D.HOT_N, "热榜只在成交额前 N 名里排"
    # 冷门币（成交额最小的那批）不该出现在热榜里
    cold = {f"C{i}" for i in range(D.HOT_N, D.HOT_N + 20)}
    assert not ({r["sym"] for _p, r in up_hot} & cold)


def test_hot_is_a_display_filter_not_a_rescan():
    """热榜和全量用的是同一批数据，切换只该是一次重排。"""
    import inspect
    assert "hot" in inspect.signature(D.ranked).parameters
    # scan() 不认识 hot——它不该为了换个范围再跑一遍网络
    assert "hot" not in inspect.signature(D.scan).parameters


def test_hot_pool_smaller_than_the_cutoff_keeps_everything():
    rows = _pool(5)
    assert D.ranked(rows, 3, hot=True)[2]["n"] == 5


def test_card_says_which_pool_it_ranked():
    """口径写在脸上：不写的话热榜看起来就像"全市场就这几个"。"""
    rows = _pool(D.HOT_N + 20)
    hot_txt = D.build_text(rows, 3, "all", "all", {}, hot=True)
    assert f"前 {D.HOT_N} 名" in hot_txt and "热榜" in hot_txt
    assert f"共 {len(rows)} 个币" in hot_txt, "要告诉他全量有多少、去哪看"
    full_txt = D.build_text(rows, 3, "all", "all", {}, hot=False)
    assert "全部" in full_txt and "热榜" not in full_txt


# ── 去重 ────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,norm", [
    ("1000PEPE", "PEPE"), ("10000SATS", "SATS"), ("1MBABYDOGE", "BABYDOGE"),
])
def test_denomination_prefix_is_normalised(raw, norm):
    assert D.norm_base(raw) == norm


@pytest.mark.parametrize("sym", ["BTC", "1INCH", "100X", "PUMP", "PUMPFUN"])
def test_normalisation_does_not_over_merge(sym):
    """宁可少合一个，不能合错一个。1INCH 不是"1 × INCH"；
    PUMP 和 PUMPFUN 看着像一对，合错就是把两个币的行情算到一起。"""
    assert D.norm_base(sym) == sym


# ── 杂质过滤 ────────────────────────────────────────────────
@pytest.mark.parametrize("base,keep", [
    ("BTC", True), ("PEPE", True),
    ("USDC", False), ("USDG", False), ("RLUSD", False),   # 稳定币
    ("BTC3L", False), ("ETH3S", False),                   # 杠杆代币
])
def test_junk_pairs_are_dropped(base, keep):
    assert D._keep(base) is keep


def test_a_pegged_price_series_is_detected_as_a_stablecoin():
    """硬名单追不上新发的稳定币——USDG/RLUSD 就以 -0.1% 占了跌幅榜三个位置。
    按价格判：连续十几天贴着 1 元的不会是别的东西。"""
    assert D._is_peg([1.0002, 0.9998, 1.0001] * 5) is True
    assert D._is_peg([1.05, 0.97, 1.0]) is False
    assert D._is_peg([4.0e-06] * 15) is False


# ── 扫描（打桩，不联网）──────────────────────────────────────
def _stub(monkeypatch, rows_by_job, skip=frozenset(), fail=frozenset()):
    """rows_by_job: {(venue, market): [(inst, base, turnover), ...]}"""
    def mk_uni(v):
        async def _u(client, market):
            return rows_by_job.get((v, market), [])
        return _u

    def mk_kl(v):
        async def _k(client, market, inst):
            if inst in fail:
                raise RuntimeError("429")
            return [130.0] + [100.0] * 14
        return _k

    monkeypatch.setattr(D, "_UNI", {v: mk_uni(v) for v in D.VENUES})
    monkeypatch.setattr(D, "_KL", {v: mk_kl(v) for v in D.VENUES})

    async def _skip():
        return set(skip)
    import handlers.klines as K
    monkeypatch.setattr(K, "noncrypto_bases", _skip)


def test_scan_dedupes_across_venues_and_markets_keeping_the_deepest(monkeypatch):
    """同一个币在六个盘子里出现四五次，留成交额最大的那个。"""
    import asyncio
    _stub(monkeypatch, {
        ("bybit", "perp"): [("PEPEUSDT", "PEPE", 1e6)],
        ("binance", "perp"): [("1000PEPEUSDT", "1000PEPE", 9e6)],
        ("okx", "spot"): [("PEPE-USDT", "PEPE", 3e6)],
    })
    rows, stats = asyncio.run(D.scan("all", "all"))
    assert len(rows) == 1, "1000PEPE 和 PEPE 是同一个币"
    assert rows[0]["venue"] == "binance", "留成交额最大的那个盘子"
    assert stats["unique"] == 1 and stats["raw"] == 3


def test_scan_only_fetches_klines_for_deduped_candidates(monkeypatch):
    """先去重再拉日线：反过来做等于把四分之三的请求花在重复的币上。"""
    import asyncio
    _stub(monkeypatch, {
        ("bybit", "perp"): [("BTCUSDT", "BTC", 9e9)],
        ("binance", "perp"): [("BTCUSDT", "BTC", 8e9)],
        ("okx", "perp"): [("BTC-USDT-SWAP", "BTC", 7e9)],
    })
    _rows, stats = asyncio.run(D.scan("all", "perp"))
    assert stats["raw"] == 3 and stats["fetched"] == 1


def test_scan_drops_tokenised_stocks(monkeypatch):
    import asyncio
    _stub(monkeypatch, {("bybit", "perp"): [("BTCUSDT", "BTC", 1e9),
                                            ("SNDKUSDT", "SNDK", 5e8)]},
          skip={"SNDK"})
    rows, stats = asyncio.run(D.scan("bybit", "perp"))
    assert [r["sym"] for r in rows] == ["BTC"] and stats["stock"] == 1


def test_binance_spot_only_stocks_are_dropped_by_the_fallback_list(monkeypatch):
    """币安现货接口没有任何品类字段（实测 SPCXB 和 BTCUSDT 的 permissionSets
    一模一样），noncrypto_bases() 认不出，只能靠名单挡。"""
    import asyncio
    _stub(monkeypatch, {("binance", "spot"): [("BTCUSDT", "BTC", 1e9),
                                              ("SPCXBUSDT", "SPCXB", 5e8)]})
    rows, stats = asyncio.run(D.scan("binance", "spot"))
    assert [r["sym"] for r in rows] == ["BTC"] and stats["stock"] == 1


def test_thin_pairs_are_filtered_and_counted(monkeypatch):
    import asyncio
    _stub(monkeypatch, {("bybit", "perp"): [("BTCUSDT", "BTC", 1e9),
                                            ("DEADUSDT", "DEAD", 1000.0)]})
    rows, stats = asyncio.run(D.scan("bybit", "perp"))
    assert [r["sym"] for r in rows] == ["BTC"] and stats["thin"] == 1


def test_failed_klines_are_counted_not_silently_dropped(monkeypatch):
    """OKX 单独扫时曾经 94 个候选只回来 40 个，54 个静默消失、榜照排。"""
    import asyncio
    _stub(monkeypatch, {("bybit", "perp"): [("AUSDT", "A", 9e8), ("BUSDT", "B", 8e8)]},
          fail={"BUSDT"})
    rows, stats = asyncio.run(D.scan("bybit", "perp"))
    assert len(rows) == 1
    assert stats["failed"] == 1
    assert "1 个币的日线没取到" in D.build_text(rows, 3, "bybit", "perp", stats)


def test_scan_survives_one_venue_being_down(monkeypatch):
    """币安在某些地区连不上。挂一家不能让整张榜挂掉，但要说出来。"""
    import asyncio

    async def _boom(client, market):
        raise RuntimeError("blocked")
    _stub(monkeypatch, {("bybit", "perp"): [("BTCUSDT", "BTC", 1e9)]})
    monkeypatch.setitem(D._UNI, "binance", _boom)
    rows, stats = asyncio.run(D.scan("all", "perp"))
    assert [r["sym"] for r in rows] == ["BTC"]
    assert stats["dead"] and "币安" in stats["dead"][0]
    assert "取不到" in D.build_text(rows, 3, "all", "perp", stats)


# ── 币安 K 线方向（最容易看不出来的错）───────────────────────
def test_binance_klines_are_reversed_to_newest_first():
    """币安返回**旧→新**，Bybit/OKX 是新→旧。不反转算出来的是另一段时间，
    而且符号经常还是对的，肉眼根本看不出错。"""
    import asyncio

    class _R:
        @staticmethod
        def json():
            # 旧→新：100 → 110 → 130
            return [[0, "1", "1", "1", "100"], [0, "1", "1", "1", "110"],
                    [0, "1", "1", "1", "130"]]

    class _C:
        async def get(self, *a, **k):
            return _R()

    closes = asyncio.run(D._kl_binance(_C(), "perp", "BTCUSDT"))
    assert closes[0] == 130.0, "closes[0] 必须是最新那根"
    assert D.pct({"closes": closes}, 2) == pytest.approx(30.0)


# ── 卡片 ────────────────────────────────────────────────────
def test_card_stays_short_enough_for_the_buttons_to_be_reachable():
    """Telegram 的按钮永远在消息末尾。v1.36.0 那版一屏 27 行，
    按钮被挤出屏幕，他直接问「有做功能按钮吗」——长度本身是个功能。"""
    rows = [row(f"C{i}", closes=[100.0 + i - 10] + [100.0] * 14) for i in range(40)]
    txt = D.build_text(rows, 3, "all", "all", {"ok": 40})
    assert len(txt.splitlines()) <= 24, "卡片太长，按钮会被挤出屏幕"
    assert "👇" in txt, "还要明说下面有按钮"


def test_card_states_coverage_and_points_at_the_detail():
    rows = [row(f"C{i}", closes=[100.0 + i] + [100.0] * 14) for i in range(20)]
    txt = D.build_text(rows, 3, "all", "all", {"ok": 20})
    assert "三家" in txt and "永续+现货" in txt
    assert "ℹ️" in txt


def test_detail_card_carries_the_full_caliber():
    """细账收进按钮，但一条都不能少——口径、覆盖、剔了什么、为什么。"""
    rows = [row("A")]
    txt = D.build_detail(rows, 3, "all", "all", {
        "raw": 3400, "unique": 217, "fetched": 217, "ok": 217, "failed": 2,
        "thin": 2797, "stock": 62, "short": 1, "peg": 3, "skip_ok": True,
        "dead": [], "venues": 3, "markets": 2})
    for must in ("怎么算的", "现价", "UTC", "扫了什么", "剔掉了什么",
                 "3400", "217", "2797", "62", "稳定币", "杠杆代币",
                 "币安现货", "不构成投资建议"):
        assert must in txt, f"口径卡缺了：{must}"


def test_card_marks_cached_results():
    txt = D.build_text([row("A")], 3, "all", "all", {}, age=87)
    assert "87 秒前" in txt


# ── 参数 ────────────────────────────────────────────────────
@pytest.mark.parametrize("args,want", [
    ([], (3, "all", "all", True)),              # 默认只看热榜——群里的原话
    (["7"], (7, "all", "all", True)),
    (["3日"], (3, "all", "all", True)),          # 他就是这么说话的
    (["7天", "bybit"], (7, "bybit", "all", True)),
    (["币安"], (3, "binance", "all", True)),
    (["现货"], (3, "all", "spot", True)),
    (["3", "okx", "永续"], (3, "okx", "perp", True)),
    (["全部"], (3, "all", "all", False)),
    (["7", "全部"], (7, "all", "all", False)),
    (["999"], (14, "all", "all", True)),         # 夹到上限，标题里看得见
])
def test_parse_args(args, want):
    assert D.parse_args(args) == want


# ── 入口 ────────────────────────────────────────────────────
def test_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("rank"' in src and 'BotCommand("rank"' in src


def test_top_with_days_routes_here():
    import inspect
    from handlers import price
    assert "dayrank" in inspect.getsource(price.top)


def test_every_button_round_trips_through_the_dispatcher():
    """按钮的 callback_data 必须和分发器解析出来的位数对得上——
    差一位就是点了报错，而这种错只有真机才看得见。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert 'd.startswith("dr:")' in src
    kb = D.kb(3, "all", "all")
    for r in kb.inline_keyboard:
        for b in r:
            d = b.callback_data
            if not d.startswith("dr:"):
                continue
            bits = d.split(":")
            assert len(bits) == 6, f"{d} 位数不对"
            assert bits[1] in ("w", "r", "i")
            assert bits[3] in D.V_LABEL and bits[4] in D.M_LABEL
            assert bits[5] in ("hot", "full")
            int(bits[2])


def test_menu_entry_uses_the_same_shape():
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert "dr:w:3:all:all:hot" in src and "dr:w:7:all:all:hot" in src


def test_entry_is_two_taps_from_the_home_page():
    """埋三层等于没有入口：他连按钮做没做都没看见。
    /menu → 📊 行情 → 📅 3日涨跌榜，两下够得着。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "cat_market":')[1].split("elif d ==")[0]
    assert "dr:w:3:all:all:hot" in seg, "行情面板里应该直接有 3 日榜"
    assert "cat_market" in [b.callback_data
                            for r in menu.main_menu_kb().inline_keyboard for b in r]


def test_keyboard_offers_every_venue_and_market():
    """他要的就是"币安 bybit okx 都要、永续和现货都要"，
    这几个入口一个都不能少。"""
    labels = [b.text for r in D.kb(3, "all", "all").inline_keyboard for b in r]
    blob = " ".join(labels)
    for must in ("Bybit", "币安", "OKX", "永续", "现货", "3日", "7日", "14日",
                 "热榜", "全部币"):
        assert must in blob, f"按钮里缺了：{must}"


def test_command_is_categorised_in_the_panel():
    from handlers import cmdpanel
    assert cmdpanel.MODULE_CN.get("handlers.dayrank")


def test_heavy_scan_is_gated():
    import inspect
    assert "busy.guard" in inspect.getsource(D.rank_cmd)
