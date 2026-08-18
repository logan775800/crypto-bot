"""重活的并发闸 —— 同一个人的同一个重操作，一次只能跑一个。

放开 concurrent_updates 之后出现的新问题：以前串行时，他点第二次动量回测
会排在后面；现在两个回测**同时**跑，各自去抢 CoinGecko 那把 2 秒间隔的全局锁，
结果两个都变慢一倍，别的功能（查价、链上）也一起被挤。

所以：重活按 (用户, 名字) 上闸。第二次点直接告诉他"上一次还在跑"，
而不是默默排队——排队的表现和卡死一模一样，这正是他上次遇到的观感。

只闸**耗时且可重复触发**的：动量回测、全市场扫描、缓步增长、微市值。
查价那种一秒返回的不必闸，闸了反而添乱。
"""
import logging
import time

log = logging.getLogger(__name__)

# (uid, name) -> 开始时间
_running = {}
STALE = 600      # 超过这么久还没释放，当成漏放了（异常路径没走 finally）


def is_busy(uid, name):
    ts = _running.get((str(uid), name))
    if ts is None:
        return False
    if time.time() - ts > STALE:
        _running.pop((str(uid), name), None)
        return False
    return True


def elapsed(uid, name):
    ts = _running.get((str(uid), name))
    return int(time.time() - ts) if ts else 0


class guard:
    """async with busy.guard(uid, "momentum") as ok:  —— ok 为 False 就别跑。

    刻意不抛异常：调用方要做的是"友好告诉他还在跑"，
    抛异常会走到全局错误处理器，变成一条 traceback 推给管理员。
    """

    def __init__(self, uid, name):
        self.key = (str(uid), name)
        self.ok = False

    async def __aenter__(self):
        if is_busy(*self.key):
            return False
        _running[self.key] = time.time()
        self.ok = True
        return True

    async def __aexit__(self, *exc):
        if self.ok:
            _running.pop(self.key, None)
        return False


def busy_text(uid, name, cn):
    return (f"⏳ 你的{cn}还在跑（已 {elapsed(uid, name)} 秒）。\n"
            f"这类活要逐个拉行情、还要让着数据源的限流，急不得。\n"
            f"跑完会直接把结果发给你，不用重复点。")
