"""持仓结构：判读逻辑 + 告警接线。

这个模块的价值全在**判读**上（取数只是三个接口）。所以测试重点是：
同一组数字配不同的价格方向，会不会讲出两个不同的故事——
第一版就栽在这里：MVLL 跌了 25%、两个比值都在涨，卡片却印
「多头拥挤度在上升，回调时容易多杀多」，那是涨势里的话术。
"""
import asyncio

import pytest

from handlers import posflow as P


# ── 门槛 ────────────────────────────────────────────────────
def test_below_threshold_is_noise_not_a_signal():
    """基线组（不涨不跌的大盘币）日常自己就飘 6~7%，所以 10% 以下不能当变化。
    把噪音读成信号，比不读更糟——它会让人以为自己看见了什么。"""
    assert P._dir(9.9) == 0
    assert P._dir(-9.9) == 0
    assert P._dir(10.0) == 1
    assert P._dir(-10.0) == -1
    assert P._dir(None) is None


def test_delta_is_relative_not_absolute():
    """大户比基数 0.8 和 3.0 的币，同样 +0.3 意义差好几倍。
    用绝对差就没法跨币比较，也没法和门槛比。"""
    assert P._delta(1.0, 1.5) == pytest.approx(50)
    assert P._delta(3.0, 3.5) == pytest.approx(16.67, abs=0.1)
    assert P._delta(0, 1) is None          # 除零不能炸
    assert P._delta(None, 1) is None


# ── 判读：主干是持仓量 ────────────────────────────────────────
def test_price_up_with_oi_up_is_new_money():
    v = P.verdict(price_chg=30, oi_pct=60)
    assert "新资金" in v[0]


def test_price_up_with_oi_down_is_a_short_squeeze_not_new_money():
    """他那套框架里最关键的一条：价涨+仓减说明是空头平仓推上去的，
    上方燃料正在被烧掉，烧完就没人接力——**这是转折信号，不是利好**。"""
    v = P.verdict(price_chg=30, oi_pct=-25)
    assert "轧空" in v[0] and "燃料" in v[0]
    assert "不是新资金" in v[0]         # 「不是新资金」里也含「新资金」，别用否定断言


def test_price_down_branches():
    assert "新空" in P.verdict(price_chg=-30, oi_pct=40)[0]
    assert "认赔离场" in P.verdict(price_chg=-30, oi_pct=-40)[0]


def test_flat_oi_says_so_instead_of_guessing():
    v = P.verdict(price_chg=30, oi_pct=2)
    assert "没有明显的新钱" in v[0]


def test_missing_oi_admits_it_cannot_judge():
    """取不到数和"没问题"是两回事。这里必须明说判不了，
    不能因为缺一个字段就悄悄换成另一套说辞。"""
    v = P.verdict(price_chg=30, oi_pct=None)
    assert "判不了" in v[0]


def test_no_price_no_verdict():
    assert P.verdict(price_chg=None, oi_pct=50) == []


# ── 判读：同一组数字，涨和跌讲的是两个故事 ─────────────────────
def test_both_ratios_rising_reads_differently_up_vs_down():
    """真机抓到的那个 bug：MVLL -25%、大户 +123%、散户 +63%，
    却印出「回调时容易多杀多」——那是涨势里的话术。"""
    up = P.who_line(60, 40, up=True)
    down = P.who_line(60, 40, up=False)
    assert up != down
    assert "追多" in up                       # 涨势里比值升 = 追高盘在进
    assert "抄底" in down                     # 跌势里比值升 = 有人在往下接
    assert "回调" not in down


def test_both_ratios_falling_during_a_pump_means_fuel_is_rebuilding():
    """拉上去的同时两边都在往空头挪 = 新空在冲进来。
    空头是多头的燃料，所以这在他那套框架里是"补给中"，不是利空。
    真机 SKR：+19.8%、大户 -18%、散户 -35%、OI +133%。"""
    assert "燃料在重建" in P.who_line(-18, -35, up=True)


def test_the_distribution_pattern_is_called_out_in_both_directions():
    """大钱在减、散户在追（涨势）/ 大钱在跑、散户在接（跌势）——
    两种都是最该警惕的组合，不能只在其中一个方向上提醒。"""
    assert "最该警惕" in P.who_line(-30, 30, up=True)
    assert "最该警惕" in P.who_line(-30, 30, up=False)


