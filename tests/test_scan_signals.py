"""扫描的结论层：一行一个币，买卖分开。

他 2026-08-20 甩了一张别人做的扫描截图：「信号扫描·Top100」，
强买入(12)/强卖出(8)，每行是「币 价格 强·多头共振+放量」，一屏 20 个名字。
而我们的 /scan 把每个币铺成 6 行（四个分数 + verdict + 缺失项），
一屏只看得到一个半——**信息密度差了一个量级**。

但我们有它没有的东西：**否决**。盘口薄、费率极端、顺势方向恰是拥挤方的币，
指标再漂亮也下不进去。所以这里守的是：压缩排版，**别把否决一起压没了**。
"""
import pytest

from handlers import scan as S


def _row(symbol="BTCUSDT", aligns=(1, 1, 1), cross=0, vol=1.0, near=None,
         verdict="可以做", total=70, price=100.0):
    tf = {}
    for i, a in enumerate(aligns):
        tf[f"{i+1}h"] = {"align": a, "cross": cross if i == 0 else 0,
                         "vol_ratio": vol if i == 0 else 1.0,
                         "near": near if i == 0 else None}
    return {"symbol": symbol, "price": price, "chg": 1.0, "total": total,
            "verdict": verdict, "tf": tf, "missing": []}


# ── 方向与标签 ──────────────────────────────────────────────
def test_all_timeframes_up_is_a_buy_resonance():
    side, strength, tags = S.signal_of(_row(aligns=(1, 1, 1)))
    assert side == 1 and "多头共振" in tags


def test_all_timeframes_down_is_a_sell():
    side, _s, tags = S.signal_of(_row(aligns=(-1, -1, -1)))
    assert side == -1 and "空头共振" in tags


def test_mixed_timeframes_give_no_signal():
    """1h 多 4h 空的币，哪个方向进去都是在猜——不该出现在名单里。"""
    side, _s, tags = S.signal_of(_row(aligns=(1, -1, 1)))
    assert side == 0 and tags == []


def test_volume_and_cross_and_support_become_tags():
    _side, _s, tags = S.signal_of(
        _row(aligns=(1, 1, 1), cross=1, vol=S.VOL_HOT + 0.1, near="support"))
    assert set(tags) == {"多头共振", "金叉", "放量", "贴支撑"}


def test_strength_needs_several_hits():
    weak = S.signal_of(_row(aligns=(1, 1), cross=0, vol=1.0))[1]
    strong = S.signal_of(_row(aligns=(1, 1), cross=1, vol=S.VOL_HOT))[1]
    assert weak < strong == 2


def test_resistance_tag_only_for_shorts():
    """做多时"贴压力"不是利好，别把方向相反的标签贴上去。"""
    _s, _st, tags = S.signal_of(_row(aligns=(1, 1, 1), near="resist"))
    assert "贴压力" not in tags
    _s2, _st2, tags2 = S.signal_of(_row(aligns=(-1, -1, -1), near="resist"))
    assert "贴压力" in tags2


# ── 排版：一行一个 ──────────────────────────────────────────
def test_one_line_per_coin():
    rows = [_row(f"C{i}USDT", price=1.0 + i) for i in range(5)]
    out = S.render_signals(rows)
    body = out.split("```")[1]
    assert len([x for x in body.strip().split("\n") if x]) == 5


def test_buys_and_sells_are_separated():
    out = S.render_signals([_row("AUSDT", aligns=(1, 1, 1)),
                            _row("BUSDT", aligns=(-1, -1, -1))])
    assert "买入 (1)" in out and "卖出 (1)" in out


def test_strong_ones_come_first():
    weak = _row("WEAKUSDT", aligns=(1, 1), total=90)
    strong = _row("STRONGUSDT", aligns=(1, 1, 1), cross=1, vol=S.VOL_HOT, total=50)
    out = S.render_signals([weak, strong])
    assert out.index("STRONG") < out.index("WEAK"), "强信号要排前面，不看分数"


# ── 别把否决压没了 ──────────────────────────────────────────
def test_vetoed_coins_are_excluded_but_counted():
    """这是我们和"指标共振榜"的根本区别：指标漂亮但下不进去的，
    不能混进名单——但也不能悄悄少给几个，要报数量和理由。"""
    ok = _row("GOODUSDT", aligns=(1, 1, 1))
    bad = _row("THINUSDT", aligns=(1, 1, 1), verdict="不建议（盘口太薄）")
    out = S.render_signals([ok, bad])
    assert "GOOD" in out and "THIN" not in out
    assert "1 个" in out and "剔除" in out


def test_no_signal_says_so_plainly():
    """没有信号本身就是信号，别输出一张空表让人以为坏了。"""
    out = S.render_signals([_row(aligns=(1, -1, 0))])
    assert "没有共振信号" in out


def test_missing_klines_never_becomes_a_signal():
    r = _row()
    r["tf"] = {}
    assert S.signal_of(r)[0] == 0


# ── 接线 ────────────────────────────────────────────────────
def test_compact_is_the_default_and_detail_is_a_button():
    import inspect
    src = inspect.getsource(S.scan_cmd)
    assert "render_signals" in src, "默认要给紧凑版"
    assert "scan:detail" in src, "四维明细收进按钮，不能删"


def test_detail_reuses_the_cached_rows():
    """重扫要 15~30 秒。点"看明细"是想看刚才那批，不是再等半分钟。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "scan:detail":')[1].split("elif d ==")[0]
    assert "scan_rows" in seg and "run(" not in seg


def test_coverage_limit_is_disclosed():
    """只细算了成交额前 N 个——排在后面的币有信号也扫不到。
    不说出来的话，这张名单看起来像"全市场就这几个"。"""
    out = S.render_signals([_row()])
    assert "只看了" in out and "扫不到" in out
