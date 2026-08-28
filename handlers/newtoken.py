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
# 每条链翻几页（一页 20 个）。**这个数不是随便定的，翻少了会静默丢池子**：
# 实测各链每 10 分钟出多少新池 —— Solana 102、BSC 79、Base 72、以太坊 52。
# 原来是「每 10 分钟扫一次、每链翻 3 页(60 个)」，等于 Solana 有四成新池
# 从来没被看见过。而这种漏是**看不出来的**：计数偏低、告警只是"没响"。
# 现在改成 5 分钟一轮、每链 4 页(80 个)，最忙的 Solana 也只出 51 个，留了余量。
PAGES = 4
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
    burst_chats = [str(x) for x in (_cfg().get("burst") or [])]
    if not chats and not hot_chats and not burst_chats:
        return
    import httpx
    min_liq, min_txns = thresholds()
    hits, hot_hits = [], []
    burst_pool_chain = {}          # 池子地址 -> (链中文名, 安全检查用的 key)
    async with httpx.AsyncClient(timeout=20) as c:
        for net, (cn, sec_key) in CHAINS.items():
            pools = await fetch_new_pools(net, client=c)
            # 梗爆发的窗口**每一轮都要喂**，和有没有订阅者无关顺序：
            # 计数是 30 分钟的滑动窗口，中途开订阅的人也该立刻有数可用。
            # 放在最前面还有一层意思——它不受下面那些门槛影响，
            # 一个 500 美元流动性的池子照样算一次"有人在抄"。
            if burst_chats:
                burst_track(pools)
                for p in pools:
                    if p.get("pool_address"):
                        burst_pool_chain[p["pool_address"]] = (cn, sec_key)
            # 热点模式：中文名 + 低门槛。**先挑再查安全**——
            # 安全检查一次一个接口调用，对着 60 个池子挨个查会被限频。
            if hot_chats and hot_quota_left() > 0:
                _lv, _liq, _tx, _same = hot_level()
                for a in pools:
                    addr = a.get("pool_address") or ""
                    if not addr or not _fresh(addr) or not is_hot_name(a):
                        continue
                    ok, _w = passes(a, _liq, _tx)
                    if not ok:
                        continue
                    # 同名下限：抄袭要花钱建池子，有人愿意抄说明梗真在被讨论。
                    # 这一条比流动性更接近"有热度"，也是他嫌吵之后最有效的那道闸。
                    n_same_pre, _rk = rank_same_name(pools, a)
                    if n_same_pre < _same:
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
    for a, cn, warn, n_same, rank in sorted(hot_hits, key=lambda x: -x[3]):
        if hot_quota_left() <= 0:
            break                    # 这一小时的配额用完了，剩下的下小时再说
        nm = base_name(a)
        if nm in seen_names or rank != 1:
            continue
        seen_names.add(nm)
        await _push([str(x) for x in hot_chats],
                    format_hot(a, cn, warn, n_same, rank))
        _hot_used(1)

    # 梗爆发：推的是**梗**不是池子，所以一个梗只发一条，附流动性最大的那个当入口
    if burst_chats:
        ready = [h for h in burst_hits() if not burst_cooled(h[0])]
        for i, (nm, n, span, pools) in enumerate(ready):
            if burst_quota_left() <= 0:
                break
            top = pools[0]
            cn, sec_key = burst_pool_chain.get(top.get("pool_address"), ("链上", None))
            good, warn = (True, "") if not sec_key else \
                await safety(sec_key, base_token_address(top))
            if not good:
                continue        # 蜜罐/假盘，热度再高也不推
            burst_mark(nm)
            await _push(burst_chats,
                        format_burst(nm, n, span, pools, cn, warn,
                                     dropped=max(0, len(ready) - i - 1)
                                     if burst_quota_left() <= 1 else 0))
            _burst_used(1)
        save_data()


