"""两段对比：这一段 vs 上一段行情。

起因是他贴的那份 BTR 分析。我照着去核数，发现每个数字都能在 Gate 的
`contract_stats` 里对上，而 `/pos` 当时给不出其中四样：
爆仓量、账户数原值、自定义窗口、两段对比。

最关键的是**两段对比**——「爆仓引擎熄火了」这个结论单看当前那一段
是看不出来的：$12k 的爆空量本身既不高也不低，只有和上一段一比
才知道是"熄火"还是"从来就没着过"。
"""
import pytest

from handlers import posflow as P


def bar(oi, ll=0.0, sl=0.0, lsr=1.0, top=1.0, lu=100, su=100, fr=0.0001):
    return {"open_interest": oi, "open_interest_usd": oi * 10,
            "long_liq_usd": ll, "short_liq_usd": sl,
            "lsr_account": lsr, "top_lsr_size": top,
            "long_users": lu, "short_users": su, "last_funding_rate": fr}


def flat_rows(n=13, base=3_800_000):
    """横盘：来回震荡，涨跌次数接近对半，振幅小。"""
    out = []
    for i in range(n):
        out.append(bar(base + (150_000 if i % 2 else -150_000)))
    return out


def trend_rows(n=13, base=700_000):
    """单边扩张：一路涨上去。"""
    return [bar(int(base * (1.15 ** i))) for i in range(n)]


# ── 横盘 vs 趋势 ────────────────────────────────────────────
def test_flat_is_not_just_a_small_endpoint_difference():
    """**只看首尾差会把来回震荡读成"没动"**。真正区分横盘的是
    振幅小 *且* 涨跌次数接近对半（= 没有一方在净建仓）。
    他那份分析第一条就是这个：价格振了 8.6%，持仓却毫无净变化。"""
    s = P.seg_stats(flat_rows())
    assert P.is_flat(s)
    assert s["amp"] < P.FLAT_AMP
    assert abs(s["up"] - s["dn"]) <= 2


def test_one_sided_expansion_is_not_flat():
    s = P.seg_stats(trend_rows())
    assert not P.is_flat(s)
    assert s["oi_pct"] > 100


def test_a_ramp_with_small_amplitude_is_still_a_trend():
    """振幅小但一路单边涨 —— 那是慢速建仓，不是横盘。
    只卡振幅不看涨跌次数的话会判错。"""
    rows = [bar(1_000_000 + i * 6_000) for i in range(13)]
    s = P.seg_stats(rows)
    assert s["amp"] < P.FLAT_AMP
    assert not P.is_flat(s), "单边慢涨被当成横盘了"


def test_too_few_bars_is_not_called_flat():
    """两三根数据判不出形态，宁可不说。"""
    assert not P.is_flat(P.seg_stats([bar(100), bar(101), bar(100)]))


# ── 爆仓引擎：只能靠两段对比 ─────────────────────────────────
def test_squeeze_engine_dying_needs_the_previous_segment():
    """真机数据：这一段每小时爆仓 $1,727，上一段 $21,460，塌了 12 倍。"""
    now = P.seg_stats([bar(1e6, ll=800, sl=800) for _ in range(13)])
    prev = P.seg_stats([bar(1e6, ll=4000, sl=17000) for _ in range(39)])
    v = P.squeeze_verdict(now, prev)
    assert v and "熄火" in v
    assert "爆空" in v, "没说清上一段是靠哪一边推的"


def test_no_verdict_without_a_previous_segment():
    now = P.seg_stats([bar(1e6, ll=800, sl=800) for _ in range(13)])
    assert P.squeeze_verdict(now, None) is None


def test_no_verdict_when_the_previous_segment_had_no_engine_either():
    """上一段本来就没爆仓，"熄火"无从谈起——这种要闭嘴，不能报个假结论。"""
    now = P.seg_stats([bar(1e6, ll=1, sl=1) for _ in range(13)])
    prev = P.seg_stats([bar(1e6, ll=2, sl=2) for _ in range(39)])
    assert P.squeeze_verdict(now, prev) is None


def test_comparison_is_per_hour_not_total():
    """两段长度不一样（13h vs 39h），比总额的话短的那段天然更小，
    再平静的行情都会被判成"熄火"。"""
    now = P.seg_stats([bar(1e6, ll=500, sl=500) for _ in range(13)])
    prev = P.seg_stats([bar(1e6, ll=500, sl=500) for _ in range(39)])
    assert P.squeeze_verdict(now, prev) is None, "按总额比了，短段被冤枉"


