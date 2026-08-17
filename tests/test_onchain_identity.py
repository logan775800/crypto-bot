"""链上代币的身份、价格来源、K线异常标记。

三条要求对应三种"看起来有数、实际不能用"的情况：
  1. 只认名字 → 同名假币；只认合约地址 → 还是说不清报的是哪个池的价
     （实测「牛来」同一条链上 12 个池子）；
  2. 不说价格来自哪个池、什么时候取的、别的池差多少 → 给的是一个"不存在的价"；
  3. 链上池子浅，一笔单就能戳出一根插针，那个价**成交不到量**——
     照着影线设止损或判突破，等于按一个不存在的价格做决策。
"""
import time

import pytest

from handlers import onchain as OC


def pool(price, liq, addr="0xp", dex="pancakeswap", chain="bsc", vol=100_000):
    return {"price": price, "liq": liq, "pool": addr, "dex": dex,
            "chain_key": chain, "vol24": vol}


# ── 1) 唯一身份 = 链 + 合约地址 + 池地址 ─────────────────────────
def test_identity_includes_all_three():
    t = {"chain_key": "bsc", "address": "0xtoken", "pool": "0xpool"}
    assert OC.token_id(t) == "bsc:0xtoken:0xpool"


def test_same_token_different_pools_are_different_identities():
    """同一个代币的两个池子价格和深度都不一样，不能当成一回事。"""
    a = {"chain_key": "bsc", "address": "0xtoken", "pool": "0xp1"}
    b = {"chain_key": "bsc", "address": "0xtoken", "pool": "0xp2"}
    assert OC.token_id(a) != OC.token_id(b)


def test_same_symbol_different_chains_are_different_identities():
    a = {"chain_key": "bsc", "address": "0xa", "pool": "0xp"}
    b = {"chain_key": "eth", "address": "0xa", "pool": "0xp"}
    assert OC.token_id(a) != OC.token_id(b)


# ── 2) 多池价格偏差 ──────────────────────────────────────────────
def test_spread_across_pools():
    dev, lo, hi, n = OC.price_spread([pool(1.00, 500_000), pool(1.05, 300_000)])
    assert n == 2 and lo == 1.00 and hi == 1.05
    assert dev == pytest.approx(5.0)


def test_shallow_pools_do_not_count_in_the_spread():
    """一个 3000 美元的池子价格偏到天上去也不说明什么，不该污染偏差。"""
    dev, _lo, _hi, n = OC.price_spread([pool(1.00, 500_000), pool(5.00, 3_000)])
    assert n == 1 and dev == 0.0


def test_single_pool_has_no_spread():
    dev, _lo, _hi, n = OC.price_spread([pool(1.0, 500_000)])
    assert (dev, n) == (0.0, 1)


def test_card_shows_where_the_price_came_from():
    t = {"symbol": "X", "name": "X", "chain_cn": "BNB链", "dex": "pancakeswap",
         "address": "0xtoken", "pool": "0xpool1234567890abcdef",
         "price": 1.0, "liq": 500_000, "vol24": 100_000, "chg24": 1.0, "chg1h": 0.1,
         "fdv": 0, "buys": 10, "sells": 9, "created_ms": 0, "chain_key": "bsc",
         "fetched_at": time.time(), "pool_count": 3,
         "pools": [pool(1.00, 500_000), pool(1.06, 300_000)]}
    txt = OC.render_token(t)
    assert "价格来源" in txt and "pancakeswap" in txt
    assert "取数时刻" in txt
    assert "多池偏差" in txt and "6.00%" in txt
    assert "正在被拉或被砸" in txt          # 偏差 ≥3% 要给出解读
    assert "0xtoken" in txt and "0xpool1234567890abcdef" in txt   # 身份三件套


def test_small_spread_is_not_alarmed():
    t = {"symbol": "X", "name": "", "chain_cn": "BNB链", "dex": "uni",
         "address": "0xa", "pool": "0xp", "price": 1.0, "liq": 500_000,
         "vol24": 100_000, "chg24": 1.0, "chg1h": 0.1, "fdv": 0, "buys": 10,
         "sells": 9, "created_ms": 0, "chain_key": "bsc", "fetched_at": time.time(),
         "pool_count": 2, "pools": [pool(1.00, 500_000), pool(1.004, 300_000)]}
    txt = OC.render_token(t)
    assert "多池偏差" in txt
    assert "正在被拉或被砸" not in txt