def test_one_sided_data_still_says_something():
    """只有一边动了，也该说话，别整行消失。"""
    assert P.who_line(30, 2, up=True)
    assert P.who_line(2, 30, up=True)
    assert P.who_line(2, 2, up=True) == ""     # 两边都没动才闭嘴
    assert P.who_line(None, None, up=True) == ""


def test_who_line_is_wired_into_verdict_with_the_right_direction():
    """判读函数拿到了 price_chg，就必须把方向传下去——
    第一版漏传，who_line 默认按涨势解释，跌的时候全错。"""
    up = P.verdict(price_chg=30, oi_pct=50, top_pct=60, retail_pct=40)
    down = P.verdict(price_chg=-30, oi_pct=50, top_pct=60, retail_pct=40)
    assert up[1] != down[1]


# ── 资金费率 ────────────────────────────────────────────────
def test_funding_only_speaks_when_it_is_actually_expensive():
    """日常 0.01%/8h ≈ 年化 11%，印出来是废话，会把真正贵的那次淹掉。"""
    assert len(P.verdict(30, 50, apr=11)) == 1
    v = P.verdict(30, 50, apr=200)
    assert any("资金费" in x and "多头" in x for x in v)
    v = P.verdict(30, 50, apr=-200)
    assert any("资金费" in x and "空头" in x for x in v)


def test_funding_threshold_is_shared_with_precheck():
    """同一个"贵不贵"的判断在两个地方各定一个数，迟早分叉。"""
    from handlers import precheck
    assert P.FUNDING_COSTLY_APR is precheck.FUNDING_COSTLY_APR


# ── 渲染 ────────────────────────────────────────────────────
FAKE = {"sym": "BTR", "hours": 24,
        "top": 2.09, "top_prev": 1.62, "top_pct": 29.0, "top_src": "币安",
        "retail": 0.57, "retail_prev": 1.04, "retail_pct": -45.0,
        "retail_src": "币安",
        "oi": 39_800_000, "oi_pct": 115.6,
        "funding": 0.095, "funding_h": 8, "funding_apr": 104.0, "chg": 73.3}


def test_previous_value_is_always_printed():
    """他要的是**变化**。只印一个当前值的话，"变了多少"根本看不出来。"""
    txt = "\n".join(P.lines(FAKE, 73.3))
    assert "1.62" in txt and "2.09" in txt and "+29%" in txt


def test_the_two_ratios_are_listed_separately_with_their_units_explained():
    """一个按持仓金额、一个按账户个数。口径不写出来，
    两个数打架时看的人会以为是数据错了。"""
    txt = "\n".join(P.lines(FAKE, 73.3))
    assert "大户持仓比" in txt and "人数多空比" in txt
    assert "持仓金额" in txt and "账户个数" in txt


def test_compact_drops_the_explainer_but_keeps_the_numbers_and_verdict():
    """配图的说明有 1024 字硬上限，每多一行都在挤别的内容。"""
    full = P.lines(FAKE, 73.3)
    small = P.lines(FAKE, 73.3, compact=True)
    assert len(small) < len(full)
    assert any("大户持仓比" in x for x in small)
    assert any(x.startswith("→") for x in small)


def test_block_is_empty_when_there_is_nothing_to_say():
    assert P.block(None) == ""
    assert P.block({}) == ""


def test_missing_fields_do_not_crash_or_get_invented():
    """币安没上的币只有 Bybit 的人数比，大户比是真的没有——
    这时候要少印一行，不是补个 0 或者写"正常"。"""
    part = {"sym": "X", "hours": 24, "top": None, "top_prev": None,
            "top_pct": None, "retail": 1.2, "retail_prev": 1.0,
            "retail_pct": 20.0, "retail_src": "Bybit", "oi": None,
            "oi_pct": None, "funding_apr": None}
    txt = "\n".join(P.lines(part, 30))
    assert "大户持仓比" not in txt
    assert "人数多空比" in txt and "Bybit" in txt


def test_non_binance_source_is_labelled():
    """人数比可能来自 Bybit，两家统计的是各自的用户、数字对不上，
    不标来源等于把两套口径混成一个数（/lsr 那条同理）。"""
    f = dict(FAKE, retail_src="Bybit")
    assert "Bybit" in "\n".join(P.lines(f, 73.3))
    assert "币安" not in "\n".join(P.lines(FAKE, 73.3)).split("→")[0].replace(
        "币安", "币安")  # 默认源不啰嗦地重复标注


