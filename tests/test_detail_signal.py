"""行情研判 `_build_signal_text` —— 发币名时那张蜡烛图下面的文案。

`handlers/detail.py` 545 行一直零测试，而它是**发一个币名就会走到**的路径，
用得比任何命令都频繁。

2026-08-25 参考 `D:\\Scripts\\bit` 那个 Go 机器人大改了判定逻辑，这里钉住新规矩：
ADX 从「只印在屏幕上」变成闸门、DI 管方向、背离和早期反转能压制顺势结论。
"""
import asyncio
import io
import random

import pytest

from handlers import detail as D
from handlers.detail import _build_signal_text


def _ohlcv(closes, wick=0.01, vols=None):
    """由收盘价造出 (opens, highs, lows, closes, volumes)。"""
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * (1 + wick) for o, c in zip(opens, closes)]
    lows = [min(o, c) * (1 - wick) for o, c in zip(opens, closes)]
    if vols is None:
        vols = [1000.0] * len(closes)
    return opens, highs, lows, closes, vols


def _uptrend(n=90, rate=1.02):
    c = [100.0]
    for i in range(n):
        c.append(c[-1] * (rate + (0.004 if i % 2 else -0.004)))
    return c


def _counter_trend_bounce():
    """连跌 60 根之后急反抽 6 根——ADX 会很高，而反抽这几根上顺势指标全部翻多。"""
    c = [100.0]
    for _ in range(60):
        c.append(c[-1] * 0.97)
    for _ in range(6):
        c.append(c[-1] * 1.05)
    return c


# ── 基本形状 ──────────────────────────────────────────────────
def test_returns_the_five_core_lines():
    out = _build_signal_text(*_ohlcv(_uptrend()))
    for head in ("综合信号：", "趋势：", "动能：", "量能：", "强度："):
        assert head in out, f"少了「{head}」这一行"


def test_direction_is_shown_next_to_adx():
    """ADX 旁边必须印出 DI 方向。只报强度不报方向，正是老版本会误判的原因。"""
    out = _build_signal_text(*_ohlcv(_uptrend()))
    assert "+DI" in out and "-DI" in out
    assert "多头占优" in out or "空头占优" in out


def test_no_markdown_italics_in_output():
    """这段是图片 caption，中文不能被 Markdown 斜体化（见 tests/test_no_italics.py）。"""
    out = _build_signal_text(*_ohlcv(_uptrend()))
    assert "_" not in out


def test_caption_stays_within_telegram_limit():
    """Telegram 图片 caption 上限 1024 字符，超了整条消息发不出去。

    新加的背离/反转/资金面提示都是条件出现的，最坏情况也要留足余量。
    """
    random.seed(1)
    worst = 0
    for seed in range(80):
        random.seed(seed)
        c = [100.0]
        for _ in range(90):
            c.append(c[-1] * (1 + random.uniform(-0.05, 0.055)))
        out = _build_signal_text(*_ohlcv(c))
        worst = max(worst, len(out))
    assert worst < 900, f"最长的一版已经 {worst} 字，逼近 caption 上限"


# ── 这条是整套改造的核心：逆势反抽不能给强买入 ──────────────────
def test_counter_trend_bounce_is_never_a_strong_buy():
    """老版本在这里会给「买入·强」。

    因为反抽那几根让 MA3>MA13、MACD 红柱走强、RSI 从超卖抬头，分数轻松到 3；
    而 ADX 被前面的猛跌顶到 80 以上，老代码只把它当文案印出来，一分不参与。
    结果就是在下跌中继处喊「趋势较强，可顺势」。
    """
    out = _build_signal_text(*_ohlcv(_counter_trend_bounce()))
    assert "买入信号·强" not in out
    assert "逆势反抽" in out, "不但要降级，还要说清为什么——否则看起来像判据变钝了"


def test_strength_line_never_claims_a_direction():
    """强度那一行不许出现「可顺势」这类带方向的措辞。

    ADX 只测强弱，顺哪边由 DI 说了算。原来写「趋势较强，可顺势」，
    碰上逆势反抽就会一边说可顺势、一边警告别当趋势追——自己跟自己打架，
    而读的人只会记住先出现的那句。
    """
    for closes in (_counter_trend_bounce(), _uptrend(rate=1.03), _uptrend(rate=0.97)):
        out = _build_signal_text(*_ohlcv(closes))
        strength = [ln for ln in out.splitlines() if ln.startswith("强度：")][0]
        assert "顺势" not in strength, f"强度行替 DI 表了态：{strength}"


def test_clean_uptrend_still_allowed_to_be_strong():
    """闸门不能一刀切：方向一致的干净上涨该给强信号还是要给，
    否则这个改造就变成「把所有信号都调弱」，那不叫准确。"""
    out = _build_signal_text(*_ohlcv(_uptrend(rate=1.03)))
    assert "买入信号" in out
    assert "逆势反抽" not in out


def test_choppy_market_is_downgraded():
    """ADX<20 的震荡里，顺势指标会被反复打脸，要降级并说明。"""
    random.seed(4)
    c = [100.0]
    for _ in range(90):
        c.append(c[-1] * (1 + random.uniform(-0.01, 0.01)))
    out = _build_signal_text(*_ohlcv(c))
    if "无明显趋势" in out:
        assert "买入信号·强" not in out and "卖出信号·强" not in out


# ── 量能 / 资金面 ─────────────────────────────────────────────
def test_thin_volume_downgrades():
    """缩量却给强信号是自相矛盾——这条老逻辑不能在改造中丢掉。"""
    c = _uptrend(rate=1.03)
    vols = [1000.0] * (len(c) - 1) + [100.0]      # 最后一根明显缩量
    out = _build_signal_text(*_ohlcv(c, vols=vols))
    assert "缩量" in out


