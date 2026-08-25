import logging
import httpx
from telegram.ext import ContextTypes
from config import ADMIN_IDS

# 数据源健康状态（内存记录，检测状态变化）
# 币安是 v1.46.0 之后的**主源**（扫描/清算地图/急涨急跌都币安优先），
# 它挂了大半个机器人会哑——而在补上之前，这里只探 CoinGecko 和 OKX，
# 于是最要紧的那家反而没人看着。
_health = {"coingecko": True, "okx": True, "binance": True, "bybit": True}

async def notify_admin(context, text):
    """发告警给所有管理员"""
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=int(aid), text=text)
        except Exception as e:
            logging.error(f"管理员告警发送失败 {aid}: {e}")

# 启动告警（post_init后发一次）
async def startup_notify(context: ContextTypes.DEFAULT_TYPE):
    """启动播报带上本版改了什么——光一个版本号对使用者没有信息量。"""
    from config import VERSION
    from handlers.changelog import startup_text
    await notify_admin(context, startup_text(VERSION) + _live_warning())
    await announce_update(context, VERSION)


def _live_warning():
    """连的是实盘就在启动播报里说一声（只发管理员那条）。

    重启、改 .env、部署都会走这里，而「现在连的是真钱账户还是模拟盘」不会主动
    告诉任何人——只有去发 /version 才看得到。真钱的事不该靠人记得去查。
    顺带把下单总开关的状态一并报出来：开关是关着的话，他点开仓会被拒，
    到时候又要排查半天。
    """
    try:
        from bybit_trade import BYBIT_API_KEY, _mode
        if not BYBIT_API_KEY or _mode() != "live":
            return ""
        from handlers.keyguard import trading_enabled
        sw = "✅ 开启" if trading_enabled() else "🔴 已禁用"
        return (f"\n\n🔴 *当前连的是 Bybit 实盘*（真钱）\n"
                f"下单总开关：{sw}　停手发 `/killswitch on`")
    except Exception as e:
        logging.warning(f"实盘提示生成失败: {e}")
        return ""


async def announce_update(context, version):
    """版本变了就通知所有订阅会话——不然群里只看得到行为变了，不知道为什么变。

    两条克制：
    • 只在**版本号变化**时发。容器重启、崩溃拉起、改个 .env 都会走 startup_notify，
      拿这些去刷群，几次之后大家就不看播报了。管理员那条不受此限（运维要知道进程起没起）。
    • 没写更新说明就不发（update_text 返回空），宁可不播报也不发一句废话。
    """
    from storage import data, save_data, subscribed_chats
    from handlers.changelog import update_text
    if data.get("announced_version") == version:
        return
    text = update_text(version)
    if not text:
        return
    # 管理员刚在私聊收过 startup_text，别再让他收一遍几乎一样的。
    # ⚠️ **只排除正数 id**：Telegram 的 id 有符号约定，正数是用户、负数是群/频道。
    # ADMIN_CHAT_ID 里填群 id 是常见配置（想让整个群都能管），
    # 而按老写法那会把**整个群**从更新播报里静默剔掉——
    # 表现就是"部署成功了但群里没有更新内容"，而且完全查不出原因。
    admins = {int(a) for a in ADMIN_IDS
              if str(a).lstrip("-").isdigit() and int(a) > 0}
    sent = 0
    for cid in subscribed_chats():
        if cid in admins:
            continue
        try:
            await context.bot.send_message(chat_id=cid, text=text)
            sent += 1
        except Exception as e:
            # 单个会话发失败（被踢、群解散）不能挡住其余会话，也不该让版本标记不落地
            logging.warning(f"更新播报发送失败 {cid}: {e}")
    # 无论发成功几个都记下来：否则一个死会话会让每次重启都重播一遍
    # ⚠️ **只有真发出去过才标记已播报。**
    #
    # 老写法是"无论发成功几个都记下来"，理由是"否则一个死会话会让每次重启都重播"。
    # 但那把两件事混了：**一个会话失败**和**一个都没成功**完全不同。
    # startup_notify 挂在容器启动后 15 秒，那时网络/Telegram 连接常常还没稳——
    # 只要那一次全挂，版本就被永久标记成"已播报"，再也不会重试。
    # 现场表现正是他说的：部署成功了，群里**经常**没有更新内容（不是每次都没有）。
    if sent == 0:
        logging.warning(
            f"更新播报 {version}：一个会话都没发出去，**不标记已播报**，下一轮会重试。"
            f"候选 {len(list(subscribed_chats()))} 个、排除管理员 {len(admins)} 个")
        return
    data["announced_version"] = version
    save_data()
    logging.info(f"更新播报 {version}：已通知 {sent} 个会话")


