"""AI 模型自动降级的测试（全部打桩，不联网）。

背景：中转站渠道会不定期下线——模型还在 /v1/models 列表里，但 token 分组下
没账号支持它，请求直接 404 model_not_found，整个 AI 罢工（2026-07-23 线上就是
这么挂的）。降级链是这类故障的唯一兜底，必须锁死行为。
"""
import asyncio
import json
import types
import pytest

from handlers import ai as A


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(payload or {})

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)


def _fake_client(route):
    """route(model) -> _Resp；用它替掉 httpx.AsyncClient。"""
    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            return route(json.get("model"))

    return _C


def _ok(model):
    return _Resp(200, {"choices": [{"message": {"content": f"hi from {model}"}}]})


def _dead(model, code=404):
    return _Resp(code, {"error": {"message": f'Model "{model}" is not supported',
                                  "type": "model_not_found"}})


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(A, "_ACTIVE_MODEL", None, raising=False)
    monkeypatch.setattr(A, "AI_API_KEY", "k")
    monkeypatch.setattr(A, "AI_BASE_URL", "https://relay.invalid/v1")
    monkeypatch.setattr(A, "AI_MODEL", "dead-primary")
    monkeypatch.setattr(A, "AI_FALLBACK_MODELS", ["dead-backup", "good-model", "never"])
    # 没有 /aimodel 手动指定
    monkeypatch.setattr(A, "_model_override", lambda: "")
    yield


def test_falls_through_to_first_working_model(monkeypatch):
    seen = []

    def route(m):
        seen.append(m)
        return _ok(m) if m == "good-model" else _dead(m)

    monkeypatch.setattr(A.httpx, "AsyncClient", _fake_client(route))
    msg = asyncio.run(A._post_chat({"messages": [{"role": "user", "content": "x"}]}))
    assert msg["content"] == "hi from good-model"
    # 按序试过前两个死模型，试到能用的就停（不该继续试 never）
    assert seen == ["dead-primary", "dead-backup", "good-model"]


def test_sticks_to_working_model_on_next_call(monkeypatch):
    seen = []

    def route(m):
        seen.append(m)
        return _ok(m) if m == "good-model" else _dead(m)

    monkeypatch.setattr(A.httpx, "AsyncClient", _fake_client(route))
    asyncio.run(A._post_chat({"messages": []}))
    seen.clear()
    asyncio.run(A._post_chat({"messages": []}))
    assert seen == ["good-model"], "第二次不该再去撞已知的死模型"
    assert A.current_model() == "good-model"


def test_503_no_available_channel_also_switches(monkeypatch):
    def route(m):
        return _ok(m) if m == "good-model" else _dead(m, 503)

    monkeypatch.setattr(A.httpx, "AsyncClient", _fake_client(route))
    msg = asyncio.run(A._post_chat({"messages": []}))
    assert msg["content"] == "hi from good-model"


def test_auth_error_raises_immediately(monkeypatch):
    """401 是密钥问题，换模型没用——必须立刻抛，不要白试一串。"""
    seen = []

    def route(m):
        seen.append(m)
        return _Resp(401, {"error": {"message": "invalid key"}})

    monkeypatch.setattr(A.httpx, "AsyncClient", _fake_client(route))
    import httpx
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(A._post_chat({"messages": []}))
    assert seen == ["dead-primary"]


def test_all_dead_raises_readable_error(monkeypatch):
    monkeypatch.setattr(A.httpx, "AsyncClient", _fake_client(lambda m: _dead(m)))
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(A._post_chat({"messages": []}))
    s = str(ei.value)
    assert "所有 AI 模型都不可用" in s
    assert "dead-primary" in s and "good-model" in s


def test_override_takes_priority(monkeypatch):
    """/aimodel 手动指定的模型必须排在最前面。"""
    monkeypatch.setattr(A, "_model_override", lambda: "chosen-by-admin")
    assert A._model_candidates()[0] == "chosen-by-admin"
    assert A.current_model() == "chosen-by-admin"


def test_candidates_have_no_duplicates(monkeypatch):
    monkeypatch.setattr(A, "AI_MODEL", "good-model")
    monkeypatch.setattr(A, "AI_FALLBACK_MODELS", ["good-model", "b", "b"])
    c = A._model_candidates()
    assert len(c) == len(set(c))


def test_body_model_is_not_mutated_across_attempts(monkeypatch):
    """传入的 body 不该被就地写死成某个模型（调用方会复用它）。"""
    monkeypatch.setattr(A.httpx, "AsyncClient",
                        _fake_client(lambda m: _ok(m) if m == "good-model" else _dead(m)))
    body = {"messages": [{"role": "user", "content": "x"}]}
    asyncio.run(A._post_chat(body))
    assert "model" not in body
