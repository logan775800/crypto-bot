"""清算地图跨所聚合：一家的持仓量不是全网的持仓量。

起因是他拿另一份 BTR 分析来对。那份只够到 KuCoin + MEXC
（浏览器 IP 被币安 451 / Bybit 403 挡了），于是拿**全网 21% 的持仓量**
下了全网口径的结论——报的「全网 745.7M ≈ $160M」比实测的
506.1M ≈ $85.3M 高了约 47%，算出的持仓方向也和加上币安+Bybit 之后相反。
"""
import pytest

from handlers import liqmap as L


def rows(vals, t0=1_700_000_000_000, step=3_600_000):
    return [{"timestamp": t0 + i * step, "sumOpenInterestValue": str(v)}
            for i, v in enumerate(vals)]


# ── 合成 ────────────────────────────────────────────────────
def test_rows_to_map_skips_garbage():
    m = L._rows_to_map([{"timestamp": 1, "sumOpenInterestValue": "10"},
                        {"timestamp": 2, "sumOpenInterestValue": None},
                        {"nope": 1}])
    assert m == {1: 10.0}


def test_coverage_line_names_what_is_missing():
    """覆盖 21% 和覆盖 79% 画出来的图长得一模一样，不写没人分得清。
    而且**漏掉的是哪几家、各占多少**也要报——只说"79%"看不出漏了谁。"""
    share = {"币安": 54.0, "Bybit": 18.0, "KuCoin": 14.0, "MEXC": 7.0, "Gate": 6.0}
    t = L.coverage_line(share, ["币安", "Gate", "Bybit"])
    assert "79%" in t
    assert "KuCoin 14%" in t and "MEXC 7%" in t


def test_coverage_line_is_quiet_without_data():
    assert L.coverage_line(None, ["币安"]) == ""
    assert L.coverage_line({}, ["币安"]) == ""
    assert L.coverage_line({"币安": 0.0}, ["币安"]) == ""


def test_full_coverage_says_100():
    t = L.coverage_line({"币安": 90.0, "Gate": 10.0}, ["币安", "Gate"])
    assert "100%" in t
    assert "未计入" not in t


def test_caption_prints_coverage():
    m = L.build_map(rows([100.0, 200.0, 300.0]),
                    [[1_700_000_000_000 + i * 3_600_000, "1", "1.1", "0.9", "1"]
                     for i in range(3)], 1.0)
    cap = L.caption(m, "BTR", "7日", 1.0, "币安+Gate+Bybit", 7,
                    {"币安": 54.0, "Bybit": 18.0, "KuCoin": 14.0,
                     "MEXC": 7.0, "Gate": 6.0})
    assert "覆盖全网持仓的 79%" in cap
    assert "币安+Gate+Bybit永续" in cap


# ── 那条一直在误报的提示 ────────────────────────────────────
@pytest.mark.parametrize("win,days", [("1日", 1), ("7日", 7), ("30日", 30)])
def test_full_window_does_not_claim_the_coin_is_too_new(win, days):
    """**这是一直存在的 bug**：判据拿的是 `WINDOWS[win][1]`，
    那是**K 线根数**不是天数。7日窗口是 168 根 1 小时线，于是
    `7 < 168*0.95` 永远成立——**每一张 7 日图都在说「这个币上市时间不够」**，
    连 BTC 都不放过。只有 90/180/1年 那三个日线窗口碰巧根数==天数。
    """
    m = L.build_map(rows([100.0, 200.0]),
                    [[1_700_000_000_000 + i * 3_600_000, "1", "1.1", "0.9", "1"]
                     for i in range(2)], 1.0)
    cap = L.caption(m, "BTC", win, 1.0, "币安", days)
    assert "上市时间不够" not in cap, f"{win} 窗口对着满数据喊数据不够"


def test_a_genuinely_short_history_still_warns():
    """新上市的币点「1年」只能拿到几个月，这时候必须说——
    修上面那条不能把这条一起修没了。"""
    m = L.build_map(rows([100.0, 200.0]),
                    [[1_700_000_000_000 + i * 86_400_000, "1", "1.1", "0.9", "1"]
                     for i in range(2)], 1.0)
    cap = L.caption(m, "AKE", "1年", 1.0, "Bybit", 333)
    assert "上市时间不够" in cap and "333 天" in cap


def test_win_days_covers_every_window():
    """漏一个的话那个窗口的提示会静默失效（拿不到 want 就永远不提醒）。"""
    assert set(L.WIN_DAYS) == set(L.WINDOWS)


# ── 图上的来源 ──────────────────────────────────────────────
def test_chart_title_english_fallback_lists_all_sources():
    """图会被单独转发，脱离文字说明——所以来源必须画在图里。
    英文回退以前写死一家，多源之后会骗人说数据只来自 Binance。"""
    import inspect
    src = inspect.getsource(L.render)
    assert '"币安": "Binance"' in src
    assert '_en = "Bybit" if src ==' not in src, "英文回退还写死着一家"


# ── 聚合的两条硬规矩 ────────────────────────────────────────
def test_no_interpolation_when_a_venue_misses_a_bar():
    """**缺一根就跳过那一家，绝不插值。**插出来的值在图上会变成一段
    凭空的「新增持仓」，而新增持仓正是清算簇金额的来源——
    等于凭空造出一堆爆仓单。"""
    import inspect
    src = inspect.getsource(L._aggregate)
    assert "绝不插值" in src
    assert "m.get(ts) or 0.0" in src


def test_aggregate_falls_back_to_the_primary_when_others_are_empty():
    import asyncio

    class C:
        async def get(self, *a, **k):
            raise RuntimeError("挂了")
    r = rows([100.0, 200.0, 300.0])
    got, used = asyncio.new_event_loop().run_until_complete(
        L._aggregate(C(), "BTRUSDT", "BTR", "1h", 168, r, "币安"))
    assert got is r and used == ["币安"]


def test_kucoin_btc_alias():
    """KuCoin 把比特币叫 XBT。不转的话 BTC 上它整家取不到，
    而**取不到的那家会从分母里消失** → 覆盖率被算高。"""
    import inspect
    src = inspect.getsource(L.venue_share)
    assert '"XBT" if base == "BTC"' in src


def test_share_failure_never_kills_the_chart():
    """占比只是给卡片加一行。某一家接口抽风不能把整张图拖没。"""
    import inspect
    src = inspect.getsource(L.cached_share)
    assert "return None" in src or "share = None" in src


def test_get_return_arity_is_unchanged():
    """`_get` 的元组长度改过两次，每次都在别处安静地炸
    （合约告警的配图就这么消失了几个版本）。这次覆盖率**没有**塞进去，
    走的是单独的 cached_share。"""
    import inspect
    src = inspect.getsource(L._get)
    assert "data = (m, last, inst, src, days)" in src
