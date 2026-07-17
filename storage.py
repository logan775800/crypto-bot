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
# 确保新字段存在（兼容旧数据文件）
data.setdefault("alerts", [])
data.setdefault("holdings", {})
data.setdefault("broadcast_chats", [])  # 订阅了定时播报的群/私聊id列表
data.setdefault("market_watch", [])     # 订阅市场异动告警的chat_id
data.setdefault("alerted_coins", {})    # 已告警的币 {symbol: 时间戳}（冷却用）
data.setdefault("known_coins", [])      # 已知的OKX币种列表（检测新币用，旧版遗留）
data.setdefault("known_coins_ex", {})   # 各所已知币 {交易所: [币]}（多所新币检测）
data.setdefault("last_volumes_ex", {})  # 各所上轮成交额 {交易所: {币: 额}}（多所放量检测）
data.setdefault("coin_tiers", {})       # 分级告警：每个币已告警的台阶
data.setdefault("user_prefs", {})       # 用户偏好 {chat_id: {follows:[], threshold:20, quiet:[start,end]}}
data.setdefault("last_surge", {})       # 上轮异动币
data.setdefault("last_volumes", {})     # 上轮成交量（放量检测用）
data.setdefault("news_subs", [])        # 订阅新闻推送的chat_id
data.setdefault("pushed_news", [])      # 已推送的新闻链接（去重）
data.setdefault("unlock_subs", [])      # 订阅解锁提醒的chat_id
data.setdefault("alerted_unlocks", [])  # 已提醒的解锁事件（去重）
data.setdefault("summary_subs", [])     # 订阅每日总结的chat_id
data.setdefault("analysis_subs", [])    # 订阅每日分析推送的chat_id
data.setdefault("holding_watch", {})    # 持仓异动提醒 {uid: chat_id}
data.setdefault("holding_alerted", {})  # 持仓异动冷却记录
data.setdefault("gas_subs", {})         # gas提醒订阅 {chat_id: {"threshold":gwei,"armed":bool}}
data.setdefault("arb_subs", {})         # 套利监控订阅 {chat_id: {"threshold":pct}}
data.setdefault("arb_alerted", {})      # 套利告警冷却 {sym: 时间戳}
data.setdefault("whale_addr", {})       # 巨鲸地址追踪 {chat_id: {addr: {"label":..,"last":块高}}}
data.setdefault("whale_min", {})        # 地址追踪最小美元阈值 {chat_id: usd}
data.setdefault("ti_alerts", [])        # 技术指标告警订阅 [{chat_id,symbol,rsi_state,ma_state}]
data.setdefault("contract_watch", [])   # 订阅全交易所合约异动告警的chat_id
data.setdefault("contract_tiers", {})   # 合约分级告警记录 {交易所_币: {tier,dir,ts}}
data.setdefault("contract_alerted", {}) # 合约告警推送冷却 {币:方向:档位 -> ts}（防同一异动刷屏）
data.setdefault("grids", {})            # Bybit 永续网格 {chat_id:symbol: {区间/档位/挂单/成交/利润...}}
data.setdefault("watchpct", [])         # 持续波动监控 [{chat_id,symbol,pct,base,src,last_ts}]
data.setdefault("vtrade", {})           # 虚拟合约交易 {uid: {balance, positions{sym:{...}}, history[], chat_id}}
data.setdefault("rtrade_alert", {})     # 实盘爆仓预警 {enabled, threshold, chat_id, cooldown{sym:ts}}
data.setdefault("riskguard", {})        # 风险守护 {enabled, chat_id, checks{}, mmr/daily/conc/btc_drop 阈值, cooldown{}, day{date,start,fired}}
data.setdefault("brief", {})            # AI盘前简报每日推送 {enabled, chat_id}

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

    # 合约分档记录：48h 未更新的丢弃
    tiers = data.get("contract_tiers")
    if isinstance(tiers, dict):
        old = [k for k, v in tiers.items()
               if isinstance(v, dict) and now - v.get("ts", 0) > 2 * 86400]
        for k in old:
            tiers.pop(k, None)
        if old:
            removed["contract_tiers"] = len(old)

    # 已推新闻链接去重表：只留最近 500 条
    for key, cap in (("pushed_news", 500), ("alerted_unlocks", 500)):
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


def migrate_chat(old, new):
    """群升级为超级群时 chat_id 会变（旧 id 从此推送 400），把所有订阅从旧 id 搬到新 id。
    覆盖各类结构：id列表 / 带chat_id的字典列表 / 以chat_id为键的字典 / 值是chat_id的字段。
    返回迁移条数。"""
    old_set = {old, str(old)}
    moved = 0

    # 1) 纯 id 列表
    for key in ("broadcast_chats", "market_watch", "news_subs", "unlock_subs",
                "summary_subs", "analysis_subs", "contract_watch"):
        lst = data.get(key)
        if isinstance(lst, list):
            for i, x in enumerate(lst):
                if x in old_set:
                    lst[i] = new
                    moved += 1

    # 2) 元素是 {chat_id: ...} 的列表
    for key in ("watchpct", "alerts", "ti_alerts"):
        for w in data.get(key, []):
            if isinstance(w, dict) and w.get("chat_id") in old_set:
                w["chat_id"] = new
                moved += 1

    # 3) 以 chat_id(字符串) 为键的字典
    for key in ("gas_subs", "arb_subs", "whale_addr", "whale_min"):
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
    for key in ("rtrade_alert", "riskguard", "brief"):
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