def test_flow_against_the_signal_downgrades():
    """喊多但近一日资金净流出 → 降级并说明还没获得资金面配合。"""
    c = _uptrend(rate=1.03)
    args = _ohlcv(c)
    strong = _build_signal_text(*args)
    against = _build_signal_text(*args, flow={"1d_buy": 10.0, "1d_sell": 900.0})
    assert "资金面" in against
    assert against != strong


def test_flow_agreeing_changes_nothing():
    c = _uptrend(rate=1.03)
    args = _ohlcv(c)
    assert (_build_signal_text(*args)
            == _build_signal_text(*args, flow={"1d_buy": 900.0, "1d_sell": 10.0}))


def test_missing_flow_is_fine():
    """取不到买卖聚合只是少一层确认，不能让整段研判炸掉。"""
    assert _build_signal_text(*_ohlcv(_uptrend()), flow=None)


# ── 位置 ──────────────────────────────────────────────────────
def test_poc_is_reported_on_the_levels_line():
    out = _build_signal_text(*_ohlcv(_uptrend()))
    assert "关键位：" in out
    assert "POC" in out


def test_survives_short_history():
    """数据少到算不出 ADX/MACD 时也不能抛异常——降级给个能看的结论就行。"""
    c = [100.0 + i for i in range(31)]
    out = _build_signal_text(*_ohlcv(c))
    assert "综合信号：" in out


def test_flat_market_does_not_crash():
    out = _build_signal_text(*_ohlcv([100.0] * 60))
    assert "综合信号：" in out


# ── 多周期相册 ────────────────────────────────────────────────
# 周线/日线/4h 三张一条相册（参考 Go 那个机器人：宏观定大势 → 微观找入场）。
# 单看日线的「多头排列」分不清它是周线趋势里的顺势，还是周线跌势里的一次反抽。

def test_okx_bar_names_cover_every_timeframe():
    """OKX 的周期名和币安不一样（1w vs 1W）。**漏一个不会报错**——
    `_OKX_BAR.get(interval, "1D")` 会默默回退成日线，于是"周线图"其实是日线图，
    肉眼根本看不出来。所以这里逐个对。"""
    for tf, _label in D._TF_LABELS:
        assert tf in D._OKX_BAR, f"{tf} 没有 OKX 周期名，会静默退化成日线"


def _fake_ohlcv(n=60):
    return [(i * 86400000, 100.0, 101.0, 99.0, 100.5, 1000.0) for i in range(n)]


def test_multi_charts_returns_none_when_nothing_renders(monkeypatch):
    async def _no_data(sym, interval="1d", limit=120):
        return None
    monkeypatch.setattr(D, "_ohlcv", _no_data)
    assert asyncio.run(D.build_multi_charts("BTC")) is None


def test_multi_charts_skips_timeframes_without_data(monkeypatch):
    """某个周期取不到（新币没有周线）不能让整条相册黄掉，画出几张发几张。"""
    async def _only_daily(sym, interval="1d", limit=120):
        return _fake_ohlcv() if interval == "1d" else None
    monkeypatch.setattr(D, "_ohlcv", _only_daily)
    monkeypatch.setattr(D, "_render_candles",
                        lambda s, i, o: (io.BytesIO(b"png"),
                                         {"o": [1.0], "h": [1.0], "l": [1.0],
                                          "c": [1.0], "v": [1.0]}))
    monkeypatch.setattr(D, "_build_signal_text", lambda *a, **k: "研判")
    out = asyncio.run(D.build_multi_charts("BTC"))
    assert out is not None
    charts, _cap = out
    assert len(charts) == 1, "只有日线有数据时就只该有一张"


def test_multi_charts_labels_which_timeframes_made_it(monkeypatch):
    """相册说明要写清这次到底给了哪几个周期——少一张而不说，
    看的人会以为周线就长这样。"""
    async def _all(sym, interval="1d", limit=120):
        return _fake_ohlcv()
    monkeypatch.setattr(D, "_ohlcv", _all)
    monkeypatch.setattr(D, "_render_candles",
                        lambda s, i, o: (io.BytesIO(b"png"),
                                         {"o": [1.0], "h": [1.0], "l": [1.0],
                                          "c": [1.0], "v": [1.0]}))
    monkeypatch.setattr(D, "_build_signal_text", lambda *a, **k: "研判")
    charts, caption = asyncio.run(D.build_multi_charts("BTC"))
    assert len(charts) == 3
    for _buf, label in charts:
        assert label in caption
    assert "不构成投资建议" in caption


def test_multi_charts_refuses_to_judge_on_the_wrong_timeframe(monkeypatch):
    """日线没取到时**不能拿周线硬算研判**：均线口径 MA3/13/23 是按日线定的语义，
    换个周期同一套阈值说的不是一回事。宁可只给图。"""
    async def _no_daily(sym, interval="1d", limit=120):
        return None if interval == "1d" else _fake_ohlcv()
    monkeypatch.setattr(D, "_ohlcv", _no_daily)
    monkeypatch.setattr(D, "_render_candles",
                        lambda s, i, o: (io.BytesIO(b"png"),
                                         {"o": [1.0], "h": [1.0], "l": [1.0],
                                          "c": [1.0], "v": [1.0]}))
    called = []
    monkeypatch.setattr(D, "_build_signal_text",
                        lambda *a, **k: called.append(1) or "不该被调用")
    _charts, caption = asyncio.run(D.build_multi_charts("BTC"))
    assert not called, "日线缺失时不该用别的周期算研判"
    assert "只给图不给研判" in caption