async def burst_selftest():
    """现在窗口里都有什么。**低频告警必须能自检**：
    几小时不响的时候，「没有梗在爆」和「扫描挂了」看起来完全一样。

    窗口是内存里的滑动 30 分钟，重启后要攒一会儿——所以这里
    **把攒了多久也印出来**，否则刚重启看到空的会以为坏了。
    """
    lv, need = burst_level()
    lines = [f"🀄 *梗爆发 · 自检*（{lv}档：中文名 ≥{need}，"
             f"英文名 ≥{need * BURST_EN_FACTOR}）"]
    if not _burst:
        lines.append("")
        lines.append("窗口是空的。要么刚重启（滑动窗口 30 分钟，得攒一会儿），"
                     "要么扫描没在跑——发 /datacheck 看「链上新币」那一行的心跳。")
        return "\n".join(lines)
    now = time.time()
    tot = sum(len(v) for v in _burst.values())
    oldest = min((v[0] for slot in _burst.values() for v in slot.values()),
                 default=now)
    lines.append(f"窗口里 {len(_burst)} 个名字、{tot} 个池子，"
                 f"攒了 {int((now - oldest) / 60)} 分钟")
    hits = burst_hits(now)
    if hits:
        lines.append("")
        lines.append(f"*到线的（{len(hits)} 个）*")
        for nm, n, span, pools in hits[:5]:
            cooled = "（冷却中，不重复报）" if burst_cooled(nm, now) else ""
            lines.append(f"　🀄 {nm}　{n} 次 / {int(span // 60)} 分钟"
                         f"　最大 ${_liq_of(pools[0]):,.0f}{cooled}")
    else:
        lines.append("")
        lines.append("没有到线的。下面是**离线最近的几个**，"
                     "看着都差不多的话说明门槛偏高：")
    top = sorted(((nm, len(s)) for nm, s in _burst.items()),
                 key=lambda x: -x[1])[:6]
    lines.append("")
    lines.append("*被抄最多的*")
    for nm, n in top:
        flag = "🀄" if CJK.search(nm) else "🅰️"
        want = need if CJK.search(nm) else need * BURST_EN_FACTOR
        lines.append(f"　{flag} {nm[:20]}　{n} 次（这类要 {want} 次）")
    return "\n".join(lines)


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
        toggle_burst(chat_id, False)
        await safe_reply(update.message, "🔕 已关闭链上新币告警（含热点模式和梗爆发）")
        return
    if args and args[0] in ("burst", "爆发", "梗", "热度"):
        # 自检。这类告警可能几小时不响，「没响」和「坏了」在屏幕上一模一样——
        # 低频告警不配自检，等于让人靠猜。
        if len(args) >= 2 and args[1] in ("now", "自检", "现在"):
            await safe_reply(update.message, await burst_selftest(),
                             parse_mode="Markdown")
            return
        if len(args) >= 2:
            got = set_burst_level(context.args[1].strip())
            if not got:
                await safe_reply(update.message,
                                 "档位只有：" + "、".join(BURST_LEVELS) + "\n"
                                 "例：`/newtoken burst 严`", parse_mode="Markdown")
                return
            lv, need = got
            await safe_reply(update.message,
                f"🀄 梗爆发已切到**{lv}**档\n"
                f"判据：30 分钟内同名新池 ≥ **{need}** 个"
                f"（英文名要 ≥ {need * BURST_EN_FACTOR} 个）\n"
                f"实测频率：宽 ~8 条/小时、标准 ~4 条/小时、严 ~1 条/小时\n"
                f"兜底闸：每小时最多 {BURST_PER_HOUR} 条",
                parse_mode="Markdown")
            return
        on = not burst_enabled(chat_id)
        toggle_burst(chat_id, on)
        if not on:
            await safe_reply(update.message, "🔕 已关闭梗爆发告警")
            return
        lv, need = burst_level()
        await safe_reply(update.message,
            f"🀄 已开启**梗爆发**（{lv}档）\n\n"
            f"和另外两个不一样：它推的是**梗**，不是池子。\n"
            f"判据是「**30 分钟内同一个名字被抄了几次**」，"
            f"不是「哪个池子够大」。\n\n"
            f"为什么这么判：抄要花钱建池子。实测 8 分钟里「甜甜币」被抄 **7 次**，"
            f"而「牛来」「我的女友景甜」各只有 1 次——后两个的热度是几天前的。"
            f"短时间内有这么多人愿意花钱抄，说明这个梗**此刻**正在被讨论。\n"
            f"它和流动性正交，所以一个 500 美元的池子照样算一次「有人在抄」，"
            f"能比「等池子做大」早一大截。\n\n"
            f"当前判据：30 分钟内同名新池 ≥ **{need}** 个"
            f"（英文名要 ≥ {need * BURST_EN_FACTOR} 个，那一列噪音太多）\n"
            f"另有两道护栏：抄袭要散布在 {BURST_MIN_SPAN} 秒以上"
            f"（同一秒批量建池是一个人在刷，不是热度）；链名币名不算梗。\n"
            f"每小时最多 {BURST_PER_HOUR} 条。\n\n"
            f"嫌少：`/newtoken burst 宽`　嫌吵：`/newtoken burst 严`\n"
            f"看现在窗口里有什么：`/newtoken burst now`",
            parse_mode="Markdown")
        return
    if args and args[0] in ("hot", "热点", "梗币"):
        # /newtoken hot 宽|标准|严 —— 直接换档；不带参数才是开关
        if len(args) >= 2:
            got = set_hot_level(context.args[1].strip())
            if not got:
                await safe_reply(update.message,
                                 "档位只有：" + "、".join(HOT_LEVELS) + "\n"
                                 "例：`/newtoken hot 严`", parse_mode="Markdown")
                return
            lv, liq, tx, same = got
            same_txt = f"　且**同名至少 {same} 个**在跑" if same > 1 else ""
            await safe_reply(update.message,
                f"🔥 已切到**{lv}**档\n"
                f"流动性 ≥ ${liq:,}　1h 成交 ≥ {tx} 笔{same_txt}\n"
                f"实测频率：宽 6 条/小时、标准 3 条/小时、严 1 条/小时\n"
                f"另有兜底闸：每小时最多 {HOT_PER_HOUR} 条",
                parse_mode="Markdown")
            return
        on = not hot_enabled(chat_id)
        toggle_hot(chat_id, on)
        if not on:
            await safe_reply(update.message, "🔕 已关闭中文热点新币")
            return
        lv, liq, tx, same = hot_level()
        same_txt = f"、**同名至少 {same} 个在跑**" if same > 1 else ""
        await safe_reply(update.message,
            f"🔥 已开启**中文热点新币**（{lv}档）\n\n"
            f"抓的是「我的女友景甜」「牛来」这一类——名字带中文的梗币。\n"
            f"这类的流动性通常只有几千到两万美元，普通模式（门槛 5 万）会全筛掉。\n\n"
            f"当前门槛：流动性 ≥ ${liq:,}、1h 成交 ≥ {tx} 笔{same_txt}、过安全检查\n"
            f"每小时最多 {HOT_PER_HOUR} 条。\n\n"
            f"嫌吵：`/newtoken hot 严`（约 1 条/小时）\n"
            f"嫌少：`/newtoken hot 宽`（约 6 条/小时）\n"
            f"关掉：`/newtoken off`\n\n"
            f"⚠️ 我**不判断有没有潜力**——那不是数据能给的。"
            f"这里给的是「有人在真交易、有人在抄这个梗、且不是蜜罐」。",
            parse_mode="Markdown")
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
# ── 松紧三档（2026-08-27 他反馈「链上告警多了」之后加的）──────
# 实测一小时 120 个新池、26 个中文名，各档去重后剩多少：
#     宽    3千/20笔          → 6 条/小时   ← 原来的默认，他嫌吵
#     标准  8千/40笔+同名≥2   → 3 条/小时   ← 新默认
#     严    1.5万/40笔+同名≥2 → 1 条/小时
#
# **第三个参数（同名≥N）是这里最有价值的闸**：抄袭要花钱建池子，
# 有人愿意抄这个名字，说明梗真的在被讨论——比流动性更接近"有热度"。
# 而且它和流动性正交：一个 5 千流动性但被抄了 4 次的梗，
# 比一个 3 万流动性、没人理的币更值得看一眼。
HOT_LEVELS = {
    "宽": (3_000, 20, 1),
    "标准": (8_000, 40, 2),
    "严": (15_000, 40, 2),
}
HOT_DEFAULT_LEVEL = "标准"
HOT_PER_HOUR = 4        # 每小时最多推几条。**兜底闸**：判据再准也不能刷屏
HOT_MIN_LIQ, HOT_MIN_TXNS, HOT_MIN_SAME = HOT_LEVELS[HOT_DEFAULT_LEVEL]


