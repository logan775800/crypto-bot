"""监控链上代币。

关键在于**复用**：链上不是"交易所×市场"，是"链×合约地址"，但监控/预警的存储和
轮询全都按 (标的, 数据源标签) 组织。所以把链上表达成一个标签「链上BNB链」、
标的存合约地址，整条链路（落盘/轮询/冷却/重设基准/取消）一行都不用改。

两个必须守住的点：
  1. **合约地址不能过 norm_symbol** —— 那会 .upper() 把 Solana 的 base58 改坏
     （地址大小写敏感），也会把结尾像 USDT 的地址截断；
  2. 轮询必须回到同一个源，链上的"源"就是那条链。
"""
import asyncio

import pytest

import storage
from handlers import onchain as OC
from handlers import source as S
from handlers import watchpct as W


@pytest.fixture(autouse=True)
def _clean():
    for k in ("watchpct", "alerts", "user_prefs"):
        storage.data[k] = {} if k == "user_prefs" else []
    yield
    for k in ("watchpct", "alerts", "user_prefs"):
        storage.data[k] = {} if k == "user_prefs" else []


EVM = "0xBEEA1D618e533a387D941F58a7d4c9b7bD377777"
SOL = "djN5QdTLZGoCNwa2Q2BqKNKaZHoKn4J6BSZzWxVpump"


def token(addr, chain="bsc", price=0.04, liq=990_000, sym="牛来"):
    return {"symbol": sym, "name": sym, "address": addr, "price": price,
            "liq": liq, "chain_key": chain, "chain": chain,
            "chain_cn": OC.CHAINS[chain]["cn"], "pool": "0xpool", "vol24": 25_000_000,
            "chg24": 100.0, "chg1h": 1.0, "fdv": 40_000_000, "buys": 300,
            "sells": 250, "created_ms": 0, "dex": "pancakeswap", "url": ""}


@pytest.fixture
def chain_prices(monkeypatch):
    """{地址: (价格, 代币)}，模拟链上查询。"""
    def apply(mapping):
        async def fake(addr):
            hit = mapping.get(addr)
            return hit if hit else (None, None)
        monkeypatch.setattr(OC, "price_of", fake)
    return apply


# ── 标签：把链上塞进现有的「数据源」体系 ─────────────────────────
def test_onchain_label_round_trip():
    for key in OC.CHAINS:
        label = S.onchain_label(key)
        assert S.is_onchain_label(label)
        assert S.onchain_chain(label) == key
        assert S.split_label(label) == (S.ONCHAIN, key)


def test_exchange_labels_are_not_mistaken_for_onchain():
    for label in ("Bybit永续", "OKX", "Gate永续", "CoinGecko"):
        assert not S.is_onchain_label(label)


def test_unknown_onchain_label_degrades_quietly():
    """老数据里可能有任何东西，不能因为一个不认识的链名就崩在后台任务里。"""
    assert S.onchain_chain("链上火币链") == ""


# ── 建监控 ───────────────────────────────────────────────────────
def test_watch_by_contract_address(chain_prices):
    chain_prices({EVM: (0.04, token(EVM))})
    ok, msg = asyncio.run(W.add_watch(1, EVM, 20, "me"))
    assert ok
    w = storage.data["watchpct"][0]
    assert w["symbol"] == EVM              # 标的就是地址
    assert w["src"] == "链上BNB链"
    assert w["name"] == "牛来"
    assert w["market"] == "onchain"
    assert "牛来" in msg


def test_solana_address_case_is_preserved(chain_prices):
    """base58 大小写敏感——被 .upper() 改一下这个地址就查不到了。"""
    chain_prices({SOL: (0.09, token(SOL, "sol"))})
    ok, _msg = asyncio.run(W.add_watch(1, SOL, 30, "me"))
    assert ok
    assert storage.data["watchpct"][0]["symbol"] == SOL


