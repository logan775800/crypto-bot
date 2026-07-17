"""持仓币新闻优先。
最重要的一条：币名匹配必须**整词**——ETH 命中 "together"、SUI 命中 "lawsuit"
会把无关新闻标成 🔥，标记一旦不可信，这个功能就等于没有。
"""
from handlers.news import match_syms, prioritize, _STOP_SYMS


SYMS = {"BTC", "ETH", "SUI", "OP"}


class TestMatchSyms:
    def test_plain_hit(self):
        assert match_syms("BTC breaks 70k", SYMS) == ["BTC"]

    def test_case_insensitive(self):
        assert match_syms("btc rallies", SYMS) == ["BTC"]

    def test_multiple_hits_sorted(self):
        assert match_syms("BTC and ETH pump", SYMS) == ["BTC", "ETH"]

    def test_no_hit(self):
        assert match_syms("Solana ecosystem grows", SYMS) == []

    def test_word_boundary_blocks_substring_inside_word(self):
        # 这些以前会误伤：together 含 eth、lawsuit 含 sui、option 含 op
        assert match_syms("They worked together on it", SYMS) == []
        assert match_syms("SEC files a lawsuit", SYMS) == []
        assert match_syms("An option contract", SYMS) == []

    def test_boundary_allows_punctuation_around(self):
        assert match_syms("(BTC) surges, ETH-based apps", SYMS) == ["BTC", "ETH"]

    def test_ethereum_does_not_count_as_eth(self):
        # 整词匹配的代价：Ethereum 不算 ETH 命中。宁可漏标也不误标
        assert match_syms("Ethereum upgrade ships", SYMS) == []

    def test_digits_adjacent_do_not_match(self):
        assert match_syms("OP2 token launch", SYMS) == []

    def test_empty_inputs(self):
        assert match_syms("", SYMS) == []
        assert match_syms("BTC news", set()) == []
        assert match_syms(None, SYMS) == []


class TestPrioritize:
    def _items(self):
        return [
            {"title": "Market wrap", "desc": "general stuff", "link": "a"},
            {"title": "ETH staking update", "desc": "", "link": "b"},
            {"title": "Random altcoin", "desc": "", "link": "c"},
            {"title": "BTC ETF inflow", "desc": "", "link": "d"},
        ]

    def test_held_items_come_first(self):
        out = prioritize(self._items(), SYMS)
        assert [i["link"] for i in out] == ["b", "d", "a", "c"]

    def test_hits_field_is_attached(self):
        out = prioritize(self._items(), SYMS)
        assert out[0]["hits"] == ["ETH"]
        assert out[-1]["hits"] == []

    def test_order_is_stable_within_groups(self):
        # 组内保持原有时间顺序，不能把新闻打乱
        out = prioritize(self._items(), SYMS)
        assert [i["link"] for i in out if not i["hits"]] == ["a", "c"]

    def test_no_holdings_keeps_original_order(self):
        out = prioritize(self._items(), set())
        assert [i["link"] for i in out] == ["a", "b", "c", "d"]

    def test_desc_also_matched(self):
        items = [{"title": "Nothing", "desc": "mentions SUI here", "link": "x"}]
        assert prioritize(items, SYMS)[0]["hits"] == ["SUI"]

    def test_prioritize_before_truncation_matters(self):
        # 这个用例说明为什么必须先排序再截断：持仓新闻排第4，截前2就没了
        items = self._items()
        assert [i["link"] for i in prioritize(items, SYMS)[:2]] == ["b", "d"]


class TestStopSymbols:
    def test_stablecoins_are_excluded(self):
        # 谁都「持有」USDT，它命中一切新闻，当关键词等于全标 🔥
        assert "USDT" in _STOP_SYMS and "USDC" in _STOP_SYMS