def hot_level():
    """当前松紧档。返回 (档名, 流动性, 笔数, 同名下限)。"""
    name = _cfg().get("hot_level") or HOT_DEFAULT_LEVEL
    if name not in HOT_LEVELS:
        name = HOT_DEFAULT_LEVEL
    return (name,) + HOT_LEVELS[name]


def set_hot_level(name):
    if name not in HOT_LEVELS:
        return None
    _cfg()["hot_level"] = name
    save_data()
    return hot_level()


def _roll_hour():
    """跨小时就把计数清零。**记数和查配额都要先走这一步**——
    只在查配额时清零的话，先记数再查会把刚记的那笔一起抹掉
    （测试当场抓到的：_hot_used 之后 quota 又变回满格）。"""
    c = _cfg()
    bucket = int(time.time() // 3600)
    if c.get("hot_hour") != bucket:
        c["hot_hour"] = bucket
        c["hot_sent"] = 0
    return c


def hot_quota_left():
    """这一小时还能推几条。**兜底闸**：阈值调错、或某个梗突然被抄十几次时，
    不至于把群刷爆。"""
    c = _roll_hour()
    return max(0, HOT_PER_HOUR - int(c.get("hot_sent") or 0))


def _hot_used(n=1):
    c = _roll_hour()
    c["hot_sent"] = int(c.get("hot_sent") or 0) + n


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


# ── 梗爆发：不是"哪个池子够大"，是"哪个梗被抄得最快" ──────────
#
# 他的问题：「我怎么去获取链上热门热度第一时间发币告警」。
# 先量了"第一时间"到底卡在哪（tools\\probe_firstseen.py，并发轮询 4 条源
# 8 分钟，记每个代币被谁先看见）：
#
#     pump.fun 内盘         早 40 秒（76/77 次）  但只有 Solana
#     GeckoTerminal 新池     基准，建池后约 2 分钟   ← 现在用的
#     DexScreener 档案       晚 180 秒
#
# **延迟不是瓶颈**。而且那 20 个中文名代币里 pump.fun 只捞到 1 个，其余全在
# BSC；BSC 的内盘平台 four.meme 接口全 404，免费打不通。所以内盘这条路
# 对他要的那类币现在是死的。
#
# 真正的瓶颈是"哪个值得推"。同一批数据里：
#
#     甜甜币   8 分钟被抄 7 次   ← 此刻正在热
#     牛来     1 次              ← 热度是几天前的
#     我的女友景甜  1 次
#
# 抄要花钱建池子。短时间内有 7 拨人愿意花这个钱 = 这个梗正在被讨论。
# 所以「热度第一时间」= **同名新池的出现速度**，不是某个池子的绝对大小。
# 这跟 hot 模式那道「同名≥N」的闸是同一个思路，但那个是单次快照里的静态计数，
# 抓不到跨轮次的爆发。
#
# ## 阈值（tools\\probe_memeburst.py，净观察 14.6 分钟 / 259 个新池）
#
#     同名≥3 次   全部 41.1 个/小时    中文名 4.1 个/小时
#     同名≥5 次   全部 24.6 个/小时    中文名 4.1 个/小时
#     同名≥7 次   全部  4.1 个/小时    中文名 0.0 个/小时
#
# 英文名那一列全是噪音（"Solana"/"solana" 各 5 次这种），所以：
# **中文名按 level 判，英文名要 level×3** —— 极端爆发（那一轮 MIRA 被抄
# 23 次）仍然进得来，日常英文噪音进不来。
#
# ## 两个必须要有的护栏
#
# ① **时间跨度下限**：实测有 `joe 3 次 / 0.0 分钟` —— 一个人同一秒批量建三个池，
#    那不是热度是垃圾。要求抄袭散布在 90 秒以上。
# ② **垃圾名黑名单**：链名和主流币名天天被拿来当代币名，一抓一大把。
BURST_WINDOW = 1800        # 滑动窗口 30 分钟
BURST_LEVELS = {"宽": 3, "标准": 5, "严": 8}
BURST_DEFAULT_LEVEL = "标准"
BURST_EN_FACTOR = 3        # 英文名要几倍的量才算数（见上面那段实测）
BURST_MIN_SPAN = 90        # 抄袭必须散布这么多秒，杜绝同一秒批量建池
BURST_PER_HOUR = 2         # 兜底闸。和 hot 分开计数，免得互相饿死
BURST_COOLDOWN = 6 * 3600  # 同一个梗多久内不重复报

# 链名、主流币名天天被拿来当代币名。这些不是梗，是噪音。
BURST_JUNK = {
    "solana", "sol", "bnb", "eth", "ethereum", "base", "btc", "bitcoin",
    "usdt", "usdc", "wbnb", "weth", "wsol", "busd", "dai", "test", "token",
    "coin", "meme", "pump", "doge", "trump",
}

# name -> {pool_addr: (首见时间, 池子)}。**只放内存不落盘**：
# 这是个 30 分钟的滑动窗口，写进 data.json 就是每 5 分钟糊几百条进去。
# 重启后 30 分钟内自愈，期间只会漏报不会误报（计数偏低）。
_burst = {}


def burst_level():
    """当前松紧档。返回 (档名, 同名次数下限)。"""
    name = _cfg().get("burst_level") or BURST_DEFAULT_LEVEL
    if name not in BURST_LEVELS:
        name = BURST_DEFAULT_LEVEL
    return name, BURST_LEVELS[name]


def set_burst_level(name):
    if name not in BURST_LEVELS:
        return None
    _cfg()["burst_level"] = name
    save_data()
    return burst_level()


def burst_enabled(chat_id):
    return str(chat_id) in [str(x) for x in (_cfg().get("burst") or [])]


def toggle_burst(chat_id, on):
    c = _cfg()
    lst = [str(x) for x in (c.get("burst") or [])]
    key = str(chat_id)
    if on and key not in lst:
        lst.append(key)
    if not on:
        lst = [x for x in lst if x != key]
    c["burst"] = lst
    save_data()
    return on


def burst_quota_left():
    """和 hot 分开算。两类告警共用一个配额的话，池子多的那类会把另一类饿死。"""
    c = _cfg()
    bucket = int(time.time() // 3600)
    if c.get("burst_hour") != bucket:
        c["burst_hour"] = bucket
        c["burst_sent"] = 0
    return max(0, BURST_PER_HOUR - int(c.get("burst_sent") or 0))


def _burst_used(n=1):
    burst_quota_left()          # 先滚小时，否则记完的数下一次查询就被抹掉
    c = _cfg()
    c["burst_sent"] = int(c.get("burst_sent") or 0) + n


def is_junk_name(name):
    """这个名字算不算噪音。

    ⚠️ 「太短就是垃圾」这条**不能对中文一刀切**。第一版写的是 `len < 2`，
    测试当场撞出来：单个汉字（「猫」「狗」）是完全正常的梗币名，
    而单个字母（x、a）归组会把一堆无关的币并到一起。
    信息密度差太多，中英文得分开判——和 [中文名被吞] 那次是同一类错。
    """
    n = (name or "").strip()
    if not n:
        return True
    if n.lower() in BURST_JUNK:
        return True
    if not CJK.search(n) and len(n) < 3:
        return True                      # 纯 ASCII 的短名，归组没意义
    return not _re.search(r"[0-9A-Za-z一-鿿]", n)


def _created_at(p, now=None):
    """池子的**链上创建时间**，取不到就退回"我第一次看见它"。

    ⚠️ 这个退化路径是有代价的，真机第一跑就撞上了：刚重启时窗口里的存量池子
    全被打上同一个时间戳，跨度≈0，于是被下面那道"同一秒批量建池"的护栏
    整批毙掉——自检里「甜甜币被抄 16 次」却一条都没到线，就是这么来的。
    GeckoTerminal 每个池子都带 `pool_created_at`，用它才是真的跨度。
    """
    from datetime import datetime
    try:
        return datetime.fromisoformat(
            str(p.get("pool_created_at")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError):
        return now or time.time()


def burst_track(pools, now=None):
    """把这一轮看到的池子记进滑动窗口。返回这轮新记了几个（给自检用）。"""
    now = now or time.time()
    added = 0
    for p in pools:
        nm = base_name(p)
        addr = p.get("pool_address") or ""
        if not nm or not addr or is_junk_name(nm):
            continue
        slot = _burst.setdefault(nm, {})
        if addr not in slot:
            slot[addr] = (_created_at(p, now), p)
            added += 1
    # 过期清理。不清的话字典会一直涨，而且 30 分钟前的抄袭不该算进"正在热"
    for nm in list(_burst):
        keep = {a: v for a, v in _burst[nm].items() if now - v[0] <= BURST_WINDOW}
        if keep:
            _burst[nm] = keep
        else:
            _burst.pop(nm, None)
    return added


def burst_hits(now=None, level=None):
    """窗口里够格的梗。→ [(名字, 次数, 跨度秒, [池子…])]，按次数降序。

    中文名按 level 判，英文名要 level×BURST_EN_FACTOR —— 理由见上面那段实测。
    """
    now = now or time.time()
    need = level if level is not None else burst_level()[1]
    out = []
    for nm, slot in _burst.items():
        ts = [v[0] for v in slot.values()]
        if not ts:
            continue
        n = len(ts)
        want = need if CJK.search(nm) else need * BURST_EN_FACTOR
        if n < want:
            continue
        span = max(ts) - min(ts)
        if span < BURST_MIN_SPAN:
            # 同一秒批量建池 = 一个人在刷，不是热度。实测抓到过 joe 3 次 / 0 秒
            continue
        pools = [v[1] for v in sorted(slot.values(), key=lambda x: -_liq_of(x[1]))]
        out.append((nm, n, span, pools))
    return sorted(out, key=lambda x: -x[1])


def _liq_of(p):
    try:
        return float(p.get("reserve_in_usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def burst_cooled(name, now=None):
    """这个梗最近报过没有。**这份要落盘**：重启后不该把半小时前报过的再报一遍。"""
    now = now or time.time()
    rec = _cfg().setdefault("burst_sent_at", {})
    return now - float(rec.get(name) or 0) < BURST_COOLDOWN


def burst_mark(name, now=None):
    rec = _cfg().setdefault("burst_sent_at", {})
    rec[name] = now or time.time()
    for k in [k for k, v in rec.items()
              if (now or time.time()) - float(v) > BURST_COOLDOWN * 2]:
        rec.pop(k, None)


def format_burst(name, n, span, pools, chain_cn, warn, dropped=0):
    """推的是**这个梗**，不是某一个池子。附上流动性最大的那个当入口。"""
    top = pools[0]
    liq = _liq_of(top)
    addr = base_token_address(top)
    rate = n / max(span / 60, 1)
    lines = [
        f"🀄 *梗爆发* · {chain_cn}",
        f"*{name}*", "",
        f"⚡ **{int(span // 60)} 分钟内被抄了 {n} 次**（约 {rate:.1f} 个/分钟）",
        "　抄要花钱建池子。短时间内这么多人愿意花这个钱，说明这个梗正在被讨论——",
        "　这比「某个池子够大」更接近「有热度」，也更早。",
        "",
        f"下面是这 {n} 个里流动性最大的那个：",
        f"　流动性 ${liq:,.0f}　1h 成交 {_txns_1h(top)} 笔",
    ]
    if len(pools) > 1:
        others = [f"${_liq_of(p):,.0f}" for p in pools[1:4]]
        lines.append(f"　其余的：{'、'.join(others)}"
                     + ("…" if len(pools) > 4 else ""))
    if warn:
        lines.append(f"⚠️ {warn}")
    if addr:
        lines += ["", f"合约 `{addr}`", f"查它：`/oc {addr}`"]
    if dropped:
        # 配额挡掉的必须报数。不报的话"这一小时只有一个梗"和"还有三个没推给你"
        # 在屏幕上长得一模一样
        lines.append(f"\n（这一小时还有 {dropped} 个梗到线了，配额满了没推。"
                     f"想全收发 `/newtoken burst 宽`）")
    lines += ["",
              "⚠️ **同名越多越要认准合约地址**——大部分是跟风盘和仿盘。"
              "这里只筛掉了蜜罐和假盘，**没有任何「会不会涨」的判断**。"]
    return "\n".join(lines)


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