def test_a_dump_engine_is_described_as_such():
    """上一段主要在爆多 = 那波是多杀多砸下来的，不是轧空。
    两种情况文案不能一样。"""
    now = P.seg_stats([bar(1e6, ll=400, sl=400) for _ in range(13)])
    prev = P.seg_stats([bar(1e6, ll=20000, sl=1000) for _ in range(39)])
    v = P.squeeze_verdict(now, prev)
    assert v and "爆多" in v and "多杀多" in v


# ── 账户数原值 ──────────────────────────────────────────────
def test_raw_account_counts_disambiguate_what_the_ratio_cannot():
    """比值从 0.37 掉到 0.32，可能是多头跑了，也可能是空头进得更多。
    两个原值一摆就没有歧义——这正是 Gate 有而币安没有的那一栏。"""
    s = P.seg_stats([bar(1e6, lu=844, su=2278), bar(1e6, lu=680, su=2158)])
    txt = P.who_left(s)
    assert "844" in txt and "680" in txt
    assert "2278" in txt and "2158" in txt
    assert "两边同时离场" in txt and "多头跑得更快" in txt


def test_who_left_stays_quiet_when_nobody_moved():
    s = P.seg_stats([bar(1e6, lu=100, su=100), bar(1e6, lu=102, su=101)])
    assert P.who_left(s) is None


@pytest.mark.parametrize("lu,su,want", [
    ((100, 300), (100, 50), "多头在进、空头在退"),
    ((300, 100), (50, 100), "多头在退、空头在进"),
])
def test_who_left_directions(lu, su, want):
    s = P.seg_stats([bar(1e6, lu=lu[0], su=su[0]), bar(1e6, lu=lu[1], su=su[1])])
    assert want in P.who_left(s)


# ── 大户稳 / 散户动 ─────────────────────────────────────────
def test_big_money_holding_while_retail_runs():
    """那份分析第 4 条的落点：散户比挪了但大户比稳在 1.32~1.38 没动
    → 大户资金没走，走的是小账户。两个口径不同（金额 vs 人头），
    **可以同时成立且不矛盾**。"""
    s = P.seg_stats([bar(1e6, lsr=0.3705, top=1.38),
                     bar(1e6, lsr=0.3151, top=1.34)])
    v = P.big_vs_small(s)
    assert v and "大户资金没走" in v


def test_big_money_moving_first_is_also_called_out():
    s = P.seg_stats([bar(1e6, lsr=1.00, top=1.00),
                     bar(1e6, lsr=1.02, top=1.40)])
    v = P.big_vs_small(s)
    assert v and "大钱在调仓" in v


def test_no_claim_when_both_moved_or_neither_did():
    both = P.seg_stats([bar(1e6, lsr=1.0, top=1.0), bar(1e6, lsr=1.5, top=1.5)])
    assert P.big_vs_small(both) is None
    neither = P.seg_stats([bar(1e6, lsr=1.0, top=1.0),
                           bar(1e6, lsr=1.01, top=1.01)])
    assert P.big_vs_small(neither) is None


# ── 分段口径 ────────────────────────────────────────────────
def test_previous_segment_is_longer_than_the_current_one():
    """**对比段不等长是刻意的**：13 小时横盘的前面是 47 小时单边扩张，
    等长对比会把那 47 小时硬切成 13 小时，"OI +450%"这个量级就没了。"""
    import inspect
    src = inspect.getsource(P.fetch_segments)
    assert "prev_mult" in src and "mult = prev_mult if prev_mult is not None else 3" in src


def test_seg_stats_needs_at_least_two_bars():
    assert P.seg_stats([]) is None
    assert P.seg_stats([bar(1e6)]) is None


def test_seg_stats_survives_garbage_fields():
    """接口偶尔给 null/空串，不能整段炸掉。"""
    rows = [{"open_interest": None}, {"open_interest": "1000"},
            {"open_interest": "1100", "long_liq_usd": "", "lsr_account": None}]
    s = P.seg_stats(rows)
    assert s is not None and s["oi_first"] == 1000