async def retry_announce(context: ContextTypes.DEFAULT_TYPE):
    """定时补发更新播报。已经播过就是个空操作。

    光靠启动后 15 秒那一次太脆：那时连接常常还没稳，而一次失败就再也没有第二次。
    挂个便宜的重试，把"偶尔发不出去"变成"最多晚几分钟"。
    """
    from config import VERSION
    await announce_update(context, VERSION)


def broadcast_state():
    """更新播报这条链的现状，给 /datacheck 和排障用。

    这条链出问题时**完全没有可观测性**——部署成功了、群里没东西，
    而日志在服务器上、他看不到。所以做成能在 Telegram 里直接查的。
    """
    from config import VERSION
    from handlers.changelog import update_text
    from storage import data, subscribed_chats     # 都是函数内 import，别靠模块级
    subs = list(subscribed_chats())
    admins = {int(a) for a in ADMIN_IDS
              if str(a).lstrip("-").isdigit() and int(a) > 0}
    targets = [c for c in subs if c not in admins]
    return {
        "version": VERSION,
        "announced": data.get("announced_version"),
        "will_send": data.get("announced_version") != VERSION,
        "text_len": len(update_text(VERSION) or ""),
        "subscribed": len(subs),
        "excluded_admins": len(subs) - len(targets),
        "targets": targets[:20],
    }

# 数据源健康检查（定时调用）
async def health_check(context: ContextTypes.DEFAULT_TYPE):
    # 检查 CoinGecko
    await _check_source(context, "coingecko",
        "https://api.coingecko.com/api/v3/ping", "CoinGecko行情源")
    # 检查 OKX
    await _check_source(context, "okx",
        "https://www.okx.com/api/v5/public/time", "OKX交易所源")
    # 币安：现在的主源，扫描/清算地图/急涨急跌/K线都优先走它
    await _check_source(context, "binance",
        "https://api.binance.com/api/v3/ping", "币安行情源")
    # Bybit：账户类功能和一半告警的兜底源
    await _check_source(context, "bybit",
        "https://api.bybit.com/v5/market/time", "Bybit行情源")

log = logging.getLogger(__name__)

# 连续失败多少次才报警。单次探测就报的话，一个 10 秒超时就能触发——
# 而误报会让人开始忽略告警，那比不报还糟。
FAIL_STREAK = 3
_fails = {}

# 各数据源挂掉时**具体**哪些功能受影响。"部分功能可能受影响"等于没说，
# 用户既不知道该停手还是照用，也不知道该验证什么。
IMPACT = {
    "coingecko": ("现货报价、涨跌榜、技术分析、每日播报。"
                  "价格预警/持仓监控会自动切到 Bybit 永续价（有基差但能用）"),
    "okx": "OKX 专区、清算数据、部分合约行情（币安/Bybit 路径不受影响）",
    "binance": ("**主源**：急涨急跌、清算地图、K线与研判卡、多日涨跌榜、"
                "资金费率榜。多数模块会退到 Bybit，但覆盖的币会少一批"),
    "bybit": ("账户类功能（余额/持仓/复盘）、以及币安独缺时的兜底行情。"
              "币安正常的话行情侧影响不大"),
}


async def _check_source(context, key, url, name):
    """探一次源。区分三种结果：正常 / 限流 / 真不通。

    429 是**我们自己调太快**，不是源挂了——把它算成故障会天天误报，
    而且指向错误的排查方向。
    """
    status = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            status = resp.status_code
    except Exception as e:
        log.debug(f"健康检查 {name} 异常: {e}")
    if status == 429:
        log.warning(f"{name} 被限流(429)——是调用频率问题，不算数据源故障")
        return
    ok = status == 200

    if ok:
        _fails[key] = 0
        if not _health[key]:
            _health[key] = True
            await notify_admin(context, f"🟢 数据源恢复\n{name} 已恢复正常")
        return

    _fails[key] = _fails.get(key, 0) + 1
    if _fails[key] < FAIL_STREAK or not _health[key]:
        return          # 还没连续失败够次数，或已经报过了
    _health[key] = False
    extra = ""
    if key == "coingecko":
        from api import fallback_recent
        if fallback_recent():
            extra = "\n✅ 已自动切到 Bybit 兜底源，取价功能仍在工作"
    await notify_admin(context,
        f"🔴 数据源异常\n{name} 连续 {_fails[key]} 次探测失败"
        f"（间隔 5 分钟，非瞬时抖动）\n\n受影响：{IMPACT.get(key, '未知')}{extra}")


