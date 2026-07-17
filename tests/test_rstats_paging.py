"""切片+翻页测试。Bybit 对 closed-pnl / execution 都有「单次窗口 ≤7 天」的硬限制，
切片写错的表现是「30天复盘只返回最近7天」——数字看着正常，人根本发现不了。
这里用假 client 把窗口和翻页行为钉死。
"""
import asyncio

from handlers import rstats


def _run(coro):
    """这些用例只是驱动 async 取数函数，不值得为此引入 pytest-asyncio 依赖。"""
    return asyncio.run(coro)


class FakeClient:
    """记录每次调用的窗口，按 cursor 分页返回。"""

    def __init__(self, pages_per_window=1, window_limit_ms=7 * 86400 * 1000):
        self.calls = []
        self.pages_per_window = pages_per_window
        self.window_limit_ms = window_limit_ms

    async def closed_pnl(self, start_ms=None, end_ms=None, cursor=None, **kw):
        span = end_ms - start_ms
        # 真实 Bybit 会直接报错，这里断言等价——切片写错就当场炸出来
        assert span <= self.window_limit_ms, f"窗口 {span}ms 超过 Bybit 的 7 天上限"
        self.calls.append((start_ms, end_ms, cursor))
        page = int(cursor or 0)
        rows = [{"orderId": f"{start_ms}-{page}-{i}", "updatedTime": str(start_ms + i),
                 "side": "Sell", "closedPnl": "1", "closedSize": "1"}
                for i in range(2)]
        nxt = str(page + 1) if page + 1 < self.pages_per_window else None
        return {"list": rows, "nextPageCursor": nxt}

    async def executions(self, start_ms=None, end_ms=None, cursor=None, **kw):
        assert end_ms - start_ms <= self.window_limit_ms
        self.calls.append((start_ms, end_ms, cursor))
        return {"list": [{"execId": f"e{start_ms}", "symbol": "BTCUSDT",
                          "side": "Buy", "execQty": "1", "execTime": str(start_ms),
                          "execType": "Trade"}], "nextPageCursor": None}


def test_30_days_is_split_into_windows_within_the_7_day_limit():
    c = FakeClient()
    _run(rstats.fetch_closed(c, 30))
    # 30 天 → 5 个切片（7+7+7+7+2）
    assert len(c.calls) == 5
    for start, end, _ in c.calls:
        assert end - start <= 7 * 86400 * 1000


def test_windows_are_contiguous_and_cover_the_whole_range():
    c = FakeClient()
    _run(rstats.fetch_closed(c, 30))
    wins = [(s, e) for s, e, cur in c.calls if cur is None]
    for i in range(len(wins) - 1):
        assert wins[i][1] == wins[i + 1][0], "切片之间不能有缝，否则会漏掉那段的交易"
    span = wins[-1][1] - wins[0][0]
    assert abs(span - 30 * 86400 * 1000) < 5000


def test_single_window_when_range_is_short():
    c = FakeClient()
    _run(rstats.fetch_closed(c, 3))
    assert len(c.calls) == 1


def test_cursor_paging_pulls_every_page():
    c = FakeClient(pages_per_window=3)
    rows = _run(rstats.fetch_closed(c, 7))
    assert len(c.calls) == 3                      # 3 页全翻到
    assert [cur for _, _, cur in c.calls] == [None, "1", "2"]
    assert len(rows) == 6                         # 每页 2 行


def test_paging_stops_at_max_pages_instead_of_looping_forever():
    class Endless(FakeClient):
        async def closed_pnl(self, start_ms=None, end_ms=None, cursor=None, **kw):
            self.calls.append((start_ms, end_ms, cursor))
            # 永远给下一页：模拟接口异常/游标不前进
            return {"list": [{"orderId": f"x{len(self.calls)}", "updatedTime": "1",
                              "side": "Sell", "closedPnl": "0", "closedSize": "1"}],
                    "nextPageCursor": "always"}

    c = Endless()
    _run(rstats.fetch_closed(c, 3))
    assert len(c.calls) == rstats.MAX_PAGES


def test_results_are_deduped_and_sorted_by_close_time():
    class Dup(FakeClient):
        async def closed_pnl(self, start_ms=None, end_ms=None, cursor=None, **kw):
            # 每个切片都返回同一笔 —— 边界重复拉到是真实会发生的
            return {"list": [
                {"orderId": "same", "updatedTime": "500", "side": "Sell",
                 "closedPnl": "1", "closedSize": "1"},
                {"orderId": "older", "updatedTime": "100", "side": "Sell",
                 "closedPnl": "1", "closedSize": "1"},
            ], "nextPageCursor": None}

    rows = _run(rstats.fetch_closed(Dup(), 30))
    assert len(rows) == 2, "同一笔跨切片重复出现时必须去重，否则盈亏会被重复计算"
    assert [r["updatedTime"] for r in rows] == ["100", "500"]


def test_executions_are_deduped_by_exec_id():
    rows = _run(rstats.fetch_execs(FakeClient(), 30))
    assert len({r["execId"] for r in rows}) == len(rows)


def test_empty_list_stops_paging_early():
    class Empty(FakeClient):
        async def closed_pnl(self, start_ms=None, end_ms=None, cursor=None, **kw):
            self.calls.append((start_ms, end_ms, cursor))
            # 有游标但没数据：不能因为游标非空就一直翻
            return {"list": [], "nextPageCursor": "next"}

    c = Empty()
    _run(rstats.fetch_closed(c, 3))
    assert len(c.calls) == 1
