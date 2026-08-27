"""链上新币上线告警 —— 新池子刚建起来就推。

## 先说清这个功能最大的敌人不是技术，是刷屏

**实测（2026-08-27，GeckoTerminal `new_pools`）：光 BSC 一条链，
最近一小时就新建了 60 个池子，Solana 40 个** —— 而且那还是分页上限，
实际更多。全推等于每小时上百条，群当场废掉，然后所有人把机器人静音，
连真正有用的告警也一起听不到了。

所以这个功能的核心不是"能不能拿到新币"，是**筛掉 99%**。三层闸：

  1. **流动性** —— 池子太浅的买进去就出不来。实测 100 个新池里
     ≥1 万美元的只有 15 个、≥5 万的只有 4 个。
  2. **有人在交易** —— 建了池子没人买 = 还没成型，等它有量再说。
  3. **安全检查** —— 蜜罐/高税/能增发/LP 没锁，直接不推。
     这一层是这个机器人和"随便一个新币播报"的根本区别：
     推一个买得进卖不出的币，比不推有害得多。

## 「第一时间」和「不刷屏」是有矛盾的

刚建池的那一刻，它和当天另外几千个垃圾币**在数据上完全无法区分**——
没有流动性、没有交易、没有持币人。所谓"第一时间"如果指建池那一秒，
那就只能连垃圾一起推。
这里选的是**过了闸就推**：通常是建池后几分钟到一小时，代价是错过最开头那几分钟，
换来的是推出来的每一条都值得看一眼。**阈值可调**，他要更早就调低。
"""
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from config import is_admin
from storage import data, save_data
from handlers.util import safe_reply

log = logging.getLogger(__name__)

GT = "https://api.geckoterminal.com/api/v2"
# 链：GeckoTerminal 的网络名 -> (中文名, tokensec 的链键)
# tokensec 只认 evm 那几条 + solana，链名对不上会安静地跳过安全检查
CHAINS = {
    "bsc": ("BSC", "bsc"),
    "solana": ("Solana", "solana"),
    "base": ("Base", "base"),
    "eth": ("以太坊", "eth"),
}

# ── 默认阈值（每一条都是拿实测数据定的，别凭感觉调）───────────
MIN_LIQ = 50_000        # 池子流动性下限。实测 100 个新池里过这条的只有 4 个
MIN_TXNS = 30           # 1 小时内的买卖笔数：建了池没人买 = 还没成型
MAX_AGE_H = 24          # 太老的不算"新币"，那是漏推不是新上线
PAGES = 3               # 每条链翻几页（一页 20 个）
COOLDOWN = 7 * 86400    # 同一个池子多久内不重复推
SEEN_KEEP = 3000        # 记住多少个已推过的池子，防 data.json 无限长


def _cfg():
    return data.setdefault("newtoken", {"on": [], "min_liq": MIN_LIQ,
                                        "min_txns": MIN_TXNS, "seen": {}})


def subs():
    return [str(x) for x in _cfg().get("on", [])]


def is_on(chat_id):
    return str(chat_id) in subs()


def toggle(chat_id, on):
    c = _cfg()
    lst = [str(x) for x in c.get("on", [])]
    key = str(chat_id)
    if on and key not in lst:
        lst.append(key)
    if not on:
        lst = [x for x in lst if x != key]
    c["on"] = lst
    save_data()
    return on


def thresholds():
    c = _cfg()
    return int(c.get("min_liq") or MIN_LIQ), int(c.get("min_txns") or MIN_TXNS)


def set_threshold(min_liq=None, min_txns=None):
    c = _cfg()
    if min_liq is not None:
        c["min_liq"] = int(min_liq)
    if min_txns is not None:
        c["min_txns"] = int(min_txns)
    save_data()
    return thresholds()


def _seen():
    return _cfg().setdefault("seen", {})


def _mark(addr):
    s = _seen()
    s[addr] = int(time.time())
    # 封顶，否则 data.json 会一直长——新池子是无限供应的
    if len(s) > SEEN_KEEP:
        for k in sorted(s, key=lambda k: s[k])[:len(s) - SEEN_KEEP]:
            s.pop(k, None)


def _fresh(addr, now=None):
    now = now or time.time()
    return now - _seen().get(addr, 0) > COOLDOWN


