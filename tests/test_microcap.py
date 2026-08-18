"""微市值扫描的筛选口径。

这个扫描最容易犯的错是**把"查不到"当成"市值很小"**——查不到市值的币在这一段
特别多，一旦算成命中，他会照着一个根本没数据的币去下单。
第二个是**同名误判**：真 SUN 几亿市值，同名垃圾 SUN 三万美元，挑错了就会把大币
报成微盘。两条都在这里守着。
"""
import asyncio

import pytest

from handlers import microcap as MC


def _row(mcap, rank=1800, fdv=0, circ=0, total=0):
    return {"market_cap": mcap, "market_cap_rank": rank,
            "fully_diluted_valuation": fdv,
            "circulating_supply": circ, "total_supply": total}


def _tr(turnover=500_000, crypto=True, venues=(("gate", "spot"),)):
    return {"venues": list(venues), "turnover": turnover, "change": 2.0,
            "price": 0.01, "crypto": crypto, "best": ("Gate现货", turnover)}


def _run(tradable, table, **kw):
    async def fake_tradable():
        return tradable

    async def fake_table(symbols):
        return table, MC.PAGES

    orig_t, orig_m = MC.tradable, MC.mcap_table
    MC.tradable, MC.mcap_table = fake_tradable, fake_table
    try:
        return asyncio.run(MC.run(**kw))
    finally:
        MC.tradable, MC.mcap_table = orig_t, orig_m


# ── 筛选口径 ────────────────────────────────────────────────
def test_under_threshold_is_a_hit():
    hits, stats = _run({"SWELL": _tr()}, {"SWELL": _row(2_900_000)})
    assert [h["symbol"] for h in hits] == ["SWELL"]
    assert hits[0]["mcap"] == 2_900_000


def test_over_threshold_is_not():
    hits, _ = _run({"BTC": _tr()}, {"BTC": _row(2_000_000_000_000)})
    assert hits == []


def test_missing_market_cap_is_never_a_hit():
    """查不到 ≠ 市值小。这类币要单独计数报出来，不能混进结果。"""
    hits, stats = _run({"WHO": _tr()}, {})
    assert hits == [] and stats["no_data"] == 1

    hits, stats = _run({"ZERO": _tr()}, {"ZERO": _row(0)})
    assert hits == [] and stats["no_data"] == 1


def test_dust_volume_is_filtered():
    """市值够小但一天成交几千美元——列出来也下不了单。"""
    hits, stats = _run({"DUST": _tr(turnover=7_429)}, {"DUST": _row(2_983_658)})
    assert hits == [] and stats["thin"] == 1


def test_tokenized_stocks_are_excluded():
    hits, stats = _run({"SNDK": _tr(crypto=False)}, {"SNDK": _row(1_000_000)})
    assert hits == [] and stats["noncrypto"] == 1


def test_threshold_is_adjustable():
    tr, tb = {"X": _tr()}, {"X": _row(8_000_000)}
    assert _run(tr, tb)[0] == []                                  # 默认 300 万
    assert [h["symbol"] for h in _run(tr, tb, max_mcap=10_000_000)[0]] == ["X"]


def test_sorted_by_turnover_not_by_marketcap():
    """先看能不能下单，再谈便宜——和 /scan 一个道理。"""
    hits, _ = _run(
        {"THIN": _tr(turnover=150_000), "LIQUID": _tr(turnover=2_000_000)},
        {"THIN": _row(500_000), "LIQUID": _row(2_900_000)})
    assert [h["symbol"] for h in hits] == ["LIQUID", "THIN"]


# ── 同名误判 ────────────────────────────────────────────────
def test_duplicate_symbol_keeps_the_biggest():
    """市值榜按降序翻页，同代号必须留先出现（市值最高）的那个，
    否则一个几亿市值的真币会被同名垃圾币拖进"微盘"名单。"""
    import inspect
    src = inspect.getsource(MC._top_table)
    assert "not in table" in src, "翻页时必须只写入第一次出现的代号"

    src2 = inspect.getsource(MC._lookup)
    assert "mc > (prev" in src2, "榜外查询同样要取市值最高的候选"


