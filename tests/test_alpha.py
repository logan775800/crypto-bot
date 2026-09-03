"""币安上币候选池：这不是预知上币，是官方候选池 + 变动监控。

他问「我可以提前知道币安哪些链上的币要上交易所吗」。实测（2026-09-02）：
Binance Alpha 池里 666 个代币，**92 个已经毕业到币安现货**，基础概率约 16%。
所以这个功能给的是「把几百个缩到十几个值得盯的」，不是「这个会上」——
这句话必须一直印在卡片上，否则它会被当成内幕消息用。
"""
import pytest

from handlers import alpha as A


@pytest.fixture(autouse=True)
def clean():
    import storage
    storage.data["alpha"] = {}
    A._cache.update({"ts": 0, "rows": None})
    yield


def tok(sym, mcap=1e7, liq=1e5, stock=False, off=False, chain="BSC", tid=None):
    return {"symbol": sym, "marketCap": str(mcap), "liquidity": str(liq),
            "chainName": chain, "stockState": stock, "offline": off,
            "tokenId": tid or f"id-{sym}", "holders": "100",
            "contractAddress": f"0x{sym.lower()}", "percentChange24h": "1.0"}


# ── 筛选 ────────────────────────────────────────────────────
def test_already_listed_on_binance_spot_is_not_a_candidate():
    """已经上了就不叫候选。"""
    rows = [tok("AAA"), tok("BBB")]
    assert [A.sym_of(x) for x in A.candidates(rows, {"AAA"})] == ["BBB"]


def test_tokenized_stocks_are_excluded():
    """池子里有 80 个 Ondo 的代币化美股（XOMon / TSLAon / COINon…）。
    它们市值大，不剔会**霸占榜首**——而它们根本不是「等着上币安现货」
    的东西。同一个坑第五次了（微市值榜、涨跌榜、多空比榜、爆仓一边倒）。"""
    rows = [tok("TSLAon", mcap=1e9, stock=True), tok("REAL", mcap=1e6)]
    got = [A.sym_of(x) for x in A.candidates(rows, set())]
    assert got == ["REAL"], "代币化美股混进榜单了"


def test_stock_detector_accepts_either_field():
    """实测 stockState 和 rwaInfo 圈出的是同一批 80 个（交集 80、各自独有 0）。
    两个都判是为了将来某一个被改掉。"""
    assert A.is_stock({"stockState": True})
    assert A.is_stock({"rwaInfo": {"x": 1}})
    assert not A.is_stock({"stockState": False, "rwaInfo": None})


def test_offline_tokens_are_excluded():
    rows = [tok("DEAD", off=True), tok("LIVE")]
    assert [A.sym_of(x) for x in A.candidates(rows, set())] == ["LIVE"]


def test_ranked_by_market_cap_not_liquidity():
    """实测已毕业 vs 没毕业的中位数：市值 10.6x、流动性只有 1.9x、
    持币数 0.8x（**反的**）、币安自己的 score 1.0x（完全没用）。
    只有市值能用——凭直觉会拿持币数排，那是错的。"""
    rows = [tok("SMALL", mcap=1e6, liq=1e9), tok("BIG", mcap=1e9, liq=1)]
    assert [A.sym_of(x) for x in A.candidates(rows, set())] == ["BIG", "SMALL"]


# ── 首轮不能刷屏 ────────────────────────────────────────────
def test_first_run_only_builds_the_baseline():
    """第一次跑时整个池子都是"新"的，不挡的话一次推 600 条。
    各处告警都踩过这个坑。"""
    rows = [tok(f"T{i}") for i in range(50)]
    assert A.diff_new(rows) == []
    assert len(A._cfg()["seen"]) == 50


def test_second_run_reports_only_genuinely_new_ones():
    rows = [tok("A"), tok("B")]
    A.diff_new(rows)
    fresh = A.diff_new(rows + [tok("C")])
    assert [A.sym_of(x) for x in fresh] == ["C"]


def test_seen_list_is_capped():
    """新币是无限供应的，不封顶 data.json 会一直长。"""
    A.diff_new([tok(f"T{i}", tid=f"x{i}") for i in range(3200)])
    assert len(A._cfg()["seen"]) <= 3000


# ── 口径不能说过头 ──────────────────────────────────────────
def test_card_always_says_it_is_not_a_prediction():
    """基础概率就 16%。不写这句，它会被当成内幕消息用。"""
    rows = [tok("A", mcap=1e9), tok("B")]
    t = A.build_text(rows, {"B"})
    assert "不是预知上币" in t
    assert "16%" in t or "50%" in t   # 算出来的，不是写死的


def test_base_rate_is_computed_not_hardcoded():
    """第一版卡片上算的是 16%，而新进池那条卡片写死了 14%，
    两个数当场打架。"""
    rows = [tok("A"), tok("B"), tok("C"), tok("D")]
    assert A.base_rate(rows, {"A"}) == pytest.approx(25.0)
    assert A.base_rate([], set()) == 0.0
    import inspect
    assert "rate:.0f" in inspect.getsource(A.format_new), "新进池卡片又写死了"


def test_base_rate_ignores_tokenized_stocks_on_both_sides():
    """分子分母都得剔，不然基础概率会被 80 个代币化美股稀释。"""
    rows = [tok("A"), tok("B"), tok("STKon", stock=True)]
    assert A.base_rate(rows, {"A"}) == pytest.approx(50.0)


def test_detail_records_the_three_dead_ends():
    """listingCex / PENDING_TRADING / 公告接口——三个验过之后靠不住的。
    不写下来，下一个人（包括我）会再验一遍，或者更糟：拿它们当判据。"""
    t = A.detail_text()
    assert "listingCex" in t and "已上某个 CEX" in t
    assert "PENDING_TRADING" in t and "287 天前" in t
    assert "WAF" in t


def test_card_fits_the_line_budget():
    """超过 24 行按钮会被挤出屏幕。第一版把对比表印在卡上，直接 32 行。"""
    rows = [tok(f"T{i}", mcap=1e9 - i) for i in range(40)]
    n = len(A.build_text(rows, set()).splitlines())
    assert n <= 22, f"卡片 {n} 行，细节该收进 ℹ️"


# ── 接线 ────────────────────────────────────────────────────
def test_toggle():
    assert not A.is_on(-100)
    A.toggle(-100, True)
    assert A.is_on(-100)
    A.toggle(-100, False)
    assert not A.is_on(-100)


def test_quota_does_not_reset_itself_after_counting():
    """hot / 梗爆发 / 爆仓一边倒都踩过的同一个坑。"""
    A._used(A.NEW_PER_HOUR)
    assert A.quota_left() == 0


def test_small_new_entries_are_not_pushed():
    """太小的新进池是常态不是信号，不设闸每天几十条。"""
    import inspect
    assert "MIN_MCAP" in inspect.getsource(A.scan)


def test_command_button_and_job_registered():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    src = (root / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("alpha", alpha.alpha_cmd)' in src
    assert "alpha.scan" in src and 'BotCommand("alpha"' in src
    from handlers import menu
    cbs = [b.callback_data for row in menu.notify_kb(-100).inline_keyboard
           for b in row]
    assert "al:r" in cbs
    import inspect
    assert 'd.startswith("al:")' in inspect.getsource(menu._dispatch)