# ── 取数 ────────────────────────────────────────────────────
async def fetch_new_pools(net, pages=PAGES, client=None):
    """某条链最近新建的池子。返回原始 attributes 列表（已带 address）。"""
    import httpx
    own = client is None
    c = client or httpx.AsyncClient(timeout=20)
    out = []
    try:
        for pg in range(1, pages + 1):
            r = await c.get(f"{GT}/networks/{net}/new_pools", params={"page": pg})
            if r.status_code != 200:
                break
            for it in (r.json().get("data") or []):
                a = dict(it.get("attributes") or {})
                a["pool_address"] = a.get("address") or it.get("id", "").split("_")[-1]
                a["_net"] = net
                # relationships 里才有**代币地址**（attributes 里只有池子地址）。
                # 安全检查要查的是代币不是池子——搞混的话查出来的是
                # 一个不存在的东西，而且不会报错。
                a["_relationships"] = it.get("relationships") or {}
                out.append(a)
    except Exception as e:
        log.info(f"[newtoken] {net} 取新池失败: {e}")
    finally:
        if own:
            await c.aclose()
    return out


def _age_hours(a, now=None):
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(str(a.get("pool_created_at")).replace("Z", "+00:00"))
    except Exception:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - t).total_seconds() / 3600


def _txns_1h(a):
    t = (a.get("transactions") or {}).get("h1") or {}
    try:
        return int(t.get("buys", 0)) + int(t.get("sells", 0))
    except (TypeError, ValueError):
        return 0


def passes(a, min_liq, min_txns, now=None):
    """三层闸的前两层（流动性 + 有人交易 + 够新）。安全检查另算，那要打接口。

    返回 (过不过, 没过的原因)。**原因要留着**——调阈值时他会问
    「为什么一条都没有」，那时得答得出是卡在哪一层。
    """
    age = _age_hours(a, now)
    if age is None or age > MAX_AGE_H:
        return False, "太老"
    try:
        liq = float(a.get("reserve_in_usd") or 0)
    except (TypeError, ValueError):
        liq = 0
    if liq < min_liq:
        return False, "流动性不足"
    if _txns_1h(a) < min_txns:
        return False, "还没人交易"
    return True, ""


def base_token_address(a):
    """池子里的**新币**地址。GeckoTerminal 的 name 形如 `景甜 / WBNB`，
    而安全检查要的是代币地址不是池子地址——这两个搞混的话，
    查出来的是交易对的安全性，那是个不存在的东西。"""
    rel = a.get("_relationships") or {}
    tok = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
    # 形如 "bsc_0xabc..."
    return tok.split("_", 1)[-1] if "_" in tok else ""


async def safety(chain_key, token_addr):
    """安全检查。返回 (能不能推, 摘要)。**查不出来时不推**——
    「没查到风险」和「没有风险」是两回事，而这条链上后者代价是归零。"""
    if not token_addr:
        return False, "拿不到代币地址"
    try:
        from handlers import tokensec
        res = await tokensec.check(chain_key, token_addr)
    except Exception as e:
        log.info(f"[newtoken] 安全检查失败 {token_addr[:10]}: {e}")
        return False, "安全检查没跑通"
    if not res or not res.get("ok"):
        return False, "安全检查没数据"
    if res.get("dangers"):
        return False, "有致命风险：" + res["dangers"][0][:40]
    return True, "；".join(res.get("warnings", [])[:2])


def format_alert(a, chain_cn, warn):
    """一条新币告警。**要给出足够判断的东西，而不是只报个名字。**"""
    name = str(a.get("name") or "?").strip()
    liq = float(a.get("reserve_in_usd") or 0)
    fdv = float(a.get("fdv_usd") or 0)
    age = _age_hours(a) or 0
    n = _txns_1h(a)
    t = (a.get("transactions") or {}).get("h1") or {}
    buys, sells = int(t.get("buys", 0)), int(t.get("sells", 0))
    addr = base_token_address(a)
    lines = [
        f"🌱 *链上新币* · {chain_cn}",
        f"*{name}*",
        "",
        f"池子建了 {age:.1f} 小时　流动性 ${liq:,.0f}",
        f"1h 成交 {n} 笔（买 {buys} / 卖 {sells}）",
    ]
    if fdv:
        lines.append(f"FDV ${fdv:,.0f}")
    if warn:
        lines.append(f"⚠️ {warn}")
    if addr:
        lines.append("")
        lines.append(f"合约 `{addr}`")
        lines.append(f"查它：`/oc {addr}`")
    lines.append("")
    lines.append("⚠️ 新币九成归零。这里只筛掉了蜜罐/高税/流动性太浅的，"
                 "**不代表它是好项目**。仓位当彩票买，别当投资。")
    return "\n".join(lines)