def test_tokenized_stock_is_caught_by_coingecko_name():
    """交易所接口那层认不出只在币安**现货**上市的代币化美股：
    首次真跑（2026-08-18）微市值榜前十有一半是 QQQB/SOXSB/MRVLB 这种 ETF。
    CoinGecko 的名字里明写着 bStocks Tokenized Stock，按它认。"""
    hits, stats = _run(
        {"QQQB": _tr()},
        {"QQQB": dict(_row(1_349_703), name="Invesco QQQ Trust (bStocks Tokenized Stock)",
                      id="invesco-qqq-trust-bstocks-tokenized-stock")})
    assert hits == [] and stats["noncrypto"] == 1


def test_volume_far_above_marketcap_means_wrong_coin():
    """UTK：币安现货那个 UTK 配到了 CoinGecko 的「UNITE THE KINGDOM」，
    市值 $11,386 却一天成交 $1020 万——896 倍。这不是宝藏，是配错币了。"""
    hits, stats = _run({"UTK": _tr(turnover=10_207_949)},
                       {"UTK": dict(_row(11_386), name="UNITE THE KINGDOM",
                                    id="unite-the-kingdom")})
    assert hits == [] and stats["mismatched"] == 1


def test_normal_microcap_ratio_is_kept():
    """真微盘的量/市值实测在 1~2 倍，别把正常的也误杀。"""
    hits, _ = _run({"XTER": _tr(turnover=2_982_967)}, {"XTER": _row(1_538_721)})
    assert [h["symbol"] for h in hits] == ["XTER"]


def test_partial_mcap_pages_are_disclosed():
    """市值榜缺页 → 一批真币掉进按代号回查的分支，同名可能配错。
    这种时候必须在卡片上说出来，不能装作数据是全的。"""
    out = MC.render([], {"universe": 900, "no_data": 40, "thin": 12,
                         "noncrypto": 8, "mismatched": 3, "pages": 3})
    assert "3/" in out and "限频" in out

    full = MC.render([], {"universe": 900, "no_data": 40, "thin": 12,
                          "noncrypto": 8, "mismatched": 3, "pages": MC.PAGES})
    assert "限频" not in full


# ── 展示 ────────────────────────────────────────────────────
def test_low_float_is_warned():
    """他选的口径是流通市值，但低流通高 FDV 解锁就是砸盘——必须标出来。"""
    hits, stats = _run({"UNLOCK": _tr()},
                       {"UNLOCK": _row(2_500_000, fdv=50_000_000,
                                       circ=5_000_000, total=100_000_000)})
    out = MC.render(hits, stats)
    assert "FDV" in out and "流通 5%" in out and "解锁砸盘风险" in out


def test_healthy_float_is_not_warned():
    hits, stats = _run({"FAIR": _tr()},
                       {"FAIR": _row(2_500_000, fdv=2_600_000,
                                     circ=95_000_000, total=100_000_000)})
    out = MC.render(hits, stats)
    assert "解锁砸盘风险" not in out


def test_empty_result_explains_itself():
    """一个都没有是常态（上市本身就是门槛），不能只回一句「没结果」让他以为坏了。"""
    out = MC.render([], {"universe": 900, "no_data": 40, "thin": 12, "noncrypto": 8})
    assert "一个都没有" in out and "/microcap 1000" in out


def test_render_shows_where_to_buy():
    hits, stats = _run({"OBT": _tr(venues=(("gate", "spot"), ("bybit", "swap")))},
                       {"OBT": _row(2_951_059)})
    out = MC.render(hits, stats)
    assert "Gate现货" in out and "Bybit永续" in out


def test_render_reports_what_was_filtered_out():
    """筛掉了多少、为什么筛掉，必须写在卡片上——否则他不知道这是全部还是被截了。"""
    out = MC.render([], {"universe": 900, "no_data": 40, "thin": 12, "noncrypto": 8})
    assert "900" in out and "40" in out and "12" in out


# ── 入口 ────────────────────────────────────────────────────
def test_button_entry_exists():
    """功能必须有按钮入口，不能只有命令。"""
    from handlers import menu
    _text, rows = menu.CATS["cat_scan"]
    cbs = [b.callback_data for row in rows for b in row if b.callback_data]
    assert any(c.startswith("mc:") for c in cbs)


def test_button_is_routed():
    import inspect
    from handlers import menu
    assert 'd.startswith("mc:")' in inspect.getsource(menu._dispatch)


def test_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("microcap"' in src
    assert 'BotCommand("microcap"' in src
