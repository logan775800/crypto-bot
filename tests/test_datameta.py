"""数据可信度层。

这一层的整个价值在于「诚实」，所以测试盯的全是诚实性：
时间戳来自交易所而非本机、缺数据必须显式说、绝不把「取不到」说成「该币没有」。
"""
import pytest

from handlers.datameta import Report
from handlers.marketdata import stamp, bar_lag


class TestStamp:
    def test_formats_exchange_time(self):
        # 2026-07-17 14:32:18 +08:00 → 容器 TZ=Asia/Shanghai
        assert "数据截至" in stamp(1752733938000)

    def test_none_is_explicit_not_fabricated(self):
        # 拿不到交易所时间就说不知道，绝不能偷偷填本机时间冒充
        assert stamp(None) == "数据时间未知"
        assert stamp(0) == "数据时间未知"

    def test_garbage_does_not_crash(self):
        assert stamp("abc") == "数据时间未知"


class TestBarLag:
    def test_fresh_bar_is_silent(self):
        now = 1_700_000_000_000
        txt, stale = bar_lag(now, now - 60_000, "15m")   # 1分钟前的15m K线，正常
        assert txt == "" and stale is False

    def test_stale_bar_is_flagged(self):
        now = 1_700_000_000_000
        txt, stale = bar_lag(now, now - 3600_000, "15m")  # 1小时没新K线 = 4个周期
        assert stale is True and "滞后" in txt

    def test_boundary_two_periods_is_not_yet_stale(self):
        now = 1_700_000_000_000
        _, stale = bar_lag(now, now - 1_800_000, "15m")   # 正好2个周期
        assert stale is False

    def test_missing_inputs_are_silent(self):
        assert bar_lag(None, 1, "15m") == ("", False)
        assert bar_lag(1, None, "15m") == ("", False)
        assert bar_lag(1, 1, "bogus") == ("", False)


def _rep(missing=(), stale=()):
    r = Report("BANK")
    r.server_ms = 1752733938000
    for iv in ("5m", "15m", "30m", "1h", "4h", "1d"):
        r.klines[iv] = (iv not in missing, "空K线" if iv in missing else "")
    for iv in ("15m", "1h"):
        k = f"OI{iv}"
        r.oi[iv] = (k not in missing, "无OI" if k in missing else "")
    for name in ("资金费率", "盘口", "清算数据"):
        r.others[name] = (name not in missing, "取数失败" if name in missing else "")
    r.stale = list(stale)
    return r


class TestCompleteness:
    def test_all_ok(self):
        r = _rep()
        assert r.completeness == 100.0
        assert r.missing == []
        assert r.healthy is True

    def test_counts_missing(self):
        r = _rep(missing=("清算数据",))
        assert r.missing == ["清算数据"]
        assert r.completeness == pytest.approx(10 / 11 * 100)
        assert r.healthy is False

    def test_stale_alone_makes_it_unhealthy(self):
        # 数据取到了但滞后 → 也不能算健康，价位可能是旧的
        assert _rep(stale=("5m",)).healthy is False

    def test_empty_report_does_not_divide_by_zero(self):
        assert Report("BANK").completeness == 0.0


class TestHeader:
    def test_contains_symbol_exchange_and_time(self):
        h = _rep().header()
        assert "BANKUSDT" in h and "Bybit" in h and "数据截至" in h

    def test_healthy_header_has_no_scare_lines(self):
        h = _rep().header()
        assert "完整度" not in h and "暂不可用" not in h

    def test_missing_source_is_marked_unavailable(self):
        h = _rep(missing=("清算数据",)).header()
        assert "清算数据 ⚠️ 暂不可用" in h
        assert "完整度" in h and "清算数据" in h

    def test_missing_kline_interval_is_split_out(self):
        h = _rep(missing=("4h",)).header()
        assert "4h ⚠️" in h
        assert "5m / 15m / 30m / 1h / 1d ✅" in h

    def test_stale_is_called_out(self):
        assert "滞后" in _rep(stale=("5m",)).header()

    def test_degrade_notice_present_when_incomplete(self):
        assert "降级" in _rep(missing=("盘口",)).header()


class TestForAi:
    def test_healthy_allows_full_conclusions(self):
        s = _rep().for_ai()
        assert "全部维度取数成功" in s

    def test_missing_becomes_a_hard_constraint_not_a_hint(self):
        s = _rep(missing=("OI15m", "OI1h")).for_ai()
        assert "你必须" in s
        assert "不得" in s

    def test_explicitly_forbids_saying_the_coin_has_no_such_data(self):
        # 用户明确抱怨过这个：明明是接口挂了，AI 却说「该币没有数据」
        s = _rep(missing=("清算数据",)).for_ai()
        assert "取不到 ≠ 该币没有这项数据" in s

    def test_names_the_missing_dimensions(self):
        s = _rep(missing=("盘口", "清算数据")).for_ai()
        assert "盘口" in s and "清算数据" in s

    def test_stale_forbids_precise_entries(self):
        s = _rep(stale=("15m",)).for_ai()
        assert "不要据此给精确进场位" in s

    def test_always_carries_the_timestamp(self):
        assert "数据截至" in _rep(missing=("盘口",)).for_ai()
        assert "数据截至" in _rep().for_ai()


class TestInvalidSymbol:
    """「这个币不存在」和「接口挂了」必须分开——前者渲染成 0% 体检报告会让人以为系统坏了。"""

    def _invalid(self):
        r = Report("ZZZZ")
        r.server_ms = 1752733938000
        for iv in ("5m", "15m"):
            r.klines[iv] = (False, "Bybit: params error: Symbol Is Invalid")
        r.others["资金费率"] = (False, "Bybit: params error: symbol invalid")
        return r

    def test_detected(self):
        assert self._invalid().invalid_symbol is True

    def test_header_says_not_exist_not_data_failure(self):
        h = self._invalid().header()
        assert "没有 ZZZZUSDT 永续合约" in h
        assert "不是数据故障" in h
        assert "完整度" not in h        # 不该渲染成体检报告

    def test_for_ai_forbids_analysis_entirely(self):
        s = self._invalid().for_ai()
        assert "不存在" in s and "不要" in s

    def test_real_outage_is_not_mistaken_for_invalid_symbol(self):
        # 全挂但原因是超时 → 这是故障，要走降级报告而不是「币不存在」
        r = Report("BANK")
        r.server_ms = 1752733938000
        for iv in ("5m", "15m"):
            r.klines[iv] = (False, "ReadTimeout")
        r.others["盘口"] = (False, "ConnectError")
        assert r.invalid_symbol is False
        assert "完整度" in r.header()

    def test_partial_success_is_never_invalid_symbol(self):
        r = _rep(missing=("清算数据",))
        assert r.invalid_symbol is False

    def test_empty_report_is_not_invalid_symbol(self):
        assert Report("BANK").invalid_symbol is False


class TestReasons:
    def test_lists_only_failures_with_cause(self):
        r = _rep(missing=("清算数据",))
        r.others["清算数据"] = (False, "OKX 源取数失败：timeout")
        out = r.reasons()
        assert "清算数据" in out and "timeout" in out
        assert "资金费率" not in out

    def test_healthy_has_no_reasons(self):
        assert _rep().reasons() == ""