async def scan(context: ContextTypes.DEFAULT_TYPE):
    """后台任务：扫各链新池，过闸的推给订阅者。

    两套订阅并行：
      · 普通（`on`）——高门槛，主流新币
      · 热点（`hot`）——中文梗币，门槛低得多，见 `is_hot_name` 那段的实测
    """
    chats = subs()
    hot_chats = [c for c in _cfg().get("hot") or []]
    if not chats and not hot_chats:
        return
    import httpx
    min_liq, min_txns = thresholds()
    hits, hot_hits = [], []
    async with httpx.AsyncClient(timeout=20) as c:
        for net, (cn, sec_key) in CHAINS.items():
            pools = await fetch_new_pools(net, client=c)
            # 热点模式：中文名 + 低门槛。**先挑再查安全**——
            # 安全检查一次一个接口调用，对着 60 个池子挨个查会被限频。
            if hot_chats:
                for a in pools:
                    addr = a.get("pool_address") or ""
                    if not addr or not _fresh(addr) or not is_hot_name(a):
                        continue
                    ok, _w = passes(a, HOT_MIN_LIQ, HOT_MIN_TXNS)
                    if not ok:
                        continue
                    good, warn = await safety(sec_key, base_token_address(a))
                    _mark(addr)
                    if good:
                        n_same, rank = rank_same_name(pools, a)
                        hot_hits.append((a, cn, warn, n_same, rank))
            for a in (pools if chats else []):
                addr = a.get("pool_address") or ""
                if not addr or not _fresh(addr):
                    continue
                ok, _why = passes(a, min_liq, min_txns)
                if not ok:
                    continue
                good, warn = await safety(sec_key, base_token_address(a))
                _mark(addr)          # 查过就记账，免得下轮又花一次安全检查
                if good:
                    hits.append((a, cn, warn))
    if hits or hot_hits:
        save_data()

    async def _push(targets, text):
        for cid in targets:
            try:
                await context.bot.send_message(int(cid), text, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"[newtoken] 推送失败 {cid}: {e}")

    for a, cn, warn in hits[:5]:     # 一轮最多 5 条，再多就是刷屏
        await _push(chats, format_alert(a, cn, warn))
    # 热点这一路更容易扎堆（同一个梗好几个合约），封得更紧一点。
    # 同名的只推流动性最大的那个——其余是跟风盘，推出来只会稀释注意力。
    # 同名扎堆时按「有几个在抄」排序：抄的人越多说明这个梗越热，先推那个。
    seen_names = set()
    sent = 0
    for a, cn, warn, n_same, rank in sorted(hot_hits, key=lambda x: -x[3]):
        nm = base_name(a)
        if nm in seen_names or rank != 1:
            continue
        seen_names.add(nm)
        await _push([str(x) for x in hot_chats],
                    format_hot(a, cn, warn, n_same, rank))
        sent += 1
        if sent >= 4:
            break