# ── 后台任务心跳 ────────────────────────────────────────────────
# 告警任务挂掉的表现就是**安静地什么都不发生**——和"这段时间市场没异动"
# 在屏幕上一模一样。数据源探测管不到这一层：源好好的，任务自己抛异常死循环，
# 照样一条告警都推不出来。
#
# ⚠️ 这层能看见什么、看不见什么，要说清楚：
#   看得见：任务抛异常逃到框架、任务压根没被调度到。
#   看不见：任务内部自己 try/except 把错吞了然后正常返回——那种"成功但没结果"
#           只能靠各模块自己报数（比如急涨急跌的 /pumptop 自检）。
import time as _time

_BEATS = {}          # 任务名 -> {last_run, last_ok, fails, err, interval}
JOB_FAIL_STREAK = 3  # 连续失败这么多轮才报（一次网络抖动不值得惊动人）
_job_alerted = set()


def beat(name, ok, interval=None, err=""):
    """登记一次任务运行结果。一般不用直接调，用 `tracked()` 包一层。"""
    r = _BEATS.setdefault(name, {"last_run": 0, "last_ok": 0, "fails": 0,
                                 "err": "", "interval": interval})
    if interval:
        r["interval"] = interval
    r["last_run"] = _time.time()
    if ok:
        r["last_ok"] = r["last_run"]
        r["fails"] = 0
        r["err"] = ""
    else:
        r["fails"] += 1
        r["err"] = err


def tracked(fn, name, interval):
    """把一个定时任务包起来，跑完登记心跳。返回可直接交给 job queue 的协程。

    **异常在这里被吃掉不再往上抛**：抛出去的结果只是框架记一条日志，
    任务下一轮照跑，没有任何人知道。既然已经登记进心跳了，watchdog 会报。
    """
    async def _wrapped(context):
        try:
            await fn(context)
            beat(name, True, interval)
        except Exception as e:                      # noqa: BLE001
            beat(name, False, interval, f"{type(e).__name__}: {e}")
            log.error(f"[任务] {name} 出错: {e}", exc_info=True)
    _wrapped.__name__ = f"tracked_{getattr(fn, '__name__', 'job')}"
    return _wrapped


def job_health():
    """返回 (有问题的任务列表, 全部任务快照)，供 watchdog 和 /datacheck 用。"""
    now = _time.time()
    bad = []
    for name, r in _BEATS.items():
        iv = r.get("interval") or 300
        if r["fails"] >= JOB_FAIL_STREAK:
            bad.append((name, f"连续失败 {r['fails']} 轮：{r['err']}"))
        elif r["last_ok"] and now - r["last_ok"] > iv * 4:
            mins = (now - r["last_ok"]) / 60
            bad.append((name, f"已经 {mins:.0f} 分钟没成功跑完（间隔应为 {iv}s）"))
    return bad, dict(_BEATS)


async def job_watchdog(context: ContextTypes.DEFAULT_TYPE):
    """定时检查任务心跳，出问题就私聊管理员。

    恢复也要报一声——只报坏不报好的话，他会一直不确定现在到底恢复没有。
    """
    bad, _all = job_health()
    bad_names = {n for n, _why in bad}

    new = [(n, why) for n, why in bad if n not in _job_alerted]
    if new:
        lines = ["🔴 后台任务异常（告警可能已经在静默失效）", ""]
        lines += [f"• {n}：{why}" for n, why in new]
        lines.append("")
        lines.append("这类问题的表现是「什么都没发生」，和「市场没异动」看起来一样，")
        lines.append("所以必须主动报。看容器日志里对应任务名的 traceback。")
        await notify_admin(context, "\n".join(lines))
        _job_alerted.update(bad_names)

    for n in list(_job_alerted - bad_names):
        _job_alerted.discard(n)
        await notify_admin(context, f"🟢 后台任务恢复：{n} 已经能正常跑完了")
