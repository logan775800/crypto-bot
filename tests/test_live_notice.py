"""连着实盘时，启动播报要主动说一声。

「现在连的是真钱账户还是模拟盘」以前只有发 /version 才看得到。
重启、改 .env、部署都会重来一遍，真钱的事不该靠人记得去查。
"""
import os

import pytest

from handlers import monitor


@pytest.fixture(autouse=True)
def _restore():
    old = os.environ.get("BYBIT_TESTNET")
    yield
    if old is None:
        os.environ.pop("BYBIT_TESTNET", None)
    else:
        os.environ["BYBIT_TESTNET"] = old


def _set(mode, monkeypatch, key="K" * 18):
    os.environ["BYBIT_TESTNET"] = mode
    import bybit_trade
    monkeypatch.setattr(bybit_trade, "BYBIT_API_KEY", key)


def test_live_is_announced(monkeypatch):
    _set("false", monkeypatch)
    out = monitor._live_warning()
    assert "实盘" in out and "killswitch" in out


@pytest.mark.parametrize("mode", ["true", "demo"])
def test_simulation_says_nothing(mode, monkeypatch):
    """模拟盘不用喊——喊多了就没人看了。"""
    _set(mode, monkeypatch)
    assert monitor._live_warning() == ""


def test_no_key_says_nothing(monkeypatch):
    _set("false", monkeypatch, key="")
    assert monitor._live_warning() == ""


def test_killswitch_state_is_included(monkeypatch):
    """开关关着他点开仓会被拒，到时候又要排查半天——一起报出来。"""
    _set("false", monkeypatch)
    from handlers import keyguard
    monkeypatch.setattr(keyguard, "trading_enabled", lambda: False)
    assert "已禁用" in monitor._live_warning()
    monkeypatch.setattr(keyguard, "trading_enabled", lambda: True)
    assert "开启" in monitor._live_warning()


def test_startup_notify_carries_it(monkeypatch):
    import inspect
    src = inspect.getsource(monitor.startup_notify)
    assert "_live_warning()" in src


def test_failure_does_not_break_startup(monkeypatch):
    """这条提示再重要也不能挡住启动播报本身。"""
    _set("false", monkeypatch)
    from handlers import keyguard
    monkeypatch.setattr(keyguard, "trading_enabled",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert monitor._live_warning() == ""
