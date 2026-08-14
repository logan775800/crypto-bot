import os
import json
import logging
from config import DATA_FILE

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"读取数据出错: {e}")
    return {"alerts": [], "holdings": {}, "broadcast_chats": []}

# 全局数据
data = load_data()


def apply_defaults(d=None):
    """补齐所有字段的默认值。**可重复调用** —— /restore 恢复老备份后
    必须再跑一次，否则老文件缺的字段会让 handler 在 data[...] 上 KeyError。
    """
    d = data if d is None else d
    d.setdefault("alerts", [])
    d.setdefault("holdings", {})
    d.setdefault("broadcast_chats", [])  # 订阅了定时播报的群/私聊id列表
    d.setdefault("market_watch", [])     # 订阅市场异动告警的chat_id
    d.setdefault("alerted_coins", {})    # 已告警的币 {symbol: 时间戳}（冷却用）
    d.setdefault("known_coins", [])      # 已知的OKX币种列表（检测新币用，旧版遗留）
    d.setdefault("known_coins_ex", {})   # 各所已知币 {交易所: [币]}（多所新币检测）
    d.setdefault("last_volumes_ex", {})  # 各所上轮成交额 {交易所: {币: 额}}（多所放量检测）
    d.setdefault("coin_tiers", {})       # 分级告警：每个币已告警的台阶
    d.setdefault("user_prefs", {})       # 用户偏好 {chat_id: {follows:[], threshold:20, quiet:[start,end]}}
    d.setdefault("last_surge", {})       # 上轮异动币
    d.setdefault("last_volumes", {})     # 上轮成交量（放量检测用）
    d.setdefault("news_subs", [])        # 订阅新闻推送的chat_id
    d.setdefault("pushed_news", [])      # 已推送的新闻链接（去重）
    d.setdefault("unlock_subs", [])      # 订阅解锁提醒的chat_id
    d.setdefault("alerted_unlocks", [])  # 已提醒的解锁事件（去重）
    d.setdefault("summary_subs", [])     # 订阅每日总结的chat_id
    d.setdefault("analysis_subs", [])    # 订阅每日分析推送的chat_id
    d.setdefault("holding_watch", {})    # 持仓异动提醒 {uid: chat_id}
    d.setdefault("holding_alerted", {})  # 持仓异动冷却记录
    d.setdefault("gas_subs", {})         # gas提醒订阅 {chat_id: {"threshold":gwei,"armed":bool}}
    d.setdefault("arb_subs", {})         # 套利监控订阅 {chat_id: {"threshold":pct}}
    d.setdefault("arb_alerted", {})      # 套利告警冷却 {sym: 时间戳}
    d.setdefault("whale_addr", {})       # 巨鲸地址追踪 {chat_id: {addr: {"label":..,"last":块高}}}
    d.setdefault("whale_min", {})        # 地址追踪最小美元阈值 {chat_id: usd}
    d.setdefault("ti_alerts", [])        # 技术指标告警订阅 [{chat_id,symbol,rsi_state,ma_state}]
    d.setdefault("contract_watch", [])   # 订阅全交易所合约异动告警的chat_id
    d.setdefault("contract_tiers", {})   # 合约分级告警记录 {交易所_币: {tier,dir,ts}}
    d.setdefault("contract_alerted", {}) # 合约告警推送冷却 {币:方向:档位 -> ts}（防同一异动刷屏）
    d.setdefault("grids", {})            # Bybit 永续网格 {chat_id:symbol: {区间/档位/挂单/成交/利润...}}
    d.setdefault("watchpct", [])         # 持续波动监控 [{chat_id,symbol,pct,base,src,last_ts}]
    d.setdefault("vtrade", {})           # 虚拟合约交易 {uid: {balance, positions{sym:{...}}, history[], chat_id}}
    d.setdefault("rtrade_alert", {})     # 实盘爆仓预警 {enabled, threshold, chat_id, cooldown{sym:ts}}
    d.setdefault("riskguard", {})        # 风险守护 {enabled, chat_id, checks{}, mmr/daily/conc/btc_drop 阈值, cooldown{}, day{date,start,fired}}
    d.setdefault("brief", {})            # AI盘前简报每日推送 {enabled, chat_id}
    d.setdefault("cond_alerts", [])      # 条件触发提醒 [{chat_id,symbol,conds[],last_ts}]
    d.setdefault("plans", [])            # 交易计划 [{id,chat_id,symbol,side,status,trigger,entry,stop,tps,invalid,...}]
    d.setdefault("plan_seq", 0)          # 计划自增号（按钮 callback_data 要短 id）
    d.setdefault("fex_subs", {})         # 资金费极值订阅 {chat_id: {threshold}}
    d.setdefault("fex_alerted", {})      # 资金费极值推送冷却 {chat:ex:币:方向 -> ts}
    d.setdefault("ai_model_override", "")  # /aimodel 手动指定的AI模型（空=自动降级）
    d.setdefault("pump_watch", {})       # 15m急涨急跌订阅 {chat_id: {"pct": 阈值}}
    d.setdefault("pump_alerted", {})     # 急涨急跌去重 {chat_id: {币: {up,down,ts}}}
    d.setdefault("trading_disabled", False)  # 实盘下单总开关(killswitch)
    d.setdefault("audit_log", [])         # 实盘操作审计
    d.setdefault("risk_profile", {})      # 个性化风控参数 {uid: {...}}
    d.setdefault("weekly_subs", [])       # 周报订阅 chat_id
    d.setdefault("weekly_snap", {})       # 上周行为快照（算漂移用）
    d.setdefault("event_subs", {})        # 事件驱动预警订阅 {chat_id: {symbols:[]}}
    d.setdefault("event_state", {})       # 各币上一轮状态快照（判"切换"的基线）
    d.setdefault("event_cooldown", {})    # 事件推送冷却 {币:事件 -> ts}
    d.setdefault("contract_min_tier", {})  # 合约告警每群最低档 {chat_id: 20/30/50/100}
    d.setdefault("announced_version", "")   # 已经向订阅会话播报过更新的版本（防每次重启都刷屏）
    return d


