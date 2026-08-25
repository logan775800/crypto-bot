"""急涨急跌扫描的币池：币安 + Bybit 取并集。

他问「很多数据源之前取 Bybit，可以全换币安吗」。取证：

    币安永续 701 个｜Bybit 732 个｜两家都有 619 个
    只有币安、且成交额≥500万：19 个（PROM 377M、PUMP 349M、FET 43M…）
    只有 Bybit、且成交额≥500万：6 个（MNT、AGI、CASHCAT、PUMPFUN…）

所以**二选一怎么选都固定看漏一批**。他选了「币安优先 + Bybit 兜底」，
对扫描类功能来说就是取并集、按币去重。
"""
import pytest

from handlers import pumpalert as P


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def client(binance=None, bybit=None, bn_err=None, by_err=None):
    class C:
        async def get(self, url, **k):
            if "binance" in url:
                if bn_err:
                    raise RuntimeError(bn_err)
                return FakeResp(binance or [])
            if by_err:
                raise RuntimeError(by_err)
            return FakeResp({"retCode": 0, "result": {"list": bybit or []}})
    return C()


def bn_row(sym, price, vol):
    return {"symbol": sym + "USDT", "lastPrice": str(price), "quoteVolume": str(vol)}


def by_row(sym, price, vol):
    return {"symbol": sym + "USDT", "lastPrice": str(price), "turnover24h": str(vol)}


def run(c):
    import asyncio
    return asyncio.run(c)


def test_universe_is_the_union_of_both():
    """只扫一家就固定看漏另一家独占的那批。"""
    perps = run(P._fetch_bybit_perps(client(
        binance=[bn_row("PROM", 10, 9e8), bn_row("BTC", 80000, 9e9)],
        bybit=[by_row("MNT", 1, 9e7), by_row("BTC", 80001, 5e9)])))
    assert {m["sym"] for m in perps} == {"PROM", "BTC", "MNT"}


def test_shared_coin_keeps_the_deeper_venue():
    """两家都有时留成交额大的那家——流动性更好、报价更有代表性。"""
    perps = run(P._fetch_bybit_perps(client(
        binance=[bn_row("BTC", 80000, 9e9)],
        bybit=[by_row("BTC", 80001, 5e9)])))
    assert len(perps) == 1
    assert perps[0]["price"] == 80000 and perps[0]["turnover"] == 9e9


def test_one_venue_down_does_not_kill_the_scan():
    """急涨急跌是每 60 秒的实时告警，不能因为一家抽风就整条哑掉。"""
    perps = run(P._fetch_bybit_perps(client(
        bybit=[by_row("MNT", 1, 9e7)], bn_err="币安超时")))
    assert [m["sym"] for m in perps] == ["MNT"]

    perps = run(P._fetch_bybit_perps(client(
        binance=[bn_row("PROM", 10, 9e8)], by_err="Bybit 超时")))
    assert [m["sym"] for m in perps] == ["PROM"]


def test_both_down_raises():
    with pytest.raises(RuntimeError):
        run(P._fetch_bybit_perps(client(bn_err="x", by_err="y")))


def test_delivery_contracts_are_excluded():
    """币安的 BTCUSDT_240329 是交割合约不是永续，混进来会让榜单出现重复的币。"""
    perps = run(P._fetch_bybit_perps(client(
        binance=[bn_row("BTC", 80000, 9e9),
                 {"symbol": "BTCUSDT_240329", "lastPrice": "80500",
                  "quoteVolume": "9e9"}])))
    assert [m["sym"] for m in perps] == ["BTC"]


def test_turnover_floor_applies_to_both():
    """两家都取到了、只是没有币过门槛 —— 这**不是故障**，不能抛异常。

    按"结果为空"判故障的话，「没有合格的币」会被当成「取不到数据」报出去，
    日志里的错误从此就不能信了。只有两家都出错才算取数失败。
    """
    perps = run(P._fetch_bybit_perps(client(
        binance=[bn_row("DEAD", 1, 100)], bybit=[by_row("ALSODEAD", 1, 100)])))
    assert perps == []
