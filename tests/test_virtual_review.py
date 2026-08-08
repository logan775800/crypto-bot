"""虚拟盘接入复盘层。

「先用虚拟盘练手」这个阶段最需要行为诊断——习惯就是那时候养成的——
可亏损归因、行为画像、周报本来只认实盘，等于在最该用的时候用不上。
"""
import time

import pytest

from handlers import rstats, weekly
from storage import data


def _hist(**kw):
    base = {"sym": "BTC", "side": "long", "lev": 10, "entry": 64000,
            "exit": 65000, "margin": 1000, "pnl": 150, "roe": 15,
            "ts": time.time() - 3600, "dur": 3600, "value": 10000,
            "fee": 5.5, "funding": 1.2, "exit_kind": "manual"}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _acct():
    data["vtrade"] = {"777": {"balance": 10000, "positions": {}, "history": []}}
    yield
    data["vtrade"] = {}


def _put(*rows):
    data["vtrade"]["777"]["history"] = list(rows)


# ── 数据源转换 ───────────────────────────────────────────────────
def test_symbol_gets_usdt_suffix():
    """归因里的 MAJORS 用的是 BTCUSDT 写法，不补后缀会把 BTC 当成山寨小币。"""
    _put(_hist(sym="BTC"))
    assert rstats.load_virtual(777)[0]["symbol"] == "BTCUSDT"


def test_existing_suffix_not_doubled():
    _put(_hist(sym="BTCUSDT"))
    assert rstats.load_virtual(777)[0]["symbol"] == "BTCUSDT"


def test_timestamp_converted_to_ms():
    """实盘那边是毫秒，口径不统一会让时段拆解全错到 1970 年。"""
    ts = time.time() - 3600
    _put(_hist(ts=ts))
    assert rstats.load_virtual(777)[0]["ts"] == int(ts * 1000)


def test_days_filter_applies():
    _put(_hist(ts=time.time() - 40 * 86400), _hist(ts=time.time() - 3600))
    assert len(rstats.load_virtual(777, days=30)) == 1


def test_duration_preserved_for_behavior_tags():
    """没有 dur 就算不出「持仓超一天」「5分钟内被打掉」这些标签。"""
    _put(_hist(dur=90000))
    assert rstats.load_virtual(777)[0]["dur"] == 90000


def test_missing_optional_fields_do_not_crash():
    """老记录没有 dur/value（这些字段是后来才加的），不能因此炸掉。"""
    old = {"sym": "ETH", "side": "short", "lev": 5, "entry": 3000,
           "exit": 2900, "margin": 500, "pnl": 80, "roe": 16, "ts": time.time()}
    _put(old)
    t = rstats.load_virtual(777)[0]
    assert t["value"] == 2500 and t["dur"] is None


def test_funding_aggregated():
    _put(_hist(funding=1.5), _hist(funding=2.5), _hist(sym="ETH", funding=0.5))
    f = rstats.virtual_funding(777)
    assert f["BTC"] == pytest.approx(4.0) and f["ETH"] == pytest.approx(0.5)


def test_empty_account_returns_empty():
    assert rstats.load_virtual(999) == []


# ── 复用整套分析 ─────────────────────────────────────────────────
def test_stats_work_on_virtual_trades():
    _put(_hist(pnl=300), _hist(pnl=-100), _hist(pnl=200))
    s = rstats.compute_stats(rstats.load_virtual(777))
    assert s["n"] == 3 and s["wins"] == 2
    assert s["expectancy"] == pytest.approx(400 / 3)


def test_attribution_works_on_virtual_trades():
    """归因能跑起来才是接入的意义——山寨小币这类标签要能打上。"""
    _put(_hist(sym="AKE", pnl=-500, lev=50, dur=90000))
    rows, total = rstats.attribution(rstats.load_virtual(777))
    tags = {r[0] for r in rows}
    assert total == 500
    assert {"山寨小币", "重杠杆", "持仓超一天"} <= tags


def test_majors_not_tagged_as_altcoin():
    _put(_hist(sym="BTC", pnl=-100, lev=5, dur=600))
    rows, _t = rstats.attribution(rstats.load_virtual(777))
    assert "山寨小币" not in {r[0] for r in rows}


def test_stats_text_renders_for_virtual():
    _put(_hist(pnl=300), _hist(pnl=-100))
    txt = rstats.build_stats_text(rstats.load_virtual(777), 30, {}, "🎮虚拟盘")
    assert "虚拟盘" in txt and "胜率" in txt


def test_weekly_behavior_works_on_virtual():
    _put(_hist(pnl=300, dur=1800, lev=10), _hist(pnl=-100, dur=7200, lev=20))
    b = weekly.behavior(rstats.load_virtual(777))
    assert b["n"] == 2 and b["avg_lev"] == pytest.approx(15)
    assert b["avg_dur"] == pytest.approx(4500)
