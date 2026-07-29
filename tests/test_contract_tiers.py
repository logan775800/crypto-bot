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


# ── 每群最低档过滤 + 按钮面板 ──────────────────────────────────────
def test_min_tier_default_and_custom():
    ca.data["contract_min_tier"] = {}
    assert ca._min_tier(555) == 20                     # 默认全收
    ca.data["contract_min_tier"] = {"555": 50}
    assert ca._min_tier(555) == 50


def test_push_filters_by_per_chat_min_tier():
    """同一批告警，不同群按各自最低档收到不同内容——不能因过滤丢了该收的。"""
    import asyncio
    ca.data["contract_watch"] = [100, 200]
    ca.data["contract_min_tier"] = {"100": 20, "200": 50}
    ca.data["contract_alerted"] = {}
    sent = {}

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.setdefault(chat_id, []).append(text)

    alerts = [
        {"ex": "OKX", "sym": "AAA", "change": 22, "price": 1, "tier": 20, "direction": "up"},
        {"ex": "币安", "sym": "BBB", "change": 55, "price": 2, "tier": 50, "direction": "up"},
    ]
    asyncio.run(ca.push_to_subscribers(Bot(), alerts))
    txt100 = "".join(sent.get(100, []))
    txt200 = "".join(sent.get(200, []))
    assert "AAA" in txt100 and "BBB" in txt100          # 全收
    assert "AAA" not in txt200 and "BBB" in txt200      # 只要≥50%
    # 清理，避免污染别的用例
    ca.data["contract_watch"] = []
    ca.data["contract_min_tier"] = {}
    ca.data["contract_alerted"] = {}


class _FakeQuery:
    def __init__(self, d, cid=555):
        self.data = d
        self.message = type("M", (), {"chat": type("C", (), {"id": cid})()})()
        self.edited = None
        self.toast = None

    async def answer(self, text=None, **kw):
        self.toast = text

    async def edit_message_text(self, text, **kw):
        self.edited = text


def test_panel_tier_button_subscribes_and_sets(monkeypatch):
    import asyncio
    async def fake_edit(q, text, **kw):
        q.edited = text
    monkeypatch.setattr(ca, "safe_edit", fake_edit)
    ca.data["contract_watch"] = []
    ca.data["contract_min_tier"] = {}
    q = _FakeQuery("ctr:tier:30")
    asyncio.run(ca.on_button(q, None))
    assert 555 in ca.data["contract_watch"]            # 选档即订阅
    assert ca.data["contract_min_tier"]["555"] == 30
    assert "已订阅" in q.edited
    ca.data["contract_watch"] = []
    ca.data["contract_min_tier"] = {}


def test_panel_off_unsubscribes(monkeypatch):
    import asyncio
    async def fake_edit(q, text, **kw):
        q.edited = text
    monkeypatch.setattr(ca, "safe_edit", fake_edit)
    ca.data["contract_watch"] = [555]
    q = _FakeQuery("ctr:off")
    asyncio.run(ca.on_button(q, None))
    assert 555 not in ca.data["contract_watch"] and "555" not in [str(s) for s in ca.data["contract_watch"]]
    assert "未订阅" in q.edited


# ── 自检 / 立即补报 ────────────────────────────────────────────────
def test_alertnow_bypasses_dedup(monkeypatch):
    """/alertnow 必须无视 contract_tiers 去重，把当前异动全推出来。"""
    import asyncio
    fake = [{"ex": "OKX", "sym": "AAA", "change": 22, "price": 1, "tier": 20, "direction": "up"},
            {"ex": "币安", "sym": "BBB", "change": 55, "price": 2, "tier": 50, "direction": "up"}]

    async def fetch():
        return list(fake)
    monkeypatch.setattr(ca, "_fetch_all_movers", fetch)
    # 即使去重表已记满，alertnow 也照发
    ca.data["contract_tiers"] = {"AAA": {"up": 20, "ts": 9e18}, "BBB": {"up": 50, "ts": 9e18}}
    ca.data["contract_min_tier"] = {"555": 20}
    sent = []

    async def reply(t, **kw):
        sent.append(t)
    asyncio.run(ca._do_alert_now(555, reply))
    blob = "".join(sent)
    assert "AAA" in blob and "BBB" in blob
    ca.data["contract_tiers"] = {}


def test_alertnow_respects_min_tier(monkeypatch):
    import asyncio
    fake = [{"ex": "OKX", "sym": "AAA", "change": 22, "price": 1, "tier": 20, "direction": "up"},
            {"ex": "币安", "sym": "BBB", "change": 55, "price": 2, "tier": 50, "direction": "up"}]

    async def fetch():
        return list(fake)
    monkeypatch.setattr(ca, "_fetch_all_movers", fetch)
    ca.data["contract_min_tier"] = {"555": 50}       # 只要≥50%
    sent = []

    async def reply(t, **kw):
        sent.append(t)
    asyncio.run(ca._do_alert_now(555, reply))
    blob = "".join(sent)
    assert "BBB" in blob and "AAA" not in blob
    ca.data["contract_min_tier"] = {}


def test_diag_splits_deduped_vs_fresh(monkeypatch):
    """自检必须正确区分「已报过·去重中」和「新的·下轮会推」——这是解释安静的核心。"""
    import asyncio
    import time
    fake = [{"ex": "OKX", "sym": "AAA", "change": 22, "price": 1, "tier": 20, "direction": "up"},
            {"ex": "币安", "sym": "BBB", "change": 55, "price": 2, "tier": 50, "direction": "up"}]

    async def fetch():
        return list(fake)
    monkeypatch.setattr(ca, "_fetch_all_movers", fetch)
    ca.data["contract_watch"] = [555]
    ca.data["contract_min_tier"] = {"555": 20}
    ca.data["pump_watch"] = {}
    # AAA 已报过(去重中)，BBB 是新的
    ca.data["contract_tiers"] = {"AAA": {"up": 20, "ts": time.time()}}
    sent = []

    async def reply(t, **kw):
        sent.append(t)
    asyncio.run(ca._do_alert_diag(555, reply))
    out = sent[0]
    assert "已报过·去重中：1" in out and "新的·下轮会推：1" in out
    ca.data["contract_tiers"] = {}
    ca.data["contract_watch"] = []
