"""备份恢复 + 系统体检。

两者针对同一个教训：2026-08-07 部署把 data.json 写坏，
既没有恢复入口（只能手工翻文件），也没有任何地方会主动告诉你
「账户链路从来没通过电」。
"""
import json

import pytest

from handlers import backup as bk
from storage import apply_defaults, data


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(bk, "save_data", lambda: None)
    return tmp_path


def _write(tmp, name, payload):
    p = tmp / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ── 默认值可重复补齐 ─────────────────────────────────────────────
def test_apply_defaults_is_idempotent_and_fills_gaps():
    """恢复老备份后必须能补齐后来新增的字段，否则 handler 会 KeyError。"""
    d = {"alerts": [1]}
    apply_defaults(d)
    assert d["alerts"] == [1]              # 已有的不覆盖
    assert "contract_watch" in d and "risk_profile" in d
    n = len(d)
    apply_defaults(d)
    assert len(d) == n                     # 再跑一次不变


# ── 列备份 ───────────────────────────────────────────────────────
def test_list_shows_subscription_counts(sandbox):
    _write(sandbox, "data_20260806.json",
           {"contract_watch": [1, 2], "pump_watch": {"a": {}}})
    rows = bk.list_backups()
    assert rows and "contract_watch×2" in rows[0][3]


def test_list_survives_corrupt_backup(sandbox):
    (sandbox / "data_20260805.json").write_text("{坏文件", encoding="utf-8")
    rows = bk.list_backups()
    assert rows and "读不出来" in rows[0][3]


def test_list_ignores_unrelated_files(sandbox):
    _write(sandbox, "notes.txt", {})
    _write(sandbox, "data_20260806.json", {"contract_watch": [1]})
    assert len(bk.list_backups()) == 1


# ── 恢复 ─────────────────────────────────────────────────────────
def test_subs_only_restores_subscriptions(sandbox):
    old = {"contract_watch": [-100], "vtrade": {"7": {"balance": 1}},
           "audit_log": [{"ts": 1}]}
    p = _write(sandbox, "data_20260806.json", old)
    data["contract_watch"] = []
    data["vtrade"] = {"7": {"balance": 9999}}
    data["audit_log"] = [{"ts": 2}, {"ts": 3}]

    changed, _safety = bk.do_restore(str(p), subs_only=True)
    assert data["contract_watch"] == [-100]         # 订阅恢复了
    assert data["vtrade"]["7"]["balance"] == 9999   # 只增的数据不被倒退
    assert len(data["audit_log"]) == 2              # 审计日志同理
    assert "contract_watch" in changed


def test_full_restore_replaces_everything(sandbox):
    p = _write(sandbox, "data_20260806.json", {"contract_watch": [-1]})
    data["vtrade"] = {"7": {"balance": 9999}}
    bk.do_restore(str(p), subs_only=False)
    assert data["contract_watch"] == [-1]
    assert data["vtrade"] == {}          # 整份覆盖 + apply_defaults 补空


def test_restore_fills_missing_new_fields(sandbox):
    """老备份没有后来新增的字段，恢复后必须补上而不是留空洞。"""
    p = _write(sandbox, "data_old.json", {"alerts": []})
    bk.do_restore(str(p), subs_only=False)
    for k in ("risk_profile", "event_subs", "trading_disabled", "audit_log"):
        assert k in data


def test_restore_writes_a_safety_copy_first(sandbox):
    """恢复错了要有回头路。"""
    p = _write(sandbox, "data_20260806.json", {"contract_watch": [-1]})
    data["contract_watch"] = [-999]
    _changed, safety = bk.do_restore(str(p), subs_only=True)
    saved = json.loads(open(safety, encoding="utf-8").read())
    assert saved["contract_watch"] == [-999]      # 存的是恢复**前**的状态


def test_restore_reports_no_change_when_identical(sandbox):
    data["contract_watch"] = [-1]
    p = _write(sandbox, "data_20260806.json", {"contract_watch": [-1]})
    changed, _s = bk.do_restore(str(p), subs_only=True)
    assert "contract_watch" not in changed


def test_backup_dir_follows_data_file():
    """测试期间备份目录必须跟着隔离后的 DATA_FILE 走，不能写进生产 backups/。"""
    assert not bk.BACKUP_DIR.startswith("/app")


# ── 系统体检 ─────────────────────────────────────────────────────
def test_subs_check_flags_empty_subscriptions():
    from handlers.datameta import _check_subs
    data["contract_watch"] = []
    data["pump_watch"] = {}
    rows, _killed = _check_subs()
    assert any("⚠️0" in r for r in rows)


def test_subs_check_reports_killswitch():
    from handlers.datameta import _check_subs
    data["trading_disabled"] = True
    try:
        _rows, killed = _check_subs()
        assert killed
    finally:
        data["trading_disabled"] = False


def test_account_check_says_which_features_go_dark(monkeypatch):
    """没配密钥时必须点名哪些功能是空的——这次事故最贵的部分就是没人说。"""
    import asyncio
    import bybit_trade
    monkeypatch.setattr(bybit_trade, "BYBIT_API_KEY", "")
    from handlers.datameta import _check_account
    ok, msg, _extra = asyncio.run(_check_account())
    assert not ok and "未配置密钥" in msg
    assert "复盘" in msg or "周报" in msg
