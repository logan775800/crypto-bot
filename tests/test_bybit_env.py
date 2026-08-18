"""Bybit 环境/端点选择。

配置这把 key 时第一次就翻在这上面：主站「模拟交易 Demo」里建的 key 打去
api-testnet.bybit.com，交易所只回 `401 API key is invalid`——一个字都没提
是端点选错了。三套环境的 key 互不通用，选错就是这个症状。
"""
import importlib
import os

import pytest


def _mod(val):
    """按给定的 BYBIT_TESTNET 重新加载模块（它在导入时读环境变量）。"""
    os.environ["BYBIT_TESTNET"] = val
    import bybit_trade
    return importlib.reload(bybit_trade)


@pytest.fixture(autouse=True)
def _restore():
    old = os.environ.get("BYBIT_TESTNET")
    yield
    if old is None:
        os.environ.pop("BYBIT_TESTNET", None)
    else:
        os.environ["BYBIT_TESTNET"] = old
    import bybit_trade
    importlib.reload(bybit_trade)


@pytest.mark.parametrize("val,mode,host", [
    ("true", "testnet", "api-testnet.bybit.com"),
    ("demo", "demo", "api-demo.bybit.com"),
    ("false", "live", "api.bybit.com"),
])
def test_three_environments(val, mode, host):
    m = _mod(val)
    assert m._mode() == mode
    assert host in m._base_url()


@pytest.mark.parametrize("val", ["", "yes", "TRUE", "随便写点什么"])
def test_anything_unrecognised_stays_on_testnet(val):
    """认不出来的值一律当模拟盘——防手滑上实盘，这条不能松。"""
    assert _mod(val)._mode() == "testnet"


@pytest.mark.parametrize("val", ["FALSE", "False", " false "])
def test_live_needs_an_explicit_false(val):
    assert _mod(val)._mode() == "live"


def test_demo_is_not_treated_as_live():
    """demo 是模拟交易，绝不能被当成实盘走 api.bybit.com。"""
    m = _mod("demo")
    assert "api.bybit.com" not in m._base_url()


def test_auth_error_is_recognised():
    m = _mod("true")

    class Resp:
        status_code = 401

    class Err(Exception):
        response = Resp()

    assert m.is_auth_error(Err("401 API key is invalid"))
    assert m.is_auth_error(RuntimeError("Client error 'API key is invalid'"))
    assert not m.is_auth_error(RuntimeError("timeout"))


def test_hint_names_all_three_environments():
    """排查表必须把三个端点都点名——只说"检查 key 是否正确"等于没说。"""
    m = _mod("true")
    for token in ("testnet.bybit.com", "模拟交易", "BYBIT_TESTNET=demo",
                  "BYBIT_TESTNET=false", "force-recreate" if False else "引号"):
        assert token in m.AUTH_HINT


def test_probe_tries_every_endpoint_and_restores_env(monkeypatch):
    """挨个试三个端点，别让用户靠猜；试完必须把 BYBIT_TESTNET 还原，
    否则探测本身会把运行环境改掉（探到 live 就更危险了）。"""
    import asyncio
    m = _mod("true")
    monkeypatch.setattr(m, "BYBIT_API_KEY", "K" * 18)
    monkeypatch.setattr(m, "BYBIT_API_SECRET", "S" * 20)
    seen = []

    class FakeClient:
        async def wallet_balance(self, coin):
            seen.append(m._mode())
            if m._mode() != "demo":
                raise RuntimeError("401 API key is invalid")
            return {"totalEquity": "1000"}

    monkeypatch.setattr(m, "BybitClient", FakeClient)
    asyncio.run(m._probe())
    assert seen == ["testnet", "demo", "live"], "三个端点都要试一遍"
    assert os.environ["BYBIT_TESTNET"] == "true", "探测完必须还原环境"


def test_probe_says_nothing_when_key_is_empty(monkeypatch, capsys):
    """key 是空的说明容器没读到，这时候试端点毫无意义，要直说。"""
    import asyncio
    m = _mod("true")
    monkeypatch.setattr(m, "BYBIT_API_KEY", "")
    asyncio.run(m._probe())
    assert "force-recreate" in capsys.readouterr().out


def test_hint_points_at_the_probe():
    m = _mod("true")
    assert "probe" in m.AUTH_HINT


def test_keycheck_surfaces_the_hint():
    """他不该为了看一句提示去 SSH 服务器——/keycheck 里也要能看到。
    （排查表现在由 _explain_failure 统一输出，见 test_keycheck_diag.py）"""
    import inspect
    from handlers import keyguard
    src = inspect.getsource(keyguard._explain_failure)
    assert "AUTH_HINT" in src and "is_auth_error" in src
