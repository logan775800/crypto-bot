"""虚拟合约的盈亏/爆仓价数学——算错会直接误导用户对杠杆风险的认知。"""
from handlers import vtrade as vt


def _pos(side="long", margin=1000.0, lev=10.0, entry=100.0):
    return {"side": side, "margin": margin, "lev": lev, "entry": entry,
            "qty": margin * lev / entry}


def test_pnl_long_and_short():
    long_p = _pos("long")
    # 10x、名义 10000，涨 5% → 盈利 500（ROE +50%）
    assert abs(vt._pnl(long_p, 105) - 500) < 1e-9
    assert abs(vt._pnl(long_p, 95) + 500) < 1e-9       # 跌5% → 亏500

    short_p = _pos("short", margin=500, lev=20)         # 名义 10000
    assert abs(vt._pnl(short_p, 95) - 500) < 1e-9       # 空头跌5% → 赚500
    assert abs(vt._pnl(short_p, 105) + 500) < 1e-9


def test_liq_price_isolated():
    # 逐仓：多头爆仓价 = entry*(1-1/lev)
    assert abs(vt._liq(_pos("long", lev=10)) - 90) < 1e-9
    assert abs(vt._liq(_pos("long", lev=5)) - 80) < 1e-9
    # 空头 = entry*(1+1/lev)
    assert abs(vt._liq(_pos("short", lev=20)) - 105) < 1e-9


def test_liq_means_margin_fully_lost():
    """爆仓价上的浮亏应恰好等于保证金——这是逐仓的定义，两个公式必须自洽。"""
    for side in ("long", "short"):
        for lev in (3, 10, 50):
            p = _pos(side, margin=1000.0, lev=lev)
            loss = vt._pnl(p, vt._liq(p))
            assert abs(loss + p["margin"]) < 1e-6, (side, lev, loss)


def test_fmt_adapts_to_magnitude():
    assert vt.fmt(63123.4) == "63,123.40"
    assert vt.fmt(None) == "?"
    assert "0.0000" in vt.fmt(0.00001234)              # 小币不能显示成 0.00
