"""多日涨跌榜（/rank 3、/rank 7）。

群里有人问「来个 3 日跌涨幅榜」——机器人当时答不了：所有的榜都是 24h，
而 `/upstreak` 是"连续 N 天同向"，那是另一件事（3 天累计涨 30% 但中间回调过一天的币，
在连涨榜里根本不存在）。

这里守的三条，前两条都是真机第一次冒烟当场抓到的：
  1. **涨幅榜和跌幅榜按正负切开**，不是"前 15 + 后 15"——后者在币少时
     会把 -30% 的币列进涨幅榜；
  2. **1000PEPE 和 PEPE 是同一个币**，不合并就白占两个名额；
     但只合并纯面值前缀，绝不按"名字像"合（PUMP / PUMPFUN 必须分开）；
  3. **代币化美股要剔掉**（OKX 合约接口没有品类字段，SNDK 成交额还排得很前）。
"""
import pytest

from handlers import dayrank as D


def row(sym, ex="Bybit", closes=None, turnover=1e7):
    return {"sym": sym, "ex": ex, "turnover": turnover,
            "closes": closes if closes is not None else [110.0] + [100.0] * 14}


# ── 口径 ────────────────────────────────────────────────────
def test_pct_is_now_versus_the_close_n_days_ago():
    """终点用现价（今天那根未收盘 K 线的最新价），不是昨天的收盘价。"""
    r = row("A", closes=[130.0, 120.0, 110.0, 100.0, 90.0])
    assert D.pct(r, 3) == pytest.approx(30.0)     # 130 vs closes[3]=100
    assert D.pct(r, 1) == pytest.approx(8.3333, rel=1e-3)


def test_pct_is_none_when_there_are_not_enough_candles():
    """不够根数返回 None，不是 0——0 会假装这个币没涨没跌，混进榜里当中位数。"""
    r = row("NEW", closes=[200.0, 100.0])
    assert D.pct(r, 7) is None
    assert D.pct(r, 1) == pytest.approx(100.0)


def test_zero_base_does_not_explode():
    assert D.pct(row("Z", closes=[10.0, 0.0]), 1) is None


# ── 分组：按正负切，不是取头尾 ────────────────────────────────
def test_gainers_and_losers_never_overlap():
    """真机冒烟抓到的：币少时 -30% 的币会同时出现在涨幅榜第三名。"""
    rows = [row("UP", closes=[130.0] + [100.0] * 14),
            row("DOWN", closes=[70.0] + [100.0] * 14),
            row("FLATUP", closes=[101.0] + [100.0] * 14)]
    up, down, st = D.ranked(rows, 3)
    up_syms = {r["sym"] for _p, r in up}
    down_syms = {r["sym"] for _p, r in down}
    assert not (up_syms & down_syms), "同一个币不能同时在涨榜和跌榜"
    assert up_syms == {"UP", "FLATUP"} and down_syms == {"DOWN"}


def test_losers_are_sorted_worst_first():
    rows = [row("A", closes=[90.0] + [100.0] * 14),
            row("B", closes=[70.0] + [100.0] * 14)]
    _up, down, _st = D.ranked(rows, 3)
    assert [r["sym"] for _p, r in down] == ["B", "A"]


def test_all_red_market_reports_an_empty_gainers_group():
    """空的分组也要印出来：整段消失读起来像"根本没扫这一边"，
    而"这 3 天一个上涨的都没有"本身就是最重要的信息。"""
    rows = [row("A", closes=[99.0] + [100.0] * 14),
            row("B", closes=[97.0] + [100.0] * 14)]
    txt = D.build_text(rows, 3, "bybit", 200, {})
    assert "涨幅榜*（0）" in txt
    assert "没有一个币是涨的" in txt
    assert "涨得最少" not in txt          # 这边全跌，该提示的是"跌得最少"
    assert "跌得最少" in txt


def test_all_green_market_reports_an_empty_losers_group():
    rows = [row("A", closes=[130.0] + [100.0] * 14)]
    txt = D.build_text(rows, 3, "bybit", 200, {})
    assert "跌幅榜*（0）" in txt and "没有一个币是跌的" in txt


# ── 去重：面值前缀 ──────────────────────────────────────────
@pytest.mark.parametrize("raw,norm", [
    ("1000PEPE", "PEPE"), ("10000SATS", "SATS"), ("1MBABYDOGE", "BABYDOGE"),
    ("100000BABYDOGE", "BABYDOGE"),
])
def test_denomination_prefix_is_normalised(raw, norm):
    assert D.norm_base(raw) == norm


@pytest.mark.parametrize("sym", ["BTC", "1INCH", "100X", "PUMP", "PUMPFUN"])
def test_normalisation_does_not_over_merge(sym):
    """宁可少合一个，不能合错一个。1INCH 不是"1 × INCH"，
    PUMP 和 PUMPFUN 看着像一对，合错就是把两个币的行情算到一起。"""
    assert D.norm_base(sym) == sym