apply_defaults()

def prune_data(now=None):
    """治理 data.json 无限增长：清掉过期冷却/去重记录、给历史类列表封顶。
    每次 save_data 都是全量重写 JSON，文件越大越慢越危险，所以定期修剪。
    返回 {字段: 清掉的条数}。"""
    import time as _t
    now = now or _t.time()
    removed = {}

    def _drop_old_ts(key, max_age):
        """{k: 时间戳} 形式的冷却字典。"""
        d = data.get(key)
        if not isinstance(d, dict):
            return
        old = [k for k, v in d.items() if isinstance(v, (int, float)) and now - v > max_age]
        for k in old:
            d.pop(k, None)
        if old:
            removed[key] = len(old)

    _drop_old_ts("alerted_coins", 7 * 86400)      # 现货异动告警冷却
    _drop_old_ts("contract_alerted", 2 * 86400)   # 合约告警推送冷却
    _drop_old_ts("arb_alerted", 7 * 86400)        # 套利告警冷却
    _drop_old_ts("fex_alerted", 7 * 86400)        # 资金费极值告警冷却

    # 合约分档记录：48h 未更新的丢弃
    tiers = data.get("contract_tiers")
    if isinstance(tiers, dict):
        old = [k for k, v in tiers.items()
               if isinstance(v, dict) and now - v.get("ts", 0) > 2 * 86400]
        for k in old:
            tiers.pop(k, None)
        if old:
            removed["contract_tiers"] = len(old)

    # 已推新闻链接去重表 / 审计日志：只留最近 N 条
    for key, cap in (("pushed_news", 500), ("alerted_unlocks", 500),
                     ("audit_log", 500)):
        lst = data.get(key)
        if isinstance(lst, list) and len(lst) > cap:
            removed[key] = len(lst) - cap
            data[key] = lst[-cap:]

    # 虚拟合约历史：每人只留最近 200 笔
    for uid, acct in (data.get("vtrade") or {}).items():
        h = acct.get("history") if isinstance(acct, dict) else None
        if isinstance(h, list) and len(h) > 200:
            removed[f"vtrade[{uid}].history"] = len(h) - 200
            acct["history"] = h[-200:]

    # 实盘爆仓预警 / 风险守护 冷却
    for key in ("rtrade_alert", "riskguard"):
        ra = data.get(key)
        if isinstance(ra, dict) and isinstance(ra.get("cooldown"), dict):
            cd = ra["cooldown"]
            old = [k for k, v in cd.items()
                   if isinstance(v, (int, float)) and now - v > 2 * 86400]
            for k in old:
                cd.pop(k, None)
            if old:
                removed[f"{key}.cooldown"] = len(old)

    if removed:
        save_data()
    return removed