# ── 渲染 ────────────────────────────────────────────────────
def _fake_g(now_rows, prev_rows, hours=13):
    return {"sym": "BTR", "hours": hours, "prev_hours": len(prev_rows),
            "now": P.seg_stats(now_rows), "prev": P.seg_stats(prev_rows),
            "src": "Gate"}


def test_card_leads_with_the_flat_verdict():
    """结论在最上面，数字在下面——他定过的列表版式。
    而且横盘这条必须排第一：后面所有解读都建立在它上面。"""
    g = _fake_g(flat_rows(), [bar(1e6, ll=4000, sl=17000) for _ in range(39)])
    out = P.gate_lines(g, chg=8.6)
    assert out[0].startswith("→")
    assert "横盘" in out[0] and "净建仓" in out[0]


def test_card_contrasts_with_the_previous_regime():
    """「而前 47 小时 OI +450%，是完全不同的状态」——
    横盘这个判断只有配上"前面那段不是这样"才有分量。"""
    g = _fake_g(flat_rows(), trend_rows(39, 100_000))
    txt = "\n".join(P.gate_lines(g, chg=8.6))
    assert "完全不同的状态" in txt


def test_card_shows_both_segments_liquidations():
    g = _fake_g([bar(1e6, ll=863, sl=863) for _ in range(13)],
                [bar(1e6, ll=4000, sl=17000) for _ in range(39)])
    txt = "\n".join(P.gate_lines(g))
    assert "爆仓" in txt and "前 39 小时是" in txt


def test_card_states_its_source_and_what_the_fallback_lacks():
    """Gate 有爆仓量和账户数，币安没有。走了哪条路必须写脸上，
    否则同一个命令在不同币上给出的信息量不一样，看的人会以为漏了。"""
    import inspect
    src = inspect.getsource(P.build_text)
    assert "数据源 Gate" in src
    assert "没有爆仓量和账户数原值" in src


# ── 窗口 ────────────────────────────────────────────────────
@pytest.mark.parametrize("args,want", [
    (["BTR", "13"], 13),
    (["BTR", "48h"], 48),
    (["BTR", "13小时"], 13),
    (["BTR"], P.DEFAULT_HOURS),
    (["BTR", "abc"], P.DEFAULT_HOURS),
    (["BTR", "0"], P.DEFAULT_HOURS),
    (["BTR", "99999"], P.DEFAULT_HOURS),
])
def test_window_argument(args, want):
    assert P.parse_hours(args) == want


def test_window_buttons_exist_and_mark_the_current_one():
    """固定 24 小时会把两段行情糊成一段。窗口必须能一键换。"""
    cbs = [b.callback_data for row in P.kb("BTR", 13).inline_keyboard for b in row]
    assert "pf:h13:BTR" in cbs and "pf:h24:BTR" in cbs
    texts = [b.text for row in P.kb("BTR", 13).inline_keyboard for b in row]
    assert "✅13h" in texts


def test_window_callback_is_routed():
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert 'act.startswith("h")' in src


def test_rows_are_shorter_than_the_button_squeeze_limit():
    """消息太长按钮会被挤出屏幕（他反馈过）。"""
    g = _fake_g(flat_rows(), [bar(1e6, ll=4000, sl=17000) for _ in range(39)])
    assert len(P.gate_lines(g, 8.6)) <= 16


def test_zero_liquidations_this_segment_does_not_crash():
    """冷门币或纯横盘时，这一段可能**一笔爆仓都没有**——很常见，不是异常。
    第一版直接算 1/塌落比例，除零当场崩。"""
    now = P.seg_stats([bar(1e6, ll=0, sl=0) for _ in range(13)])
    prev = P.seg_stats([bar(1e6, ll=4000, sl=17000) for _ in range(39)])
    v = P.squeeze_verdict(now, prev)
    assert v and "一笔都没有了" in v


# ── 跨所分歧 ────────────────────────────────────────────────
# 实测区分度 3.27x（异动组极差 20.3pt vs 基线 6.2pt），是量过的六个候选里
# 最强的一个，而且和持仓量本身正交。另外四个（主动买卖盘、合约/现货倍数、
# 现货主动买入占比、大单占比）实测 0.58~0.99x，全是噪音，**刻意没加**。
def test_all_venues_agreeing_says_so():
    v, d = P.venue_verdict({"bn": 12.0, "by": 10.0, "gate": 9.0})
    assert v and "一致" in v
    assert "币安" in d and "Bybit" in d and "Gate" in d


