"""LIVE_TRADING=off —— 给「我根本不做实盘」准备的硬开关。

为什么不能只靠 /killswitch：它的状态存在 data.json 里，而 data.json 被恢复/
重置过（v1.6.1 修的那次部署跑测试覆盖生产数据）。一旦丢了，
`trading_enabled()` 默认返回 True，也就是**默认允许下单**——
对一个不打算碰实盘的人来说，这个默认值是反的，而且丢了他不会知道。
"""
import os

import pytest

import storage
from handlers import keyguard as K


@pytest.fixture(autouse=True)
def _restore():
    old = os.environ.get("LIVE_TRADING")
    storage.data.pop("trading_disabled", None)
    yield
    if old is None:
        os.environ.pop("LIVE_TRADING", None)
    else:
        os.environ["LIVE_TRADING"] = old
    storage.data.pop("trading_disabled", None)


@pytest.mark.parametrize("val", ["off", "OFF", "0", "false", "no", " off "])
def test_hard_off_blocks_trading(val):
    os.environ["LIVE_TRADING"] = val
    assert K.trading_enabled() is False
    assert K.hard_disabled() is True


@pytest.mark.parametrize("val", ["", "on", "true", "1"])
def test_other_values_leave_the_soft_switch_in_charge(val):
    os.environ["LIVE_TRADING"] = val
    assert K.hard_disabled() is False
    assert K.trading_enabled() is True
    storage.data["trading_disabled"] = True
    assert K.trading_enabled() is False       # 软开关照常生效


def test_hard_off_survives_a_data_reset():
    """data.json 丢了也不会悄悄变回可下单——这正是硬开关存在的理由。"""
    os.environ["LIVE_TRADING"] = "off"
    storage.data.pop("trading_disabled", None)
    assert K.trading_enabled() is False


def test_order_guard_respects_it():
    """闸门在下单函数内部，不靠调用方自觉。"""
    import bybit_trade
    os.environ["LIVE_TRADING"] = "off"
    with pytest.raises(RuntimeError):
        bybit_trade._guard_order({"symbol": "BTCUSDT", "reduceOnly": False})


def test_closing_is_still_allowed():
    """禁的只有开仓。出事时最该畅通的恰恰是平仓。"""
    import bybit_trade
    os.environ["LIVE_TRADING"] = "off"
    bybit_trade._guard_order({"symbol": "BTCUSDT", "reduceOnly": True})


def test_compose_passes_it_through():
    """docker-compose 的 environment 是白名单，漏了这行 .env 里写了也没用。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "docker-compose.yml").read_text(encoding="utf-8")
    assert "LIVE_TRADING" in src
