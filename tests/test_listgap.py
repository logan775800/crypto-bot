"""跨所上币缺口：两家有、第三家没有 = 第三家的候选。

他看完 /alpha 说「不全面吧，这只拉币安的候选池」，想要三家现货和三家合约
各一个命令。**照字面做不到**：实测只有币安有公开孵化池，
Gate 的 Alpha/launchpad 是 403、Bybit 的是 404。
所以换成用三家各自的上币列表相减——不需要谁公布候选池。
"""
import pytest

from handlers import listgap as G


def _book(binance, gate, bybit):
    return {"币安": set(binance), "Gate": set(gate), "Bybit": set(bybit)}


# ── 核心算法 ────────────────────────────────────────────────
def test_gap_is_what_the_other_two_have_and_this_one_does_not():
    g = G.gaps(_book(["A"], ["A", "B"], ["A", "B"]))
    assert g["币安"] == ["B"]
    assert g["Gate"] == [] and g["Bybit"] == []


def test_a_coin_on_only_one_exchange_is_not_a_gap():
    """只有一家有，说明另外两家都没看上，那不是"缺"。"""
    g = G.gaps(_book(["A"], [], []))
    assert all(v == [] for v in g.values())


def test_missing_one_exchange_kills_the_whole_thing():
    """某一家取数失败时，它的「缺口」会变成另外两家的全部——
    一个看着很吓人但完全错误的数字。宁可这一轮不出。"""
    assert G.gaps({"币安": {"A"}, "Gate": {"A", "B"}}) is None


def test_card_says_why_when_data_is_incomplete():
    txt = G.build_text({"spot": {"币安": {"A"}}, "fut": {}, "vol": {}}, "spot")
    assert "没取全" in txt and "另外两家的全部" in txt


# ── 面值前缀：真机第一跑抓到的假缺口 ─────────────────────────
def test_denomination_prefix_must_be_normalised():
    """**真机第一跑就抓到**：「Gate 合约缺 1000PEPE / 1000BONK」——
    那是币安和 Bybit 的千倍合约命名，Gate 上就叫 PEPE / BONK，它一点不缺。
    不归一的话每个千倍合约都会在两家的缺口里各冒出来一次，全是假的。"""
    import inspect
    src = inspect.getsource(G.fetch_all)
    assert "norm_base" in src
    assert "1000PEPE" in src, "没把这个坑记在代码里"


def test_normalised_names_keep_their_volume():
    """归一之后如果不同步搬成交额，榜上会显示成 0。"""
    import inspect
    src = inspect.getsource(G.fetch_all)
    i_norm = src.index("norm_base(x) for x in book[k]")
    i_vol = src.index("for s in list(vol):")
    assert i_vol > i_norm, "成交额归一排在集合归一之前，搬不到"


def test_tokenized_stocks_are_excluded():
    """同一个坑第六次了。而且"某家没上代币化美股"根本不是有价值的缺口。"""
    import inspect
    src = inspect.getsource(G.fetch_all)
    assert "noncrypto_bases" in src and "_BSTOCK" in src


# ── 渲染 ────────────────────────────────────────────────────
def test_ranked_by_volume_not_alphabetically():
    """缺口的意思是「这家的用户买不到」，成交额直接衡量有多少人在买。
    （/alpha 那边按市值排，因为那问的是「够不够格上」，判据不一样）"""
    data = {"spot": _book([], ["A", "Z"], ["A", "Z"]), "fut": {},
            "vol": {"A": 1, "Z": 999}}
    txt = G.build_text(data, "spot")
    assert txt.index("Z") < txt.index("　A"), "没按成交额排"


def test_zero_gap_says_so_instead_of_printing_nothing():
    """空着不说话，看的人分不清是"没有缺口"还是"坏了"。"""
    data = {"spot": _book(["A"], ["A"], ["A"]), "fut": {}, "vol": {}}
    assert "（没有）" in G.build_text(data, "spot")


@pytest.mark.parametrize("kind", ["spot", "fut"])
def test_card_fits_the_line_budget(kind):
    """5 行/家的时候合约那张 26 行，按钮被挤出屏幕。"""
    many = [f"C{i}" for i in range(200)]
    book = _book([], many, many)
    data = {"spot": book, "fut": book, "vol": {c: 1 for c in many}}
    n = len(G.build_text(data, kind).splitlines())
    assert n <= 24, f"{kind} 那张 {n} 行"


# ── 口径 ────────────────────────────────────────────────────
def test_detail_records_that_only_binance_publishes_a_pool():
    """Gate 403 / Bybit 404 —— 不写下来，下次还会有人去找「Gate 的候选池」。"""
    t = G.detail_text()
    assert "403" in t and "404" in t
    assert "只有币安" in t


def test_detail_states_the_validation_and_its_hole():
    """98% 的共同上市是强先验，但我只看得到**现在**、看不到**先后**。
    把洞说出来，别把相关说成因果。"""
    t = G.detail_text()
    assert "98%" in t
    assert "先后" in t and ("不是因果" in t or "强先验" in t)


def test_detail_explains_why_gate_barely_has_gaps():
    """Gate 现货只缺 3 个不是 bug，是它上了 2042 个。
    不解释的话看着像取数错了。"""
    t = G.detail_text()
    assert "2042" in t


# ── 接线 ────────────────────────────────────────────────────
def test_two_commands_registered():
    """他要的就是两个命令：一个现货一个合约。"""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("gapspot", listgap.gapspot_cmd)' in src
    assert 'CommandHandler("gapfut", listgap.gapfut_cmd)' in src
    assert 'BotCommand("gapspot"' in src and 'BotCommand("gapfut"' in src


def test_two_cards_link_to_each_other():
    """现货和合约是同一个问题的两面，来回切不该要重打命令。"""
    cbs = [b.callback_data for row in G.kb("spot").inline_keyboard for b in row]
    assert "gp:fut" in cbs
    cbs = [b.callback_data for row in G.kb("fut").inline_keyboard for b in row]
    assert "gp:spot" in cbs


def test_callback_routed():
    import inspect
    from handlers import menu
    assert 'd.startswith("gp:")' in inspect.getsource(menu._dispatch)
