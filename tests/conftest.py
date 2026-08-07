"""测试公共装置。

⚠️ 这里最重要的一件事是 **DATA_FILE 隔离**，不是打桩 save_data。

历史教训（2026-08-07）：部署流水线用 `docker run -v "$PWD":/app` 跑测试，
容器里的 `/app/data.json` 就是**生产数据**。测试的清理夹具会把
`data["contract_watch"] = []` 这类赋值写回去，于是每次部署都清空用户订阅、
风控参数，甚至把 killswitch 留在"已禁用"。

原来只打桩 `storage.save_data` 是无效的：各模块写的是
`from storage import save_data`，绑定的是**函数对象**，替换
`storage.save_data` 属性对它们没有任何影响。

所以真正的护栏是**改写盘路径**——不管谁持有哪个 save_data 引用，
写出去的目标都只能是临时文件。
"""
import os
import sys
import tempfile

# 必须在 import config/storage **之前**改掉，storage 在 import 时就会读它
_tmp_fd, _TMP_DATA = tempfile.mkstemp(prefix="pytest_data_", suffix=".json")
os.close(_tmp_fd)
os.environ["DATA_FILE"] = _TMP_DATA

os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("ADMIN_CHAT_ID", "111,222")
os.environ.setdefault("AI_API_KEY", "")
os.environ.setdefault("AI_BASE_URL", "")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


def pytest_sessionstart(session):
    """开跑前硬校验：写盘路径绝不能指向生产文件。

    这条断言比任何测试都重要——它保护的是用户的真实数据，
    而不是某个函数的正确性。宁可整场测试拒绝启动。
    """
    import storage
    bad = ("/app/data.json", "data.json")
    assert storage.DATA_FILE == _TMP_DATA, (
        f"DATA_FILE 没被隔离（当前 {storage.DATA_FILE}）——"
        f"测试会写进生产数据，拒绝启动")
    assert not any(storage.DATA_FILE.endswith(b) and "pytest_data_" not in
                   storage.DATA_FILE for b in bad)


def pytest_sessionfinish(session, exitstatus):
    try:
        os.unlink(_TMP_DATA)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch):
    """再加一层：把 storage.save_data 也换成空操作。

    注意这一层**只对通过 `storage.save_data` 调用的地方生效**，
    对 `from storage import save_data` 的模块无效——真正兜底的是上面的
    DATA_FILE 隔离。这层留着是为了少写一堆临时文件。
    """
    import storage
    monkeypatch.setattr(storage, "save_data", lambda: None)
    yield
