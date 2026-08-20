"""币安交易接入 + 交易所切换。

他要「再接个币安 api 做币安合约现货交易」。做法是**换 client 不换逻辑**：
handlers/rtrade.py 有 965 行风控（二次确认、reduceOnly 强制、杠杆护栏、审计），
复制一套等于同一套风控维护两遍，分叉的那天会在真钱路径上发现。

所以这里守两件事：
  1. 两家客户端的**方法签名必须对齐**——对不齐 rtrade 就会在换所时炸；
  2. killswitch / LIVE_TRADING 这类闸门**换所也要拦得住**——
     换个交易所就绕过去的话，那个开关等于没有。
"""
import inspect
import os

import pytest

import bybit_trade as BY
import binance_trade as BN
from handlers import venue


@pytest.fixture(autouse=True)
def _restore():
    old = {k: os.environ.get(k) for k in
           ("BINANCE_TESTNET", "BINANCE_API_KEY", "BINANCE_API_SECRET",
            "LIVE_TRADING", "BYBIT_TESTNET")}
    import storage
    old_v = storage.data.get("trade_venue")
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if old_v is None:
        storage.data.pop("trade_venue", None)
    else:
        storage.data["trade_venue"] = old_v


# ── 接口对齐 ────────────────────────────────────────────────
SHARED = ["instrument_info", "last_price", "wallet_balance", "position",
          "positions_all", "set_leverage", "place_limit", "place_market",
          "set_trading_stop", "cancel", "cancel_all", "open_orders"]


@pytest.mark.parametrize("name", SHARED)
def test_both_clients_expose_the_same_method(name):
    assert hasattr(BY.BybitClient, name), f"Bybit 少了 {name}"
    assert hasattr(BN.BinanceClient, name), f"币安少了 {name}"


@pytest.mark.parametrize("name", ["place_limit", "place_market", "position",
                                  "wallet_balance", "set_leverage"])
def test_signatures_line_up(name):
    """签名对不齐，rtrade 换所那一刻就炸——而那是真钱路径。"""
    a = list(inspect.signature(getattr(BY.BybitClient, name)).parameters)
    b = list(inspect.signature(getattr(BN.BinanceClient, name)).parameters)
    assert a[:len(a)] == b[:len(a)], f"{name}: Bybit {a} vs 币安 {b}"


# ── 端点与默认安全 ──────────────────────────────────────────
@pytest.mark.parametrize("val,mode", [
    ("false", "live"), ("0", "live"), ("no", "live"),
    ("true", "testnet"), ("", "testnet"), ("随便写", "testnet"),
])
def test_default_is_testnet(val, mode):
    """认不出来的值一律当测试网——防手滑上实盘，和 Bybit 同一条规矩。"""
    os.environ["BINANCE_TESTNET"] = val
    assert BN._mode() == mode


def test_futures_and_spot_have_separate_endpoints():
    os.environ["BINANCE_TESTNET"] = "true"
    assert "testnet.binancefuture.com" in BN.BASES[("testnet", "futures")]
    assert "testnet.binance.vision" in BN.BASES[("testnet", "spot")]
    assert "fapi.binance.com" in BN.BASES[("live", "futures")]


def test_auth_hint_names_the_two_key_systems():
    """真机上 Bybit 那次就栽在"两套模拟盘 key 不通用"，币安同样有这个坑。"""
    for token in ("testnet.binancefuture.com", "启用期货", "BINANCE_TESTNET=true"):
        assert token in BN.AUTH_HINT
    assert "probe" in BN.AUTH_HINT


def test_binance_error_codes_are_kept():
    """币安错误码很具体（-2019 保证金不足），甩原码比"下单失败"有用。"""
    e = BN.BinanceError(-2019, "Margin is insufficient", "/fapi/v1/order")
    assert e.ret_code == -2019 and "-2019" in str(e)
    assert BN.is_auth_error(BN.BinanceError(-2015, "Invalid API-key"))
    assert not BN.is_auth_error(BN.BinanceError(-2019, "Margin is insufficient"))


# ── 闸门换所也要拦得住 ──────────────────────────────────────
def test_killswitch_blocks_binance_too():
    """换个交易所就绕过去的话，那个开关等于没有。"""
    import storage
    os.environ["BYBIT_TESTNET"] = "false"
    os.environ.pop("LIVE_TRADING", None)
    storage.data["trading_disabled"] = True
    try:
        src = inspect.getsource(BN.BinanceClient.place_limit)
        assert "_guard_order" in src
        src2 = inspect.getsource(BN.BinanceClient.place_market)
        assert "_guard_order" in src2
        src3 = inspect.getsource(BN.BinanceClient.spot_order)
        assert "_guard_order" in src3, "现货下单也要过闸"
        with pytest.raises(RuntimeError):
            BY._guard_order({"symbol": "BTCUSDT", "reduceOnly": False})
    finally:
        storage.data.pop("trading_disabled", None)


# ── 切换 ────────────────────────────────────────────────────
def test_default_venue_is_bybit():
    import storage
    storage.data.pop("trade_venue", None)
    assert venue.current() == "bybit"


def test_switch_persists():
    assert venue.set_venue("binance") is True
    assert venue.current() == "binance"
    assert venue.set_venue("okx") is False, "不支持的交易所要拒绝"


def test_tag_says_which_exchange_and_whether_real_money():
    """只写"实盘/模拟"是不够的——多了一家之后，"这单下在哪儿"必须一眼看到。"""
    os.environ["BINANCE_TESTNET"] = "true"
    venue.set_venue("binance")
    t = venue.tag()
    assert "币安" in t and ("测试网" in t or "模拟" in t)
    os.environ["BYBIT_TESTNET"] = "false"
    venue.set_venue("bybit")
    assert "Bybit" in venue.tag() and "实盘" in venue.tag()


def test_configured_checks_before_switching():
    """切过去才发现没 key，等于白切一次。"""
    os.environ.pop("BINANCE_API_KEY", None)
    assert venue.configured("binance") is False
    os.environ["BINANCE_API_KEY"] = "k"
    os.environ["BINANCE_API_SECRET"] = "s"
    assert venue.configured("binance") is True


def test_rtrade_takes_its_client_from_venue():
    """整个 rtrade 只在一处取客户端，换所才不用改那 900 行。"""
    from handlers import rtrade
    src = inspect.getsource(rtrade._client)
    assert "venue.client()" in src
    assert "BybitClient()" not in src


def test_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("venue"' in src and 'BotCommand("venue"' in src


def test_compose_passes_binance_keys():
    """docker-compose 的 environment 是白名单，漏了这几行 .env 写了也没用。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "docker-compose.yml").read_text(encoding="utf-8")
    for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_TESTNET"):
        assert k in src