# chat_id 都存在哪儿——按结构分四类。migrate_chat（搬家）和 subscribed_chats（找人）
# 必须看同一份清单：以前只有 migrate_chat 知道，再写一个就会漏掉后加的订阅类型，
# 而漏掉的表现是「某个群搬完家收不到推送」或「更新播报少一个群」，都很难查。
_ID_LISTS = ("broadcast_chats", "market_watch", "news_subs", "unlock_subs",
             "summary_subs", "analysis_subs", "contract_watch", "weekly_subs")
_DICT_LISTS = ("watchpct", "alerts", "ti_alerts", "cond_alerts", "plans")
_ID_KEYED = ("gas_subs", "arb_subs", "whale_addr", "whale_min", "fex_subs",
             "pump_watch", "event_subs", "contract_min_tier")
_EMBEDDED = ("rtrade_alert", "riskguard", "brief")


def subscribed_chats():
    """所有订阅过本机器人任意推送的会话 id（去重）。

    用来做「有事要通知所有人」的目标集合，比如版本更新播报 ——
    以前只发管理员私聊，群里的人根本不知道机器人换了行为。
    """
    out = []
    seen = set()

    def add(cid):
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return
        if cid not in seen:
            seen.add(cid)
            out.append(cid)

    for key in _ID_LISTS:
        for x in data.get(key) or []:
            add(x)
    for key in _DICT_LISTS:
        for w in data.get(key) or []:
            if isinstance(w, dict):
                add(w.get("chat_id"))
    for key in _ID_KEYED:
        d = data.get(key)
        if isinstance(d, dict):
            for k in d:
                add(k)
    for key in _EMBEDDED:
        d = data.get(key)
        if isinstance(d, dict):
            add(d.get("chat_id"))
    for acct in (data.get("vtrade") or {}).values():
        if isinstance(acct, dict):
            add(acct.get("chat_id"))
    for cid in (data.get("holding_watch") or {}).values():
        add(cid)
    return out


def migrate_chat(old, new):
    """群升级为超级群时 chat_id 会变（旧 id 从此推送 400），把所有订阅从旧 id 搬到新 id。
    覆盖各类结构：id列表 / 带chat_id的字典列表 / 以chat_id为键的字典 / 值是chat_id的字段。
    返回迁移条数。"""
    old_set = {old, str(old)}
    moved = 0

    # 1) 纯 id 列表
    for key in _ID_LISTS:
        lst = data.get(key)
        if isinstance(lst, list):
            for i, x in enumerate(lst):
                if x in old_set:
                    lst[i] = new
                    moved += 1

    # 2) 元素是 {chat_id: ...} 的列表
    for key in _DICT_LISTS:
        for w in data.get(key, []):
            if isinstance(w, dict) and w.get("chat_id") in old_set:
                w["chat_id"] = new
                moved += 1

    # 3) 以 chat_id(字符串) 为键的字典
    for key in _ID_KEYED:
        d = data.get(key)
        if isinstance(d, dict):
            for ov in (str(old), old):
                if ov in d:
                    d[str(new)] = d.pop(ov)
                    moved += 1

    # 4) 值是 chat_id 的：holding_watch {uid: chat_id}
    hw = data.get("holding_watch", {})
    for uid, cid in list(hw.items()):
        if cid in old_set:
            hw[uid] = new
            moved += 1

    # 5) 内嵌 chat_id 字段
    for key in _EMBEDDED:
        ra = data.get(key, {})
        if isinstance(ra, dict) and ra.get("chat_id") in old_set:
            ra["chat_id"] = new
            moved += 1
    for acct in data.get("vtrade", {}).values():
        if isinstance(acct, dict) and acct.get("chat_id") in old_set:
            acct["chat_id"] = new
            moved += 1

    if moved:
        save_data()
    return moved


def save_data():
    # 原子写入：先写临时文件再 os.replace，避免写盘中途被打断（多个定时任务并发保存）
    # 导致 data.json 只写了一半而损坏，下次启动整份数据丢失
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        logging.error(f"保存数据出错: {e}")
