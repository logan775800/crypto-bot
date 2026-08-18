"""/keycheck 失败时要能自己说清是哪一种。

真机上（2026-08-18）：probe 明确验出这把 key 在实盘端点有效、能查到 198 USDT，
可 /keycheck 仍报 `[Bybit 10003] API key is invalid (/v5/user/query-api)`，
而那条消息里**连自己在打哪个端点都没写**——排查表第一条恰恰是"端点对不上"，
看的人没法对号入座。
"""
import asyncio

import pytest

from handlers import keyguard


def _explain(err, alive_ok, alive_detail="1000"):
    async def fake_alive():
        return alive_ok, alive_detail

    orig = keyguard._alive
    keyguard._alive = fake_alive
    try:
        return asyncio.run(keyguard._explain_failure(err, "⚠️ 实盘 LIVE　api.bybit.com"))
    finally:
        keyguard._alive = orig


def test_endpoint_is_always_stated():
    """无论哪种失败，都要写明当时在打哪个端点。"""
    for ok in (True, False):
        out = _explain(RuntimeError("[Bybit 10003] API key is invalid"), ok)
        assert "api.bybit.com" in out and "实盘" in out


def test_key_works_but_endpoint_not_supported():
    """余额查得到 = 密钥和端点都对，只是这个接口调不了。
    这时候不该甩一张"key 无效"的排查表吓人。"""
    out = _explain(RuntimeError("[Bybit 10003] API key is invalid"), True, "198.58")
    assert "查余额是通的" in out and "198.58" in out
    assert "不受影响" in out
    assert "BYBIT_TESTNET=demo" not in out, "别在这种情况下贴端点排查表"
    # 自动核对不了，就必须明确让他自己去后台看这两项
    assert "提现权限" in out and "IP" in out


def test_key_really_broken_gets_the_hint():
    out = _explain(RuntimeError("401 API key is invalid"), False, "401 …")
    assert "查余额也不通" in out
    assert "BYBIT_TESTNET=demo" in out, "这种才该贴端点排查表"


def test_non_auth_failure_does_not_get_the_auth_hint():
    out = _explain(RuntimeError("Connection timeout"), False, "timeout")
    assert "BYBIT_TESTNET=demo" not in out


def test_keycheck_uses_the_explainer():
    import inspect
    src = inspect.getsource(keyguard.keycheck_cmd)
    assert "_explain_failure" in src
