"""部署触发失败时，错误信息要能指导下一步动作。

2026-08-14 那次只显示「HTTP 503」——看不出是该点重试还是该去改配置。
这两类的处理方式完全相反：503/502/504 等一会重试就好（部署系统在重启），
401/403/404 点一百次也没用（认证或任务名的问题）。
"""
import asyncio

import httpx
import pytest

from handlers import deploy


@pytest.mark.parametrize("code", [502, 503, 504])
def test_transient_failures_tell_you_to_retry(code):
    why = deploy.explain(code)
    assert "重试" in why


@pytest.mark.parametrize("code", [401, 403, 404])
def test_config_failures_tell_you_retrying_wont_help(code):
    """认证/任务名错了还让人一直点重试，是最浪费时间的一种提示。"""
    why = deploy.explain(code)
    assert "重试没用" in why and "配置" in why


def test_503_names_the_most_likely_cause():
    """Jenkins 启动期间对所有请求都回 503——说出来就不用瞎猜了。"""
    assert "启动" in deploy.explain(503)


def test_unknown_code_still_reports_something():
    assert "418" in deploy.explain(418)


def _fake_post(status=None, exc=None):
    class Resp:
        status_code = status
        text = "boom"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            if exc:
                raise exc
            return Resp()
    return lambda *a, **k: Client()


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(deploy, "JENKINS_URL", "https://jenkins.example")
    monkeypatch.setattr(deploy, "JENKINS_USER", "u")
    monkeypatch.setattr(deploy, "JENKINS_API_TOKEN", "t")


def test_failure_message_carries_the_explanation(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _fake_post(status=503))
    ok, msg = asyncio.run(deploy.trigger_deploy("v1.0.0"))
    assert not ok
    assert "503" in msg and "重试" in msg


def test_redirect_is_still_success(monkeypatch):
    """Jenkins 触发成功常回 302/303，别把它当失败。"""
    monkeypatch.setattr(httpx, "AsyncClient", _fake_post(status=302))
    assert asyncio.run(deploy.trigger_deploy("v1.0.0"))[0]


def test_timeout_says_it_could_not_connect(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient",
                        _fake_post(exc=httpx.ConnectTimeout("timed out")))
    ok, msg = asyncio.run(deploy.trigger_deploy("v1.0.0"))
    assert not ok and "连不上" in msg


def test_network_error_says_the_code_did_not_move(monkeypatch):
    """触发失败 ≠ 部署失败：服务器上什么都没动，这句能省掉一轮排查。"""
    monkeypatch.setattr(httpx, "AsyncClient",
                        _fake_post(exc=RuntimeError("dns 挂了")))
    ok, msg = asyncio.run(deploy.trigger_deploy("v1.0.0"))
    assert not ok and "代码没动" in msg


def test_failure_card_explains_nothing_was_deployed():
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    start = src.index("部署触发失败")
    assert "线上仍是旧版本" in src[start:start + 600]
