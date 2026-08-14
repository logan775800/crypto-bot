"""BTC 市场环境提醒：这个功能的成败全在防抖。

一个会在均线纠缠期一天喊三次的提醒，几次之后就被静音了，等于没做。
所以这里绝大多数用例测的不是「能不能识别多头排列」，而是**什么时候闭嘴**。
"""
import time

import pytest

import storage
from handlers import regime as R


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    storage.data.clear()
    storage.apply_defaults()
    monkeypatch.setattr(storage, "save_data", lambda: None)
    yield
    storage.data.clear()
    storage.apply_defaults()


def _st(**kw):
    st = {"state": "", "pending": "", "pending_n": 0, "changed_ts": 0}
    st.update(kw)
    return st


# ---------------------------------------------------------------- 分类
def test_classify_three_states():
    assert R.classify(3, 2, 1) == R.BULL
    assert R.classify(1, 2, 3) == R.BEAR
    assert R.classify(2, 3, 1) == R.CHOP


def test_classify_refuses_to_guess_on_missing_data():
    assert R.classify(None, 2, 1) is None
    assert R.classify(3, 2, None) is None


# ---------------------------------------------------------------- 防抖
def test_first_run_records_baseline_without_shouting():
    """刚上线时不该因为「从无到有」就播报一次环境变化。"""
    st = _st()
    assert R.step(R.BULL, st, 1000) == ""
    assert st["state"] == R.BULL


def test_single_flip_is_not_enough():
    """只读到一次新状态就播报 = 均线一纠缠就刷屏。"""
    st = _st(state=R.BULL)
    assert R.step(R.BEAR, st, 1000) == ""
    assert st["state"] == R.BULL, "还没确认，基线不能动"
    assert st["pending"] == R.BEAR and st["pending_n"] == 1


def test_confirmed_flip_announces():
    st = _st(state=R.BULL)
    R.step(R.BEAR, st, 1000)
    text = R.step(R.BEAR, st, 2000)
    assert text and "空头排列" in text and "多头排列" in text
    assert st["state"] == R.BEAR
    assert st["pending_n"] == 0, "认定后要清零，否则下次一跳就播"


def test_flip_back_resets_confirmation():
    """跳过去又跳回来 —— 典型的纠缠，必须当没发生过。"""
    st = _st(state=R.BULL)
    R.step(R.BEAR, st, 1000)          # 在途确认 1 次
    R.step(R.BULL, st, 2000)          # 又回来了
    assert st["pending_n"] == 0 and st["pending"] == ""
    assert R.step(R.BEAR, st, 3000) == "", "得重新从头确认"


def test_cooldown_suppresses_second_change(monkeypatch):
    """刚播报完又变回去：状态照记，但冷静期内不再吭声。"""
    monkeypatch.setattr(R, "COOLDOWN", 3600)
    st = _st(state=R.BULL)
    R.step(R.BEAR, st, 1000)
    assert R.step(R.BEAR, st, 1100)                     # 第一次播报
    R.step(R.BULL, st, 1200)
    assert R.step(R.BULL, st, 1300) == "", "冷静期内不播报"
    assert st["state"] == R.BULL, "但状态要跟上，否则后面判变化会用错基线"


def test_change_after_cooldown_announces_again(monkeypatch):
    monkeypatch.setattr(R, "COOLDOWN", 3600)
    st = _st(state=R.BULL)
    R.step(R.BEAR, st, 1000)
    R.step(R.BEAR, st, 1100)                            # changed_ts = 1100
    R.step(R.BULL, st, 1200)
    R.step(R.BULL, st, 1300)                            # 冷静期内，静默改为 BULL
    R.step(R.BEAR, st, 90000)
    assert R.step(R.BEAR, st, 90100), "过了冷静期就该正常播报"


def test_unreadable_data_changes_nothing():
    """取不到数据这轮就跳过，绝不把 None 当成一种环境。"""
    st = _st(state=R.BULL, pending=R.BEAR, pending_n=1)
    assert R.step(None, st, 1000) == ""
    assert st == _st(state=R.BULL, pending=R.BEAR, pending_n=1)


def test_same_state_is_silent_forever():
    st = _st(state=R.BULL)
    for t in range(1000, 20000, 1800):
        assert R.step(R.BULL, st, t) == ""


# ---------------------------------------------------------------- 播报内容
def test_announcement_says_what_it_means_and_is_not_a_signal():
    st = _st(state=R.BULL)
    R.step(R.BEAR, st, 1000)
    text = R.step(R.BEAR, st, 2000)
    assert R.MEANING[R.BEAR] in text
    assert "不是买卖信号" in text, "环境提醒被当成信号用会很危险"


# ---------------------------------------------------------------- 订阅
def test_regime_subs_migrate_with_the_group():
    """群升级成超级群时订阅要跟着搬，否则从此再也收不到。"""
    storage.data["regime_subs"] = [-100]
    assert storage.migrate_chat(-100, -200) == 1
    assert storage.data["regime_subs"] == [-200]


def test_regime_subs_counted_as_subscribed_chat():
    """只订了环境提醒的群，也该收得到版本更新播报。"""
    storage.data["regime_subs"] = [-100]
    assert storage.subscribed_chats() == [-100]