def test_one_venue_carrying_the_whole_move():
    """真机：龙虾 +81.7%，币安持仓 +117.5% 而 Gate 只有 +2.5%。
    这波全是币安一家的杠杆在推——和三家一起加仓是完全不同的东西。"""
    v, _d = P.venue_verdict({"bn": 117.5, "gate": 2.5})
    assert v and "一家的杠杆在推" in v and "脆" in v


def test_venues_disagreeing_on_direction():
    """真机 BICO：币安 -10.2%、Bybit +7.0%、Gate -28.3%。
    一边加一边减 = 不是同一批人在做同一件事。"""
    v, _d = P.venue_verdict({"bn": -10.2, "by": 7.0, "gate": -28.3})
    assert v and "方向都不一致" in v


def test_one_venue_is_not_a_divergence():
    """只有一家数据谈不上"分歧"，闭嘴比编一个结论强。"""
    assert P.venue_verdict({"bn": 50.0}) == (None, None)
    assert P.venue_verdict({}) == (None, None)
    assert P.venue_verdict(None) == (None, None)


def test_flat_verdict_defers_to_another_venue_exploding():
    """**横盘不能只看主源那一家**。真机：龙虾涨 79%，Gate 持仓只动 2.3%，
    卡片印出「横盘，没有一方在净建仓」——而同一时间币安持仓 +120%。
    仓位确实在净增，只是增在另一家。"""
    g = _fake_g(flat_rows(24), [bar(1e6) for _ in range(72)], hours=24)
    txt = "\n".join(P.gate_lines(g, chg=79.3, venues={"bn": 120.0, "gate": 2.3}))
    assert "没有一方在净建仓" not in txt, "一家横盘就下了全局结论"
    assert "不在 Gate 发生" in txt


def test_flat_verdict_stands_when_no_venue_moved():
    g = _fake_g(flat_rows(24), [bar(1e6) for _ in range(72)], hours=24)
    txt = "\n".join(P.gate_lines(g, chg=0.5, venues={"bn": 1.0, "gate": -2.0}))
    assert "没有一方在净建仓" in txt


# ── 爆仓量分位：参照系，不是判据 ────────────────────────────
def test_liq_percentile_translates_an_unreadable_number():
    """「这段爆了 $11k」本身读不出多少——对 BTC 是零头，对小币是天量。
    实测区分度只有 1.28x，所以它是**参照系不是判据**：
    只把数字翻译成高/低，不用来下结论。"""
    assert "94 分位" in P.liq_words({"pct": 94.0, "rate": 1727, "days": 30})
    assert "最高的那 5%" in P.liq_words({"pct": 97.0, "rate": 9, "days": 30})
    assert "低于日常" in P.liq_words({"pct": 12.0, "rate": 9, "days": 30})
    assert P.liq_words(None) is None


# ── 账户数四种组合要凑齐 ────────────────────────────────────
def test_both_sides_piling_in_is_not_a_missing_line():
    """真机漏掉的分支：龙虾多头 +230、空头 +737，两边都在进场，
    结果这一行整个消失了——看的人会以为没数据。"""
    s = P.seg_stats([bar(1e6, lu=296, su=280), bar(1e6, lu=526, su=1017)])
    txt = P.who_left(s)
    assert txt and "两边都在进场" in txt and "空头进得更猛" in txt


def test_venue_change_uses_rates_not_absolutes():
    """三家的持仓单位不一样（币安给美元名义值、Bybit 和 Gate 给张数），
    只能比变化率。这条锁的是"别哪天顺手改成比绝对值"。"""
    import inspect
    src = inspect.getsource(P.venue_oi)
    assert "只能比变化率不能比绝对值" in src
    assert "倒序" in src, "Bybit 是最新在前，索引反了会算出相反的变化"


# ── 减掉的仓：自己走的还是被打爆的 ──────────────────────────
# 另一份 BTR 分析里说「爆仓量很小，说明大部分是主动平仓」。除法本身对，
# 但实测（35 币 / 30 天 / 1107 个 24h 减仓窗口）结论**和那句话相反**：
#   强平占比 <5% 的占 73.7%，中位数才 1.9%
# **主动平仓才是减仓的常态**，所以那句话在描述默认状态，不是洞察。
# 有信息量的是反过来——强平占比高的窗口跌得更狠（-2.8% vs -1.0%）。
def _seg(oi_a, oi_b, liq, usd=None):
    """oi_* 是张数；bar() 里 open_interest_usd = 张数×10，所以换算率恒为 10。"""
    rows = [bar(oi_a, ll=liq / 2, sl=liq / 2), bar(oi_b)]
    return P.seg_stats(rows)


