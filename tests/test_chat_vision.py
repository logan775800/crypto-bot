"""AI 视觉（看图）链路的纯函数/结构测试，不联网。

关注三件容易回归的事：
  1. 多尺寸图要挑「不超限的最大张」，全超限也不能崩；
  2. 发给中转站的 content 必须是 OpenAI 视觉数组格式；
  3. 图片只在当轮上传，历史里必须换成纯文本占位——否则每轮重传几百KB base64。
"""
import time
import types
import pytest

from handlers import chat as C


class _PS:
    def __init__(self, fid, size):
        self.file_id = fid
        self.file_size = size


class _Msg:
    def __init__(self, photo=None, caption=None, text=None, reply_to=None, doc=None):
        self.photo = photo or []
        self.caption = caption
        self.text = text
        self.reply_to_message = reply_to
        self.document = doc
        self.sent = []

    async def reply_text(self, t, **kw):
        self.sent.append(t)


class _Ctx:
    def __init__(self):
        self.chat_data = {}
        self.user_data = {}


# ── _pick_photo ──────────────────────────────────────────────────
def test_pick_photo_takes_largest_within_limit():
    msg = _Msg(photo=[_PS("s", 1000), _PS("m", 50_000), _PS("l", 200_000)])
    assert C._pick_photo(msg)[0] == "l"


def test_pick_photo_skips_oversize():
    msg = _Msg(photo=[_PS("ok", 1000), _PS("huge", C.MAX_IMG_BYTES + 1)])
    assert C._pick_photo(msg)[0] == "ok"


def test_pick_photo_all_oversize_falls_back_not_crash():
    msg = _Msg(photo=[_PS("a", C.MAX_IMG_BYTES + 1), _PS("b", C.MAX_IMG_BYTES + 2)])
    fid, mime = C._pick_photo(msg)
    assert fid == "a" and mime == "image/jpeg"


def test_pick_photo_accepts_image_document():
    doc = types.SimpleNamespace(mime_type="image/png", file_size=2000, file_id="d1")
    assert C._pick_photo(_Msg(doc=doc)) == ("d1", "image/png")


def test_pick_photo_rejects_non_image_document():
    doc = types.SimpleNamespace(mime_type="application/pdf", file_size=2000, file_id="d1")
    assert C._pick_photo(_Msg(doc=doc)) == (None, None)


def test_pick_photo_none_message():
    assert C._pick_photo(None) == (None, None)


# ── 「先发图、下一条才@提问」的暂存 ───────────────────────────────
def test_remember_and_recall_photo():
    ctx = _Ctx()
    C._remember_photo(ctx, 555, "f1", "image/jpeg")
    assert C._recall_photo(ctx, 555) == ("f1", "image/jpeg")


def test_recall_photo_expires():
    ctx = _Ctx()
    C._remember_photo(ctx, 555, "f1", "image/jpeg")
    ctx.chat_data["recent_photo"]["555"]["ts"] = time.time() - C.PHOTO_TTL - 1
    assert C._recall_photo(ctx, 555) == (None, None)


def test_recall_photo_is_per_user():
    ctx = _Ctx()
    C._remember_photo(ctx, 555, "f1", "image/jpeg")
    assert C._recall_photo(ctx, 666) == (None, None)


# ── 发给模型的报文结构 + 历史瘦身 ─────────────────────────────────
def test_vision_payload_shape_and_history_is_slimmed(monkeypatch):
    """带图那轮 content 必须是 [text, image_url] 数组；回合结束后历史里不许残留 base64。

    仓库没装 pytest-asyncio，用 asyncio.run 直接驱动协程（不为一个测试加运行时依赖）。
    """
    import asyncio
    seen = {}

    async def fake_tools(messages, tools, executor, system=None, **kw):
        import copy
        seen["msgs"] = copy.deepcopy(messages)   # 深拷贝：调用后会被就地改写
        return "看到了，这是一张K线截图。"

    async def _noop(**kw):
        return None

    monkeypatch.setattr(C, "ask_ai_tools", fake_tools)
    monkeypatch.setattr(C, "AI_API_KEY", "k")
    monkeypatch.setattr(C, "AI_BASE_URL", "https://x.invalid/v1")

    msg = _Msg(text="这单怎么看")
    ctx = _Ctx()
    ctx.bot = types.SimpleNamespace(id=1, username="b", send_chat_action=_noop)
    upd = types.SimpleNamespace(message=msg,
                                effective_chat=types.SimpleNamespace(id=-1, type="supergroup"),
                                effective_user=types.SimpleNamespace(id=777))

    asyncio.run(C._reply(upd, ctx, "这单怎么看",
                         images=["data:image/jpeg;base64,AAAA"]))

    sent = seen["msgs"][-1]["content"]
    assert isinstance(sent, list)
    assert sent[0] == {"type": "text", "text": "这单怎么看"}
    assert sent[1]["type"] == "image_url"
    assert sent[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    hist = ctx.chat_data["chat_hist"]
    assert all(isinstance(h["content"], str) for h in hist), "历史里不该残留图片数组"
    assert "base64" not in str(hist)


def test_system_prompt_mentions_vision():
    """系统提示必须交代它能看图 + 别拿截图旧价当现价，否则模型会继续说「我看不到图」。"""
    assert "你能看图" in C.SYSTEM
    assert "截图" in C.SYSTEM
