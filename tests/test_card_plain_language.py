"""信息卡：每个数字都要带一句人话。

2026-08-25 真机反馈。群里有人专门贴了一大段解释「多空比 0.43 是什么意思」——
**那说明卡片自己没说清**。他接着说「查币各方面的信息都不够详细通俗」。

而且同一个数在两个地方说法不一样：`/lsr` 榜里会把 0.43 翻译成
「空头是多头的 2.3 倍」，信息卡却只写「散户偏空」。
"""
from handlers import detail as D


# ── RSI ───────────────────────────────────────────────────────
def test_rsi_gets_words_not_just_a_number():
    """46 到底算高算低？没看过刻度的人一个都读不出来。"""
    assert "超买" in D._rsi_words(None, 75)
    assert "超卖" in D._rsi_words(None, 25)
    assert D._rsi_words(None, 50) == "中性"


def test_rsi_prefers_the_daily_reading():
    """短周期噪音大，以日线为准；日线拿不到才退 4h。"""
    assert D._rsi_words(80, 50) == "中性"       # 有日线就用日线
    assert "超买" in D._rsi_words(80, None)     # 没日线才用 4h


def test_rsi_without_data_says_nothing():
    assert D._rsi_words(None, None) == ""


# ── 持仓量 × 价格 = 四象限 ────────────────────────────────────
def test_open_interest_alone_means_nothing():
    """**持仓量单独看没有意义**——涨了可能是新多进场，也可能是新空进场。
    要和价格方向配着读才知道是谁在进场。卡片以前只印一个金额。"""
    assert "新资金在做多" in D._oi_quadrant(5, 8)
    assert "轧空" in D._oi_quadrant(5, -8)
    assert "新资金在做空" in D._oi_quadrant(-5, 8)
    assert "抛压在释放" in D._oi_quadrant(-5, -8)


def test_flat_market_is_called_flat():
    assert "观望" in D._oi_quadrant(0.1, 0.1)


def test_quadrant_needs_both_inputs():
    """缺一个就不说话——不能拿一半数据编一个结论出来。"""
    assert D._oi_quadrant(None, 5) == ""
    assert D._oi_quadrant(5, None) == ""


# ── 恐惧贪婪 ──────────────────────────────────────────────────
def test_fear_greed_says_what_to_do_with_it():
    """「74（贪婪）」看完还是不知道该干嘛。"""
    assert "追高性价比下降" in D._fng_words(80)
    assert "机会" in D._fng_words(15)


def test_fear_greed_says_it_is_market_wide():
    """它是**全市场**情绪，不是这个币的——不说清会被当成这个币的指标读。"""
    for v in (15, 35, 50, 65, 85):
        assert "全市场" in D._fng_words(v)


def test_fear_greed_survives_junk():
    assert D._fng_words("坏数据") == ""


# ── 多空比：口径必须和 /lsr 一致 ──────────────────────────────
def test_card_reuses_the_same_reading_as_the_lsr_board():
    """同一个数在两个地方说法不一样，是最让人不信任的那种不一致。"""
    import inspect
    src = inspect.getsource(D.build_info_card)
    assert "lsratio" in src, "信息卡没复用 /lsr 的解读，两处会说不同的话"


def test_sub_one_ratio_is_translated_into_a_multiple():
    """群友那段解释就是在做这件事：0.43 → 空头是多头的 2.3 倍。
    小于 1 的比值人脑读不出量级。"""
    from handlers.lsratio import read
    assert read(0.43) == "空头是多头的 2.3 倍"


def test_card_states_the_ratio_is_account_based():
    """账户数 ≠ 持仓金额。不写口径的话，几十个大户和几万个散户看着一样。"""
    import inspect
    assert "账户数" in inspect.getsource(D.build_info_card)
