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
         verdict="可以做", total=70, price=100.0, near_tight=None):
    tf = {}
    for i, a in enumerate(aligns):
        tf[f"{i+1}h"] = {"align": a, "cross": cross if i == 0 else 0,
                         "vol_ratio": vol if i == 0 else 1.0,
                         "near": near if i == 0 else None,
                         "near_tight": near_tight if i == 0 else None}
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
                            _row("BUSDT", aligns=(-1, -1, -1))],
                           source="Bybit永续")
    assert "做多 (1)" in out and "做空 (1)" in out


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
    assert "没有信号本身就是信号" in out and "这一轮没有" in out


def test_missing_klines_never_becomes_a_signal():
    r = _row()
    r["tf"] = {}
    assert S.signal_of(r)[0] == 0


# ── 接线 ────────────────────────────────────────────────────
def test_compact_is_the_default_and_detail_is_a_button():
    import inspect
    src = inspect.getsource(S.scan_cmd)
    assert "render_signals" in src, "默认要给紧凑版"
    cbs = [b.callback_data for row in S.result_kb().inline_keyboard for b in row]
    assert "scan:detail" in cbs, "四维明细收进按钮，不能删"


def test_detail_reuses_the_cached_rows():
    """重扫要 15~30 秒。点"看明细"是想看刚才那批，不是再等半分钟。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "scan:detail":')[1].split("elif d ==")[0]
    assert "scan_rows" in seg and "run(" not in seg


def test_coverage_limit_is_disclosed():
    """打了多少标签、细算了几个、只扫哪个盘子——三个都要写在脸上。
    不说的话，这张名单看起来像"全市场就这几个"。"""
    r = _row()
    r["scanned"] = 39
    out = S.render_signals([r], source="Bybit永续")
    assert "39" in out and "细算" in out
    assert "不含现货" in out


# ── 三个真机 bug（2026-08-20 他一眼看出"不太对劲"）────────────
def test_volume_uses_the_closed_bar_not_the_running_one():
    """最后一根还在走，量只累积了一部分。拿它比均量必然偏低——
    实测 15m 的比值全在 0.13~0.67，「放量」这标签在短周期上永远不触发。"""
    import inspect
    src = inspect.getsource(S._tf_snapshot)
    assert "vol[-2]" in src, "要用已收盘的那根"
    assert "vol[-1] /" not in src


def test_cross_is_checked_on_every_timeframe():
    """真机：XLM 的 4h 和 1h 双双金叉，因为 15m 没交叉就整个丢了。
    大周期的金叉本来比小周期更有分量。"""
    r = _row(aligns=(1, 1, 1))
    # 只让最长的那个周期金叉（构造里 index 0 才是 cross，所以手工改）
    keys = list(r["tf"])
    for k in keys:
        r["tf"][k]["cross"] = 0
    r["tf"][keys[-1]]["cross"] = 1
    _s, _st, tags = S.signal_of(r)
    assert "金叉" in tags


def test_volume_is_checked_on_every_timeframe():
    r = _row(aligns=(1, 1, 1))
    keys = list(r["tf"])
    for k in keys:
        r["tf"][k]["vol_ratio"] = 1.0
    r["tf"][keys[-1]]["vol_ratio"] = S.VOL_HOT + 0.5
    assert "放量" in S.signal_of(r)[2]


def test_a_long_sitting_under_resistance_is_flagged_not_dropped():
    """真机：XRP/XLM/ENA 都贴着压力位却在做多名单里，而这条被默默丢掉了。
    多头正贴压力却什么都不提，正是最容易让人追在高点的那种沉默。"""
    _s, _st, tags = S.signal_of(
        _row(aligns=(1, 1, 1), near="resist", near_tight="resist"))
    assert any("上方有压力" in t for t in tags)


def test_a_short_sitting_above_support_is_flagged():
    _s, _st, tags = S.signal_of(
        _row(aligns=(-1, -1, -1), near="support", near_tight="support"))
    assert any("下方有支撑" in t for t in tags)


def test_risk_tag_alone_is_not_a_signal():
    """只剩一个风险标签，不该被当成"有信号"塞进名单。"""
    r = _row(aligns=(1, 1, 0), near="resist", near_tight="resist")
    assert S.signal_of(r)[0] == 0


def test_risk_tag_needs_a_much_tighter_distance():
    """上涨趋势里价格本来就贴着近期高点——用同一个阈值的话 10 个信号 6 个都挂，
    不区分就是噪音，而噪音会让人连真正该看的警示一起忽略。"""
    loose = S.signal_of(_row(aligns=(1, 1, 1), near="resist"))[2]
    assert not any("上方有压力" in t for t in loose), "只是靠近不该报警"
    tight = S.signal_of(_row(aligns=(1, 1, 1), near="resist",
                             near_tight="resist"))[2]
    assert any("上方有压力" in t for t in tight)
    assert S.RISK_ATR < S.SUPPORT_ATR


def test_veto_reason_is_the_actual_reason():
    """verdict 是「❌ 不建议（盘口太薄）」——要括号里那半句。
    只显示"❌ 不建议"等于没说理由。"""
    bad = _row("THINUSDT", aligns=(1, 1, 1), verdict="❌ 不建议（盘口太薄）")
    out = S.render_signals([_row("OKUSDT", aligns=(1, 1, 1)), bad])
    assert "盘口太薄" in out


# ── 他 2026-08-20 的两条批评 ─────────────────────────────────
def test_perp_says_long_short_not_buy_sell():
    """永续能双向开仓——用"买入/卖出"会让人以为只能做多。"""
    out = S.render_signals([_row(aligns=(1, 1, 1))], source="Bybit永续")
    assert "做多" in out and "买入" not in out


def test_spot_still_says_buy_sell():
    out = S.render_signals([_row(aligns=(1, 1, 1))], source="Bybit")
    assert "买入" in out


def test_empty_side_is_printed_not_hidden():
    """空组整段消失，读起来像"没扫做空"。而"当前没有空头共振"本身就是信息。"""
    out = S.render_signals([_row(aligns=(1, 1, 1))], source="Bybit永续")
    assert "做空 (0)" in out and "这一轮没有" in out


def test_scope_is_spelled_out():
    """他的原话：「这个只是扫描 bybit 合约的一个片面 不是现货也不是其它交易所」。"""
    out = S.render_signals([_row()], source="Bybit永续")
    assert "不含现货" in out and "其它交易所" in out


def test_switching_venue_is_a_button_on_the_result():
    """能力一直有（run() 认 source 标签），但埋在 /source 全局设置里等于没有。"""
    cbs = [b.callback_data for row in S.result_kb().inline_keyboard for b in row]
    assert "scan:src" in cbs
    labels = [b.callback_data for row in S.source_kb().inline_keyboard for b in row]
    assert any("永续" in c for c in labels), "要能选永续"
    assert len([c for c in labels if c.startswith("scan:on:")]) >= 6


def test_coverage_numbers_are_reported():
    """打了多少标签 / 细算了几个，两个数都要报——只报一个会让人以为全扫了。"""
    r = _row()
    r["scanned"] = 39
    out = S.render_signals([r])
    assert "39" in out


def test_two_stage_pipeline_exists():
    """按成交额取前 16 个做全套细算 = 排第 20 位但三周期共振的币根本轮不到被看一眼。"""
    import inspect
    src = inspect.getsource(S.run)
    assert "_lite" in src and "signal_of" in src
    assert "pre_tf" in src, "便宜段取过的周期要复用，别再打一遍"