def test_bad_address_does_not_create_a_watch(chain_prices):
    """查不到就别建——建了也永远不会触发，用户还以为在盯。"""
    chain_prices({})
    ok, msg = asyncio.run(W.add_watch(1, "0x" + "1" * 40, 10, "me"))
    assert not ok and "查不到" in msg
    assert storage.data["watchpct"] == []


def test_shallow_pool_is_warned_at_setup(chain_prices):
    chain_prices({EVM: (0.04, token(EVM, liq=9_000))})
    _ok, msg = asyncio.run(W.add_watch(1, EVM, 20, "me"))
    assert "⛔" in msg and "假信号" in msg


def test_onchain_watch_warns_about_threshold(chain_prices):
    """链上波动比交易所大得多，±2% 会被刷屏——设的时候就说清楚。"""
    chain_prices({EVM: (0.04, token(EVM))})
    _ok, msg = asyncio.run(W.add_watch(1, EVM, 20, "me"))
    assert "刷屏" in msg


def test_display_name_is_readable():
    """42 位地址直接显示没法读，要给币名 + 首尾缩写。"""
    w = {"symbol": EVM, "name": "牛来"}
    d = W.disp(w)
    assert d.startswith("牛来") and "0xBEEA" in d and len(d) < 30


def test_display_falls_back_to_symbol_for_exchange_coins():
    assert W.disp({"symbol": "BTC"}) == "BTC"


# ── 轮询：必须回到同一条链 ───────────────────────────────────────
def test_polling_goes_back_to_the_same_chain(chain_prices):
    chain_prices({EVM: (0.05, token(EVM))})
    p = asyncio.run(W.fetch_pinned(EVM, "链上BNB链"))
    assert p == 0.05


def test_polling_batch_for_alerts(chain_prices):
    chain_prices({EVM: (0.05, token(EVM))})
    got = asyncio.run(S.prices_at([EVM], "链上BNB链"))
    assert got == {EVM: 0.05}


def test_polling_skips_what_it_cannot_get(chain_prices):
    """链上池子可能被撤走——取不到就沉默，绝不能当成 0 触发跌破。"""
    chain_prices({})
    assert asyncio.run(S.prices_at([EVM], "链上BNB链")) == {}
    assert asyncio.run(W.fetch_pinned(EVM, "链上BNB链")) is None


# ── 价格预警也能盯链上 ───────────────────────────────────────────
def test_price_for_routes_addresses_to_chain(chain_prices):
    """不管默认源设的是哪家交易所，合约地址一律走链上——地址是无歧义的。"""
    chain_prices({EVM: (0.04, token(EVM))})
    S.set_pref(1, "bybit", S.SWAP)
    p, label = asyncio.run(S.price_for(1, EVM))
    assert p == 0.04 and label == "链上BNB链"


def test_alert_on_a_contract_address(chain_prices):
    from handlers import alert as A
    chain_prices({EVM: (0.04, token(EVM))})
    idx = A.add_alert(1, {"type": "fixed", "symbol": EVM, "target": 0.08,
                          "direction": "above", "src": "链上BNB链"})
    assert idx == 0
    assert storage.data["alerts"][0]["src"] == "链上BNB链"


# ── 入口：查完了就该能直接盯 ─────────────────────────────────────
def test_detail_card_offers_monitoring():
    """查完才知道要不要盯，让他退出去打命令等于不会用。"""
    cbs = [b.callback_data for row in OC.detail_kb(token(EVM)).inline_keyboard
           for b in row if b.callback_data]
    assert any(c.startswith(f"oc:w:{EVM}:") for c in cbs)


def test_watch_buttons_use_higher_thresholds():
    """链上默认阈值比交易所高一档：±5% 在小池子上一天能响几十次。"""
    assert min(OC.WATCH_PCTS) >= 10


def test_watch_callbacks_fit_telegram_limit():
    for row in OC.detail_kb(token(SOL, "sol")).inline_keyboard:
        for b in row:
            if b.callback_data:
                assert len(b.callback_data.encode()) <= 64


def test_watch_button_is_routed():
    import inspect
    src = inspect.getsource(OC.on_button)
    assert 'what == "w"' in src
