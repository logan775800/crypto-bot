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


# ── 缓存（首版这里有个让结果自相矛盾的 bug）─────────────────
def test_cache_is_reused_even_when_some_symbols_are_unknown():
    """首版的命中条件是"所有代号都在表里"——而交易所上千个基名里总有一批
    CoinGecko 没收录，条件永远为假，缓存等于不存在：每次扫描重拉十几页 + 回查
    上千个代号 → 必被限频 → 300万档扫出 0 个，500万档却列着 200 万市值的币。
    """
    calls = []

    async def fake_top():
        calls.append("top")
        return {"AAA": _row(1_000_000)}, MC.PAGES

    async def fake_lookup(syms):
        calls.append(("lookup", tuple(syms)))
        return {}

    orig_top, orig_lookup = MC._top_table, MC._lookup
    MC._top_table, MC._lookup = fake_top, fake_lookup
    MC._mcap_cache.update(at=0.0, table=None, pages=0, tried=set())
    try:
        syms = ["AAA", "NOPE"]                 # NOPE 永远查不到
        asyncio.run(MC.mcap_table(syms))
        asyncio.run(MC.mcap_table(syms))
        asyncio.run(MC.mcap_table(syms))
    finally:
        MC._top_table, MC._lookup = orig_top, orig_lookup
        MC._mcap_cache.update(at=0.0, table=None, pages=0, tried=set())

    assert calls.count("top") == 1, f"市值榜只该翻一次，实际 {calls.count('top')} 次"
    lookups = [c for c in calls if c != "top"]
    assert len(lookups) == 1, "查不到的代号不该每轮都重查一遍"


def test_a_failed_page_does_not_abort_the_rest():
    """某一页被限频就 break 的话，后面十几页一起丢——而微市值恰恰全在后面。
    首版 300万档扫出 0 个就是这么来的：只拿到前 5 页（排名 1250 以内）。"""
    got = []

    async def fake_get(path, params):
        page = params.get("page")
        got.append(page)
        if page in (3, 4):
            raise RuntimeError("429")
        return [{"symbol": f"C{page}", "market_cap": 9_000_000 - page}]

    import api
    orig_get, orig_wait, orig_gap = api._get, MC.PAGE_RETRY_WAIT, MC.PAGE_GAP
    api._get, MC.PAGE_RETRY_WAIT, MC.PAGE_GAP = fake_get, 0, 0
    try:
        table, pages = asyncio.run(MC._top_table())
    finally:
        api._get, MC.PAGE_RETRY_WAIT, MC.PAGE_GAP = orig_get, orig_wait, orig_gap

    assert "C5" in table and "C16" in table, "第 3、4 页失败不该让后面的页停下"
    assert pages == MC.PAGES - 2


def test_paging_stops_at_the_dust_band():
    """翻到没人上交易所的尘埃段就停，别把配额烧在没用的页上。"""
    async def fake_get(path, params):
        page = params.get("page")
        cap = 100 if page >= 3 else 9_000_000
        return [{"symbol": f"C{page}", "market_cap": cap}]

    import api
    orig, gap = api._get, MC.PAGE_GAP
    api._get, MC.PAGE_GAP = fake_get, 0
    try:
        _table, pages = asyncio.run(MC._top_table())
    finally:
        api._get, MC.PAGE_GAP = orig, gap
    assert pages == 3


def test_prebuild_is_scheduled():
    """市值表必须后台建。在命令里现拉会被限频截断，缺的正好是最小市值那几页。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert "microcap.prebuild" in src


# ── 分档 ────────────────────────────────────────────────────
def test_band_line_shows_each_bucket():
    """按成交额排序时大的那档会占满显示区，他会以为小档是空的。"""
    hits = [{"mcap": 2_000_000}, {"mcap": 2_900_000}, {"mcap": 4_000_000},
            {"mcap": 9_000_000}]
    line = MC.band_line(hits, 10_000_000)
    assert "300万以下 2 个" in line
    assert "300~500万 1 个" in line
    assert "500~1000万 1 个" in line


def test_band_line_stops_at_the_current_cap():
    line = MC.band_line([{"mcap": 2_000_000}], 3_000_000)
    assert "300万以下 1 个" in line and "300~500万" not in line


def test_keyboard_marks_current_and_refresh_keeps_it():
    """点了 1000 万再点刷新，不该莫名其妙跳回 300 万。"""
    rows = MC.kb(10_000_000).inline_keyboard
    marked = [b.text for row in rows for b in row if b.text.startswith("✅")]
    assert marked == ["✅ 1000万"]
    refresh = [b for row in rows for b in row if "刷新" in b.text][0]
    assert refresh.callback_data == "mc:1000"


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


def test_onchain_has_a_two_letter_shortcut():
    """链上查币价用得最频繁，点四五下按钮进去太慢——/oc BANK 一步到位。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("oc", onchain.onchain_cmd)' in src
    assert 'BotCommand("oc"' in src