# ── 命令 ────────────────────────────────────────────────────
async def newtoken_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/newtoken —— 链上新币上线告警的开关与阈值。"""
    chat_id = update.effective_chat.id
    args = [x.lower() for x in (context.args or [])]
    min_liq, min_txns = thresholds()

    if args and args[0] in ("on", "开", "订阅"):
        toggle(chat_id, True)
        await safe_reply(update.message,
                         f"🌱 已订阅**链上新币告警**\n"
                         f"当前门槛：流动性 ≥ ${min_liq:,}　1h 成交 ≥ {min_txns} 笔\n"
                         f"每 10 分钟扫一次 BSC/Solana/Base/以太坊的新池子。\n\n"
                         f"⚠️ 实测**光 BSC 一小时就有 60 个新池**，所以门槛不能低——"
                         f"全推的话群当场就废了。想更早看到就 `/newtoken liq 20000`，"
                         f"但垃圾会明显变多。\n取消：`/newtoken off`",
                         parse_mode="Markdown")
        return
    if args and args[0] in ("off", "关", "退订"):
        toggle(chat_id, False)
        toggle_hot(chat_id, False)
        await safe_reply(update.message, "🔕 已关闭链上新币告警（含热点模式）")
        return
    if args and args[0] in ("hot", "热点", "梗币"):
        on = not hot_enabled(chat_id)
        toggle_hot(chat_id, on)
        if not on:
            await safe_reply(update.message, "🔕 已关闭中文热点新币")
            return
        await safe_reply(update.message,
            f"🔥 已开启**中文热点新币**\n\n"
            f"抓的是「我的女友景甜」「牛来」这一类——名字带中文的梗币。\n"
            f"实测 BSC 新池里这类占 **20%**，而它们的流动性通常只有 "
            f"**几千到两万美元**，所以普通模式（门槛 5 万）会把它们全筛掉。\n\n"
            f"这一路的门槛：流动性 ≥ ${HOT_MIN_LIQ:,}、1h 成交 ≥ {HOT_MIN_TXNS} 笔、"
            f"过安全检查。\n"
            f"同名合约只推**流动性最大的那个**，并告诉你一共有几个在抄。\n\n"
            f"⚠️ 门槛压这么低意味着盘子极浅、随时归零。"
            f"而且我**不判断有没有潜力**——那不是数据能给的，"
            f"这里给的是「有人在真交易、且不是蜜罐」。",
            parse_mode="Markdown")
        return
    if len(args) >= 2 and args[0] in ("liq", "流动性"):
        try:
            v = int(float(args[1]))
        except ValueError:
            await safe_reply(update.message, "要个数字，例：`/newtoken liq 20000`",
                             parse_mode="Markdown")
            return
        if not is_admin(update.effective_user.id):
            await safe_reply(update.message, "只有管理员能改门槛")
            return
        a, b = set_threshold(min_liq=max(1000, v))
        await safe_reply(update.message,
                         f"门槛已改：流动性 ≥ ${a:,}　1h 成交 ≥ {b} 笔")
        return
    if len(args) >= 2 and args[0] in ("txns", "笔数"):
        if not is_admin(update.effective_user.id):
            await safe_reply(update.message, "只有管理员能改门槛")
            return
        try:
            v = int(args[1])
        except ValueError:
            await safe_reply(update.message, "要个数字，例：`/newtoken txns 50`",
                             parse_mode="Markdown")
            return
        a, b = set_threshold(min_txns=max(0, v))
        await safe_reply(update.message,
                         f"门槛已改：流动性 ≥ ${a:,}　1h 成交 ≥ {b} 笔")
        return

    state = "✅ 已订阅" if is_on(chat_id) else "⬜ 未订阅"
    await safe_reply(update.message,
        f"🌱 *链上新币告警*　{state}\n\n"
        f"新池子刚建起来、且**过了筛**就推到这个会话。\n\n"
        f"当前门槛：\n"
        f"· 流动性 ≥ ${min_liq:,}\n"
        f"· 1h 成交 ≥ {min_txns} 笔\n"
        f"· 池子建立 24 小时内\n"
        f"· 过安全检查（蜜罐/不可卖/高税/能增发 一律不推）\n\n"
        f"**为什么门槛不能低**：实测光 BSC 一条链，一小时就有 60 个新池、"
        f"Solana 40 个。全推等于每小时上百条，群会被刷废。\n"
        f"100 个新池里流动性 ≥1 万的只有 15 个、≥5 万的只有 4 个。\n\n"
        f"`/newtoken on` 开　`/newtoken off` 关\n"
        f"`/newtoken liq 20000` 调流动性门槛　`/newtoken txns 50` 调笔数门槛",
        parse_mode="Markdown")


# ── 热点模式：中文梗币这一类 ──────────────────────────────────
# 他要的是「我的女友景甜」「牛来」这种——**默认门槛会把它们全筛掉**。
# 实测（2026-08-27，BSC 100 个新池）：
#   · 名字带中文的 20 个（20%）——他举的两个例子都在里面
#   · 这类的流动性在 **$3k~$20k**，远低于默认的 5 万
#   · 「我的女友景甜」同一时刻有 **4 个不同合约**，税率从 0.25% 到 4.82% 不等
#   · 其中两个**流动性只有 $12 却有 60 笔成交** —— 典型假盘
#
# 所以热点模式换一套判据：门槛压到几千，但要求
#   ① 名字带中文（这一类的共同特征）
#   ② 有人在真交易
#   ③ 过安全检查（这条不放松，越是热点越多人抄）
#   ④ **报出同名撞车数**——同一个梗有几个合约在跑本身就是热度指标，
#      同时也提醒他大部分是跟风盘
import re as _re

CJK = _re.compile(r"[\u4e00-\u9fff]")
HOT_MIN_LIQ = 3_000     # 再低就是那种「$12 流动性 60 笔成交」的假盘
HOT_MIN_TXNS = 20


def is_hot_name(a):
    """名字带中文 = 中文社区的梗币。他要的就是这一类。

    **刻意用「带中文」而不是「像 meme」**：后者没法用数据判，
    前者是这批币真实、稳定的共同特征，而且他自己举的两个例子都命中。
    """
    return bool(CJK.search(str(a.get("name") or "")))


def base_name(a):
    """`我的女友景甜 / USDT 0.25%` → `我的女友景甜`。同名撞车靠它归组。"""
    return str(a.get("name") or "").split("/")[0].strip()


def hot_enabled(chat_id):
    c = _cfg()
    return str(chat_id) in [str(x) for x in (c.get("hot") or [])]


def toggle_hot(chat_id, on):
    c = _cfg()
    lst = [str(x) for x in (c.get("hot") or [])]
    key = str(chat_id)
    if on and key not in lst:
        lst.append(key)
    if not on:
        lst = [x for x in lst if x != key]
    c["hot"] = lst
    save_data()
    return on


def rank_same_name(pools, a):
    """这个梗一共几个合约在跑，以及**这个是不是流动性最大的那个**。

    同名多个 = 梗正在热（有人在抄），但也意味着大部分是跟风盘。
    只报数量不说排名等于把最关键的那句省掉——他需要知道
    自己看到的是"原盘"还是"第 3 个抄的"。
    """
    name = base_name(a)
    same = [p for p in pools if base_name(p) == name]
    def _liq(p):
        try:
            return float(p.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            return 0.0
    ordered = sorted(same, key=_liq, reverse=True)
    rank = 1
    for i, p in enumerate(ordered, 1):
        if p.get("pool_address") == a.get("pool_address"):
            rank = i
            break
    return len(same), rank


def format_hot(a, chain_cn, warn, n_same, rank):
    liq = float(a.get("reserve_in_usd") or 0)
    age = _age_hours(a) or 0
    n = _txns_1h(a)
    t = (a.get("transactions") or {}).get("h1") or {}
    addr = base_token_address(a)
    lines = [f"🔥 *中文热点新币* · {chain_cn}",
             f"*{base_name(a)}*", ""]
    if n_same > 1:
        who = "**流动性最大的那个**" if rank == 1 else f"流动性排第 {rank}"
        lines.append(f"⚡ 同名合约有 **{n_same} 个**在跑——这个梗正在热。"
                     f"这条是{who}")
        lines.append("　（同名多个 = 有人在抄，大部分是跟风盘，认准合约地址）")
    lines += [
        f"池子建了 {age * 60:.0f} 分钟　流动性 ${liq:,.0f}",
        f"1h 成交 {n} 笔（买 {t.get('buys', 0)} / 卖 {t.get('sells', 0)}）",
    ]
    if warn:
        lines.append(f"⚠️ {warn}")
    if addr:
        lines += ["", f"合约 `{addr}`", f"查它：`/oc {addr}`"]
    lines += ["",
              "⚠️ 这类币是**彩票**：门槛压到几千美元流动性才抓得到，"
              "意味着盘子极浅、随时归零。只筛掉了蜜罐和假盘，"
              "**没有任何「有潜力」的判断**——那不是数据能给的。"]
    return "\n".join(lines)
