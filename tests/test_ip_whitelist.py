"""IP 白名单的判定。

真机现象（2026-08-18，实盘 key）：Bybit 返回 `ips: ["*"]`，
/keycheck 显示「IP 白名单: *」，然后打了个绿勾说
「✅ 没有提现权限、且绑了 IP —— 这是交易机器人该有的配置」。

`*` 是「不限制任何 IP」，恰恰是最危险的那种配置。**报平安报反了，比不报还糟**——
他会以为这把 key 是安全的。安全功能的假阳性就是这么产生的。
"""
import pytest

from handlers import keyguard as K


def _info(ips=None, perms=None, expired=None):
    return {"ips": ips if ips is not None else [],
            "permissions": perms or {"ContractTrade": ["Order", "Position"]},
            "expiredAt": expired}


@pytest.mark.parametrize("ips", [["*"], ["*", ""], [" * "], [], None])
def test_wildcard_and_empty_are_not_bound(ips):
    bound, _ = K.ip_bound(_info(ips))
    assert bound is False


@pytest.mark.parametrize("ips", [["47.237.20.192"], ["1.2.3.4", "5.6.7.8"]])
def test_real_addresses_are_bound(ips):
    bound, got = K.ip_bound(_info(ips))
    assert bound is True and got == ips


def test_unbound_key_is_flagged_as_risky():
    _rows, risky, _notes = K._perm_lines(_info(["*"]))
    assert any("没有绑定 IP" in r for r in risky)


def test_unbound_key_shows_the_wildcard_meaning():
    """屏幕上光写一个 * 没人看得懂，要写明它等于不限 IP。"""
    rows, _risky, _notes = K._perm_lines(_info(["*"]))
    line = [r for r in rows if "IP" in r][0]
    assert "未绑定" in line


def test_bound_key_is_not_flagged():
    _rows, risky, _notes = K._perm_lines(_info(["47.237.20.192"]))
    assert not any("IP" in r for r in risky)


def test_withdraw_permission_still_screams():
    _rows, risky, _notes = K._perm_lines(
        _info(["1.2.3.4"], {"Wallet": ["AccountTransfer", "Withdraw"]}))
    assert any("提现权限" in r for r in risky)


def test_extra_permissions_are_a_note_not_a_risk():
    """多勾 Spot/Options 是建议收窄，不该把"配置合格"那句绿勾顶掉。"""
    _rows, risky, notes = K._perm_lines(_info(
        ["1.2.3.4"], {"ContractTrade": ["Order"], "Spot": ["SpotTrade"],
                      "Options": ["OptionsTrade"]}))
    assert risky == []
    assert notes and "Spot" in notes[0] and "Options" in notes[0]


def test_contract_only_key_has_no_notes():
    _rows, risky, notes = K._perm_lines(_info(["1.2.3.4"]))
    assert risky == [] and notes == []
