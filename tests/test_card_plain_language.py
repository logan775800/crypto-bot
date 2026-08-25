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


# ── 缺失的块必须说明原因 ──────────────────────────────────────
# 2026-08-25 他第二次抱怨「查币信息不够详细」。核对下来我加的解读**已经上线了**，
# 薄的真因是**整块数据消失而卡片一个字不提**：
# 同一个机器人查 AKE 有 8 行市值数据、查 TUT 一行都没有，看起来就像"功能很少"。

def test_missing_market_cap_block_explains_itself():
    """CoinGecko 没收录 / 价格交叉校验没过 / 被限频——三种都会让整块消失。
    不说的话，用户以为这机器人就这点信息。"""
    import inspect
    src = inspect.getsource(D.build_info_card)
    assert "市值/排名/多周期涨跌: 暂缺" in src
    assert "没收录" in src and "限频" in src


def test_missing_flow_block_explains_itself():
    """四所缺一家就整块不显示，以前一个字不提。"""
    import inspect
    src = inspect.getsource(D.build_info_card)
    assert "全市场买卖估算: 暂缺" in src
    assert "四家现货" in src


def test_absence_never_looks_like_a_missing_feature():
    """通用规矩：**"这次没有"和"没这功能"在屏幕上必须长得不一样。**
    这是这个项目反复栽的同一件事（反转扫描、LP 告警、这次的市值块）。"""
    import inspect
    src = inspect.getsource(D.build_info_card)
    assert src.count("暂缺") >= 2


# ── 白拿的推导值 ──────────────────────────────────────────────
def test_basis_is_computed_from_prices_already_on_the_card():
    """现货价和合约价卡片上本来就有，基差是白拿的一层情绪信息：
    合约溢价 = 有人愿意付钱做多。不用多打一次接口。"""
    import inspect
    src = inspect.getsource(D.build_info_card)
    assert "基差" in src and "合约溢价" in src and "合约折价" in src


def test_low_float_is_flagged():
    """低流通 = 大部分币还锁着，解锁就是持续抛压。"""
    import inspect
    src = inspect.getsource(D.build_info_card)
    assert "流通率" in src and "抛压" in src


def test_turnover_flags_both_extremes():
    """换手太低=进出有滑点，太高=多半在刷量或被爆炒。两头都要提。"""
    import inspect
    src = inspect.getsource(D.build_info_card)
    assert "换手率" in src
    assert "刷量" in src and "滑点" in src
