"""合约告警分档去重——KORU 那次每5分钟重报刷屏就是这里出的问题，必须锁死行为。"""
import pytest
from handlers import contract_alert as ca


@pytest.fixture(autouse=True)
def _clean():
    ca.data["contract_tiers"] = {}
    yield
    ca.data["contract_tiers"] = {}


def test_get_tier_steps():
    assert ca.get_tier(19) == 0        # 不到最低档
    assert ca.get_tier(20) == 20
    assert ca.get_tier(94.6) == 90     # 落在 90~100 之间 → 90
    assert ca.get_tier(999) == 400     # 封顶


def test_same_tier_reported_once():
    assert ca.eval_tier_cross("OKX", "KORU", -94.5) == 90     # 首次穿档 → 报
    assert ca.eval_tier_cross("OKX", "KORU", -94.6) is None   # 同档 → 不报
    assert ca.eval_tier_cross("币安", "KORU", -94.7) is None   # 换个所仍同档 → 不报


def test_upgrade_to_higher_tier_reports():
    assert ca.eval_tier_cross("OKX", "X", -25) == 20
    assert ca.eval_tier_cross("OKX", "X", -31) == 30          # 升档 → 报
    assert ca.eval_tier_cross("OKX", "X", -30.5) is None      # 回到同档 → 不报


def test_opposite_direction_does_not_wipe_record():
    """根因回归测试：某个源瞬时报出反向读数，不能把原方向的记录清掉，
    否则下一轮又被当成"首次穿档"重报（旧实现就是这么刷屏的）。"""
    assert ca.eval_tier_cross("OKX", "KORU", -94.5) == 90
    ca.eval_tier_cross("Bybit", "KORU", +25)                  # 反向瞬时读数
    # 原方向仍应被去重，不能再报
    assert ca.eval_tier_cross("OKX", "KORU", -94.6) is None


def test_each_direction_tracked_separately():
    assert ca.eval_tier_cross("OKX", "Y", +25) == 20          # 涨破20
    assert ca.eval_tier_cross("OKX", "Y", -25) == 20          # 跌破20 是另一个事件 → 应报
    assert ca.eval_tier_cross("OKX", "Y", +26) is None        # 涨方向同档 → 不报


def test_falling_below_hysteresis_rearms():
    assert ca.eval_tier_cross("OKX", "Z", 25) == 20
    assert ca.eval_tier_cross("OKX", "Z", 5) is None          # 回落到迟滞带下 → 解除武装
    assert ca.eval_tier_cross("OKX", "Z", 25) == 20           # 重新穿越 → 可以再报


def test_old_record_format_is_upgraded():
    """线上已有旧格式 {tier,dir,ts} 数据，升级后不能因此重报。"""
    import time
    ca.data["contract_tiers"]["OLD"] = {"tier": 90, "dir": "down", "ts": time.time()}
    assert ca.eval_tier_cross("OKX", "OLD", -94.5) is None    # 同档，仍应去重