def test_normal_deleveraging_stays_quiet():
    """73.7% 的减仓窗口强平占比都在 5% 以下。每次都印一句
    「主要是主动平仓」等于每次都说废话——常态必须闭嘴。"""
    s = _seg(1_000_000, 800_000, liq=20_000)      # 20万美元被平，2万被强平 = 1%
    assert P.forced_words(P.forced_share(s)) is None


def test_forced_deleveraging_speaks_up_with_how_rare_it_is():
    """强平占相当一部分时才出声，并且要带上"这种情况只占百分之几"
    ——不给稀有度，看的人不知道自己看见的是不是异常。"""
    s = _seg(1_000_000, 500_000, liq=1_500_000)   # 500万被平，150万被强平 = 30%
    t = P.forced_words(P.forced_share(s))
    assert t and "被强平" in t
    assert "%" in t and ("6.8" in t or "1.7" in t), "没说清这种情况有多罕见"


def test_dominant_forced_gets_a_stronger_wording():
    s = _seg(1_000_000, 400_000, liq=4_000_000)   # 600万被平，400万被强平 = 67%
    t = P.forced_words(P.forced_share(s))
    assert t and "不是自己走的" in t


def test_ratio_is_meaningless_when_position_grew():
    """加仓的窗口分母是负的，比值没有含义——不能硬算。"""
    assert P.forced_share(_seg(500_000, 900_000, liq=50_000)) is None


def test_tiny_drop_is_not_measured():
    """减得太少分母就小，比值全是噪声。"""
    assert P.forced_share(_seg(1_000_000, 990_000, liq=5_000)) is None


def test_closed_notional_is_accumulated_bar_by_bar():
    """**必须逐根按当时的价格折算。** 真机撞到的：BTR 那段价格跌了 52%，
    用期末价折算总张数变化，早先在高位平掉的仓被严重低估 → 分母偏小 →
    强平占比被算成 76%（虚高）。
    也不能直接用 open_interest_usd 的首尾差：那个数同时包含"有人平仓"
    和"价格跌了"两件事——价格腰斩而没人动手时它也会腰斩。"""
    import inspect
    src = inspect.getsource(P.seg_stats)
    assert "逐根按当时的价格" in src
    assert "closed" in src


def test_price_crash_alone_is_not_counted_as_closing():
    """价格腰斩但张数没动 —— 一分钱的仓都没平，比值不该有值。"""
    rows = [{"open_interest": 1000, "open_interest_usd": 100_000,
             "mark_price": 100, "long_liq_usd": 0, "short_liq_usd": 0},
            {"open_interest": 1000, "open_interest_usd": 50_000,
             "mark_price": 50, "long_liq_usd": 0, "short_liq_usd": 0}]
    s = P.seg_stats(rows)
    assert s["closed_usd"] == 0
    assert P.forced_share(s) is None


def test_closing_is_valued_at_the_price_it_happened_at():
    """两根各平 500 张：第一根价 100、第二根价 50 →
    应该是 500×100 + 500×50 = 75,000，不是 1000×50=50,000。"""
    rows = [{"open_interest": 2000, "open_interest_usd": 200_000,
             "mark_price": 100, "long_liq_usd": 0, "short_liq_usd": 0},
            {"open_interest": 1500, "open_interest_usd": 75_000,
             "mark_price": 50, "long_liq_usd": 0, "short_liq_usd": 0},
            {"open_interest": 1000, "open_interest_usd": 50_000,
             "mark_price": 50, "long_liq_usd": 0, "short_liq_usd": 0}]
    s = P.seg_stats(rows)
    assert s["closed_usd"] == pytest.approx(75_000, rel=0.01)


def test_oi_line_says_it_is_one_venue_only():
    """那行数字是 Gate 一家的，却紧挨着"三家各自"印——
    不标出处会被读成全网合计，而 Gate 在多数币上只占一到两成。"""
    g = _fake_g(flat_rows(24), [bar(1e6) for _ in range(72)], hours=24)
    txt = "\n".join(P.gate_lines(g, chg=1.0))
    assert "仅 Gate" in txt