# ── 接线：告警里真的挂上去了 ───────────────────────────────────
def test_attach_never_breaks_the_alert(monkeypatch):
    """告警本身已经有价值。补一段结构分析取不到数，绝不能让整条发不出去。"""
    async def boom(*a, **k):
        raise RuntimeError("接口挂了")
    monkeypatch.setattr(P, "fetch", boom)
    P._cache.clear()
    assert asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        P.attach("BTC", 30)) == ""


def test_attach_caches_so_每个订阅群不重复打接口(monkeypatch):
    """告警是逐个订阅群发的，同一轮同一个币会被问 N 次，答案完全一样。"""
    calls = []

    async def fake(sym, hours):
        calls.append(sym)
        return FAKE
    monkeypatch.setattr(P, "fetch", fake)
    P._cache.clear()
    loop = asyncio.new_event_loop()
    try:
        a = loop.run_until_complete(P.attach("BTR", 73.3))
        b = loop.run_until_complete(P.attach("BTR", 73.3))
    finally:
        loop.close()
    assert a == b and a != ""
    assert len(calls) == 1, "同一轮同一个币打了两次接口"


def test_contract_alert_attaches_the_structure_block():
    """他要的就是「大涨告警里除了清算地图，还要有大户持仓变化」。
    这条锁的是**接线**——配图那次静默失败，图消失了几个版本没人发现。"""
    import inspect
    from handlers import contract_alert
    src = inspect.getsource(contract_alert._attach_liqmap)
    assert "posflow.attach" in src


def test_caption_never_exceeds_telegram_limit():
    """图片说明超 1024 字整张图就发不出去，而配图失败是静默跳过的——
    表现成"告警突然不带图了"，正是上次那个坑。"""
    from handlers import contract_alert as C
    assert len(C._fit_caption("啊" * 5000)) <= C.CAPTION_MAX
    assert C._fit_caption("短的") == "短的"


def test_pump3_alert_also_says_who_pushed_it():
    """极端拉升一个月才响几次，多打三次接口不心疼；
    而"15m +40%"不说清是新资金还是轧空，等于没说。"""
    import inspect
    from handlers import pump3
    assert "posflow.attach" in inspect.getsource(pump3.check)


@pytest.mark.parametrize("where,fn", [
    ("币种卡片", "followup_kb"),
])
def test_button_entry_exists_on_the_coin_card(where, fn):
    """他的规矩：功能必须有按钮入口，超过两层就当 bug 修。
    发个币名 → 卡片上就有，这是一层。"""
    from handlers import menu
    kb = getattr(menu, fn)("BTC")
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "pf:r:BTC" in cbs, f"{where}上找不到持仓结构入口"


def test_liqmap_card_links_to_the_structure_card():
    """清算地图说"上下堆着多少爆仓单"，持仓结构说"这波是谁推的"——
    上方燃料清空 + 持仓不降 = 蓄力，+ 持仓降 = 派发。
    只看图会把这两种完全相反的情形读成同一种，所以两张卡必须互通。"""
    from handlers import liqmap
    cbs = [b.callback_data for row in liqmap.kb("BTC", "7日").inline_keyboard
           for b in row]
    assert "pf:r:BTC" in cbs


def test_structure_card_links_back_to_liqmap():
    cbs = [b.callback_data for row in P.kb("BTC").inline_keyboard for b in row]
    assert any(c.startswith("lq:w:BTC") for c in cbs)


def test_callback_is_routed():
    """按钮做了但没接线 = 点了没反应。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert 'd.startswith("pf:")' in src


def test_command_is_registered():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("pos", posflow.pos_cmd)' in src
    assert 'BotCommand("pos"' in src


# ── 口径文案 ────────────────────────────────────────────────
def test_detail_states_coverage_and_the_measured_basis():
    """口径写脸上：门槛哪来的、哪家有哪家没有、退回备用源时缺什么。

    这条原来断言的是「大户持仓比只有币安有」。接了 Gate 之后那句话
    不再成立，测试当场红了——**它锁的是一句会过期的事实声明，这次
    它红得对**：口径页里留着一句已经不对的话，比没写还糟。
    """
    t = P.detail_text("BTC")
    assert "Gate" in t and "币安" in t
    assert "退回币安" in t, "没写清退回备用源时缺哪两样"
    assert "10%" in t
    assert "基线" in t


def test_detail_explains_why_empty_fuel_is_not_a_top():
    """他问的就是这个：上方清算燃料清完了，多头还有没有动机继续拉。"""
    t = P.detail_text("BTC")
    assert "不会归零" in t and "蓄力" in t and "派发" in t