# ── 3) K线标记 ───────────────────────────────────────────────────
def bar(ts, o, h, lo, c, v=100.0):
    return [ts, o, h, lo, c, v]


def test_unclosed_last_bar_is_flagged():
    now = time.time() * 1000
    rows = [bar(now - 3_600_000 * 2, 1, 1.1, 0.9, 1.0),
            bar(now - 600_000, 1, 1.1, 0.9, 1.0)]     # 10 分钟前开的 1h 线还没走完
    m = OC.kline_marks(rows, "1h")
    assert m["unclosed"] is True
    assert any("还没收盘" in s for s in OC.marks_text(m, "1h"))


def test_closed_last_bar_is_not_flagged():
    now = time.time() * 1000
    rows = [bar(now - 3_600_000 * 3, 1, 1.1, 0.9, 1.0),
            bar(now - 3_600_000 * 2, 1, 1.1, 0.9, 1.0)]
    assert OC.kline_marks(rows, "1h")["unclosed"] is False


def test_spike_wick_is_detected():
    """一根上影 50% 而实体几乎为零的针，混在正常波动里要被挑出来。"""
    rows = [bar(i * 3_600_000, 1.0, 1.01, 0.99, 1.0) for i in range(30)]
    rows.append(bar(30 * 3_600_000, 1.0, 1.5, 0.99, 1.0))     # 插针
    m = OC.kline_marks(rows, "1h")
    assert len(m["wicks"]) == 1
    assert m["wicks"][0]["side"] == "上"
    assert m["wicks"][0]["price"] == 1.5


def test_normal_volatility_is_not_called_a_spike():
    """一个天天大涨大跌的币，不能整屏都是插针标记——那等于没标。"""
    rows = []
    for i in range(40):
        o = 1.0 + (i % 5) * 0.1
        rows.append(bar(i * 3_600_000, o, o * 1.15, o * 0.85, o * 1.05))
    m = OC.kline_marks(rows, "1h")
    assert len(m["wicks"]) <= 2, f"正常波动被标了 {len(m['wicks'])} 根"


def test_marks_are_capped():
    """实测「牛来」200 根 15m 里 48 根命中——标记一密就没人看了。"""
    rows = [bar(i * 900_000, 1.0, 3.0, 1.0, 1.0) for i in range(60)]
    m = OC.kline_marks(rows, "15m")
    assert len(m["wicks"]) <= OC.MAX_MARKS


def test_volume_spike_is_detected():
    rows = [bar(i * 3_600_000, 1, 1.01, 0.99, 1.0, v=100) for i in range(30)]
    rows.append(bar(30 * 3_600_000, 1, 1.01, 0.99, 1.0, v=900))
    m = OC.kline_marks(rows, "1h")
    assert len(m["vol_spikes"]) == 1
    assert m["vol_spikes"][0]["x"] == pytest.approx(9.0)


def test_quiet_chart_produces_no_noise():
    """没异常就别说话——每张图都挂三行警告，等于没有警告。"""
    now = time.time() * 1000
    rows = [bar(now - 3_600_000 * (40 - i), 1.0, 1.01, 0.99, 1.0)
            for i in range(38)]
    m = OC.kline_marks(rows, "1h")
    assert OC.marks_text(m, "1h") == []


def test_marks_text_explains_why_it_matters():
    rows = [bar(i * 3_600_000, 1.0, 1.01, 0.99, 1.0) for i in range(30)]
    rows.append(bar(30 * 3_600_000, 1.0, 1.6, 0.99, 1.0))
    txt = " ".join(OC.marks_text(OC.kline_marks(rows, "1h"), "1h"))
    assert "成交不到量" in txt        # 说清为什么这个价不能用


def test_empty_rows_do_not_crash():
    assert OC.kline_marks([], "1h")["unclosed"] is False
    assert OC.marks_text(OC.kline_marks([], "1h"), "1h") == []