# ── 扫描：去重 + 剔美股（打桩，不联网）────────────────────────
def _stub_scan(monkeypatch, universe_rows, skip=frozenset()):
    async def _uni_bybit(client, min_turnover):
        return [(s, s, t) for s, _cl, t in universe_rows]

    async def _uni_okx(client, min_turnover):
        return []

    async def _kl(client, sem, ex, inst, base, turnover):
        for s, cl, t in universe_rows:
            if s == base:
                return {"sym": base, "ex": ex, "closes": cl, "turnover": t}
        return None

    async def _skip():
        return set(skip)

    monkeypatch.setattr(D, "_bybit_universe", _uni_bybit)
    monkeypatch.setattr(D, "_okx_universe", _uni_okx)
    monkeypatch.setattr(D, "_klines", _kl)
    import handlers.klines as K
    monkeypatch.setattr(K, "noncrypto_bases", _skip)


def test_scan_merges_denominated_duplicates_keeping_the_deeper_venue(monkeypatch):
    import asyncio
    cl = [130.0] + [100.0] * 14
    _stub_scan(monkeypatch, [("PEPE", cl, 1e6), ("1000PEPE", cl, 9e6)])
    rows, scanned, stats = asyncio.run(D.scan("bybit"))
    assert len(rows) == 1, "1000PEPE 和 PEPE 是同一个币"
    assert rows[0]["turnover"] == 9e6, "留成交额大的那家（流动性更有代表性）"
    assert stats["merged"] == 1


def test_scan_drops_tokenised_stocks(monkeypatch):
    import asyncio
    cl = [130.0] + [100.0] * 14
    _stub_scan(monkeypatch, [("BTC", cl, 1e9), ("SNDK", cl, 5e8)], skip={"SNDK"})
    rows, _scanned, stats = asyncio.run(D.scan("bybit"))
    assert [r["sym"] for r in rows] == ["BTC"]
    assert stats["stock"] == 1 and stats["skip_ok"] is True


def test_scan_says_so_when_the_category_table_is_unavailable(monkeypatch):
    """取不到品类表就如实说，别假装剔过——这类"静默降级"最容易骗人。"""
    import asyncio
    _stub_scan(monkeypatch, [("BTC", [130.0] + [100.0] * 14, 1e9)])
    rows, scanned, stats = asyncio.run(D.scan("bybit"))
    assert stats["skip_ok"] is False
    assert "代币化美股可能混在榜里" in D.build_text(rows, 3, "bybit", scanned, stats)


# ── 卡片：口径和覆盖范围必须写在脸上 ─────────────────────────
def test_card_states_its_caliber_and_coverage():
    """一张 15 行的名单不写口径，看起来就像"全市场就这些"。"""
    rows = [row(f"C{i}", closes=[100.0 + i] + [100.0] * 14) for i in range(20)]
    txt = D.build_text(rows, 3, "all", 300, {"short": 2, "stock": 3, "merged": 4,
                                             "skip_ok": True})
    assert "口径：现价 vs 3 天前的日线收盘" in txt
    assert "覆盖：" in txt and "永续" in txt
    assert "不含现货" in txt
    assert "代币化美股 3 个" in txt and "跨所/面值重复 4 个" in txt
    assert "日线不够" in txt


def test_card_reports_what_it_truncated():
    rows = [row(f"C{i}", closes=[100.0 + i] + [100.0] * 14) for i in range(1, 25)]
    txt = D.build_text(rows, 3, "all", 300, {})
    assert "还有 9 个没显示" in txt      # 24 个全是涨的，只列 15


def test_card_marks_cached_results():
    """读的是几分钟前那批数据时必须说出来，否则他以为是刚扫的。"""
    txt = D.build_text([row("A")], 3, "all", 100, {}, age=87)
    assert "87 秒前" in txt


# ── 参数 ────────────────────────────────────────────────────
@pytest.mark.parametrize("args,want", [
    ([], (3, "all")),
    (["7"], (7, "all")),
    (["3日"], (3, "all")),          # 他就是这么说话的
    (["7天", "bybit"], (7, "bybit")),
    (["okx"], (3, "okx")),
    (["999"], (14, "all")),         # 夹到上限，标题里看得见
])
def test_parse_args(args, want):
    assert D.parse_args(args) == want


# ── 入口 ────────────────────────────────────────────────────
def test_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("rank"' in src
    assert 'BotCommand("rank"' in src


def test_top_with_days_routes_here():
    """他要的时候就打 `/top 3`，与其让他记第二个命令不如转过去。"""
    import inspect
    from handlers import price
    src = inspect.getsource(price.top)
    assert "dayrank" in src


def test_menu_has_buttons():
    """功能必须有按钮入口，不能只有命令。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert "dr:w:3:all" in src and "dr:w:7:all" in src
    assert 'd.startswith("dr:")' in src


def test_command_is_categorised_in_the_panel():
    """没登记分类就会沉到「其他」——那条护栏允许 3 个，不会替你拦住。"""
    from handlers import cmdpanel
    assert cmdpanel.MODULE_CN.get("handlers.dayrank")


def test_heavy_scan_is_gated():
    """全市场扫日线是重活，同一个人点两次会变成两个任务互相抢限流。"""
    import inspect
    src = inspect.getsource(D.rank_cmd)
    assert "busy.guard" in src
