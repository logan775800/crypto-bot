"""下单前检查：把 `/checklist` 那张通用清单变成针对这一单的几行。

`/checklist` 写得没错，但它对每一单说的话都一样。真正有用的是
「**你这一单**在费率的付费那边、而且大家都站你这边」——那要拿这个币此刻的
数据去算，不是背诵条款。

分工要清楚：`rtrade._cost_block` 已经算了止损距离/最大亏损/爆仓距离/手续费，
这里只补它没有、而 checklist 点名要看的两条（费率方向、拥挤交易）。
"""
import pytest

from handlers import precheck as P


# ── 年化换算 ──────────────────────────────────────────────────
def test_funding_apr_accounts_for_settlement_frequency():
    """**不换算就没法比大小。**

    同样 1% 的单次费率，1h 结算一天扣 24 次，比 8h 结算狠三倍——
    而屏幕上那两个数字长得一模一样。这是 /checklist 第 ① 条的核心。
    """
    apr_8h = P.funding_apr(0.01, 8)
    apr_1h = P.funding_apr(0.01, 1)
    assert apr_1h == pytest.approx(apr_8h * 8)


def test_funding_apr_needs_both_inputs():
    assert P.funding_apr(None, 8) is None
    assert P.funding_apr(0.01, 0) is None


# ── 你在费率的哪一边 ──────────────────────────────────────────
def test_long_pays_when_funding_is_positive():
    """永续的规矩：费率为正 = 多头付给空头。"""
    hit, txt = P.funding_verdict("long", 0.01, 8)
    assert hit and "付费那边" in txt


def test_short_pays_when_funding_is_negative():
    hit, txt = P.funding_verdict("short", -0.01, 8)
    assert hit and "付费那边" in txt


def test_being_paid_is_only_worth_saying_when_it_is_large():
    """收费的一边不用每次都提——常态费率提了就是噪音。
    大到年化 50% 以上才值得说一句"这单有补贴"。"""
    assert P.funding_verdict("short", 0.001, 8)[0] is False       # 小额，不提
    hit, txt = P.funding_verdict("short", 0.05, 8)                # 年化 ~547%
    assert hit and "站你这边" in txt


def test_high_frequency_settlement_is_called_out():
    """1h 结算 + 你在付费那边 = 一天扣 24 次，这条必须说重话。"""
    hit, txt = P.funding_verdict("long", 0.02, 1)
    assert hit
    assert "每1h结算" in txt
    assert "扛得越久漏得越多" in txt


def test_no_funding_data_says_nothing():
    """**取不到数据不能当成没问题。**
    假的"✅ 通过"会让人放心加仓，比不检查更危险。"""
    assert P.funding_verdict("long", None, 8) == (False, None)


# ── 拥挤交易 ──────────────────────────────────────────────────
def test_crowded_on_your_side_is_a_warning():
    """/checklist 第 ④ 条：大家都在同一边 = 你在补贴对面，反转最惨。"""
    hit, txt = P.crowding_verdict("long", 2.8)
    assert hit
    assert "而你也在多头这边" in txt
    assert "2.8 倍" in txt


def test_crowded_against_you_is_a_plus():
    """散户挤在对面、你站人少的一边——多空比常作反向参考，这算加分。"""
    hit, txt = P.crowding_verdict("short", 2.8)
    assert hit and "人少的一边" in txt


def test_sub_one_ratio_is_translated_into_a_multiple():
    """0.36 这个数人脑读不出量级，必须翻译成「空头是多头的 2.8 倍」。"""
    hit, txt = P.crowding_verdict("short", 1 / 2.8)
    assert hit
    assert "空头账户数是多头的 2.8 倍" in txt


def test_normal_ratio_is_not_worth_mentioning():
    """多数币日常在 0.8~1.3 之间晃，这个区间报出来全是噪音。"""
    assert P.crowding_verdict("long", 1.1)[0] is False
    assert P.crowding_verdict("long", 0.9)[0] is False


def test_no_ratio_data_says_nothing():
    assert P.crowding_verdict("long", None) == (False, None)
    assert P.crowding_verdict("long", 0) == (False, None)


# ── 拼装 ──────────────────────────────────────────────────────
def test_empty_check_prints_nothing():
    """没有可说的就别印。为了"看起来做了检查"而印一行废话，
    下次真有问题时那一段也会被跳过。"""
    assert P.block([]) == ""


def test_block_has_a_heading():
    out = P.block(["• 甲", "• 乙"])
    assert "这一单的检查" in out and "甲" in out and "乙" in out


# ── 接线 ──────────────────────────────────────────────────────
def test_wired_into_both_confirm_pages():
    """实盘确认卡和虚拟盘开仓最后一屏都要有——虚拟盘的意义是手感能迁移，
    两边点法/看到的东西不一样，那点手感也白练。"""
    import inspect
    from handlers import rtrade, vpanel
    assert "precheck" in inspect.getsource(rtrade._precheck_block)
    assert "_precheck_block" in inspect.getsource(rtrade.prepare_open)
    assert "precheck" in inspect.getsource(vpanel.pick_type)


def test_never_blocks_the_order():
    """检查取数失败绝不能挡下单——它是护栏不是依赖。"""
    import inspect
    src = inspect.getsource(P.build)
    assert src.count("except Exception") >= 3
