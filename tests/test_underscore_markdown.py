"""带下划线标识符的消息，绝不能用 Markdown 发出去。

真机现象（2026-08-18）：/keycheck 的排查表里写着 BYBIT_TESTNET=true，
Telegram 的 legacy Markdown 把成对下划线当成斜体标记吃掉，屏幕上显示的是
「BYBITTESTNET=true」，整段还变成了斜体（中文斜体本来就不该出现）。
他照着抄就是一条错的配置——**排查提示本身给出了错误的答案**。

同一个坑还有别的形态：os.environ['BYBIT_API_KEY'] 被吃成 BYBITAPIKEY。
所以这里守两条：内容里不留 Markdown 标记，发送时不带 parse_mode。
"""
import inspect

import pytest

import bybit_trade
from handlers import keyguard

# Telegram 把**任意两个**下划线配成一对斜体标记——不必在同一个词里，
# 跨行也照配。所以只要正文里下划线个数 ≥2，走 Markdown 就一定会被吃掉。


def test_auth_hint_keeps_its_underscores_intact():
    """排查表里的环境变量名必须原样可抄。"""
    for name in ("BYBIT_TESTNET=true", "BYBIT_TESTNET=demo", "BYBIT_TESTNET=false"):
        assert name in bybit_trade.AUTH_HINT


def test_auth_hint_has_no_markdown_markers():
    """它是纯文本发的，写了 ** 或 ` 只会原样显示出来。"""
    assert "**" not in bybit_trade.AUTH_HINT
    assert "`" not in bybit_trade.AUTH_HINT


def test_auth_hint_would_be_mangled_by_markdown():
    """说明为什么必须纯文本发：正文里下划线不止一个，Markdown 会两两配对成斜体。
    哪天有人给它加上 parse_mode，这条测试就是证据。"""
    assert bybit_trade.AUTH_HINT.count("_") >= 2


def test_keycheck_failure_is_sent_as_plain_text():
    src = inspect.getsource(keyguard.keycheck_cmd)
    # 只看 except 块本身：后面成功分支是可以用 Markdown 的
    fail_part = src.split("except Exception as e:")[1].split("return")[0]
    assert "_explain_failure" in fail_part
    assert "parse_mode" not in fail_part, \
        "失败提示里全是带下划线的变量名，走 Markdown 会被吃掉下划线"


def test_explain_failure_output_has_no_markdown():
    """输出是纯文本，别在里面写 ** 和反引号。"""
    import asyncio

    async def fake_alive():
        return False, "401"

    orig, keyguard._alive = keyguard._alive, fake_alive
    try:
        for err in (RuntimeError("401 API key is invalid"), RuntimeError("timeout")):
            out = asyncio.run(keyguard._explain_failure(err, "测试环境"))
            assert "**" not in out and "`" not in out
    finally:
        keyguard._alive = orig
