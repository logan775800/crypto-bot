"""测试公共装置：config 在 import 时就读 BOT_TOKEN，必须先注入假值；
storage 落盘路径是容器内的 /app，本地跑测试时把 save_data 打桩掉。"""
import os
import sys

os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("ADMIN_CHAT_ID", "111,222")
os.environ.setdefault("AI_API_KEY", "")
os.environ.setdefault("AI_BASE_URL", "")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch):
    """所有测试都不真写盘。"""
    import storage
    monkeypatch.setattr(storage, "save_data", lambda: None)
    yield
