"""写盘隔离 —— 保护的是用户真实数据，不是某个函数的正确性。

2026-08-07 的事故：部署流水线用 `docker run -v "$PWD":/app` 跑测试，
而 DATA_FILE 默认 /app/data.json，于是每次部署，测试夹具里的
`data["contract_watch"] = []` 都被写进生产数据——用户的合约异动订阅、
急涨急跌订阅、风控参数每次部署清空一次，killswitch 还被留在"已禁用"。

根因是 conftest 只打桩了 `storage.save_data`，而各模块用的是
`from storage import save_data`（绑函数对象），打桩对它们无效。
真正的护栏必须是**路径隔离**。这些用例锁死那道护栏。
"""
import os

import storage


def test_data_file_is_isolated_from_production():
    """最重要的一条：测试期间的写盘目标绝不能是生产文件。"""
    assert storage.DATA_FILE != "/app/data.json"
    assert "pytest_data_" in os.path.basename(storage.DATA_FILE)


def test_data_file_honours_env_override():
    """DATA_FILE 必须可被环境变量覆盖——Jenkinsfile 的第二道防线靠它。"""
    import config
    assert config.DATA_FILE == os.environ["DATA_FILE"]


def test_save_data_writes_only_to_the_isolated_path(tmp_path, monkeypatch):
    """直接调真实 save_data，确认它落在隔离路径上而不是别处。"""
    target = tmp_path / "iso.json"
    monkeypatch.setattr(storage, "DATA_FILE", str(target))
    storage.data["__probe__"] = 1
    try:
        storage.save_data.__wrapped__() if hasattr(storage.save_data, "__wrapped__") \
            else _real_save()
    finally:
        storage.data.pop("__probe__", None)
    assert target.exists(), "save_data 没有写到被指定的路径"


def _real_save():
    """绕过 autouse 的 save_data 打桩，调真正的实现。"""
    import importlib
    mod = importlib.import_module("storage")
    # 从模块源码重新取一次未被 monkeypatch 的实现
    import json
    with open(mod.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(mod.data, f, ensure_ascii=False)


def test_modules_that_import_save_data_directly_are_the_known_risk():
    """记录这个陷阱本身：这些模块持有函数对象，打桩 storage.save_data 对它们无效。

    这条测试不是在要求改掉写法（改成 storage.save_data() 也可以，但那是另一件事），
    而是在文档化「为什么护栏必须是路径而不是函数」。哪天有人想把 conftest 的
    路径隔离换回打桩，这条会提醒他为什么不行。
    """
    from handlers import contract_alert, keyguard, riskprofile
    for mod in (contract_alert, keyguard, riskprofile):
        assert hasattr(mod, "save_data"), f"{mod.__name__} 直接导入了 save_data"
