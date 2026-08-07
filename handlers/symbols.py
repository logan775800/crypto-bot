"""合约身份解析 —— 回答「你说的这个币，到底是哪个所的哪个合约」。

为什么这是 P0：同一个代号在不同交易所可能是完全不同的项目，而一份写着精确
价位的分析，如果底下解析成了另一个合约，比不分析更危险。典型坑：

  • 同代号不同项目：LAB / AKE / DEXE 这类新币，两个所可能各上各的；
  • 面值倍数：1000PEPE / 1000BONK / SHIB1000 的报价是「1000 枚」的价格，
    跟现货 PEPE 差 1000 倍——拿现货价当合约价算止损，距离直接错三个数量级；
  • 改名/迁移币：LUNA→LUNC 之后 LUNA 是新链，同名不同物；
  • 计价单位：Bybit 线性永续按**币**计量，OKX 永续按**张**计量(ctVal=每张面值)，
    「开 100」在两个所差着一个面值倍数。

所以本模块只做一件事：把用户随口说的代号，解析成带交易所、合约类型、结算币、
面值倍数、最小下单量、最大杠杆的**明确身份**；解析不唯一时**返回候选让人选**，
绝不替用户假定是哪一个。

同名不同项目没有权威数据库可查，这里用一个可验证的信号代替猜测：
**两个所同名合约的价格偏离超过阈值 → 极可能不是同一个项目**，据此报警。
"""
import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

BYBIT = "https://api.bybit.com"
OKX = "https://www.okx.com"

CACHE_TTL = 6 * 3600        # 合约清单缓存：新币上市不频繁，6h 够用
# 两所同名合约价格偏离超过这个比例，就当成「同名不同币」报警。
# 正常跨所价差是千分之几，5% 已经远超套利能容忍的范围。
PRICE_DIVERGENCE = 5.0

# 合约类型：交割合约(带日期)和永续是两个东西，混起来会拿错标的
_KIND = {"LinearPerpetual": "线性永续", "LinearFutures": "线性交割",
         "InversePerpetual": "反向永续", "InverseFutures": "反向交割"}

_cache = {"ts": 0, "bybit": {}, "okx": {}}
_lock = asyncio.Lock()
_client = None


async def _http():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=12)
    return _client


def _split_multiplier(base):
    """1000PEPE / SHIB1000 → ("PEPE", 1000)。没有倍数前后缀则 (base, 1)。

    交易所两种写法都有，所以两头都试——写死某一种，另一种就会被当成不同的币。
    """
    b = (base or "").upper()
    for mul in ("10000", "1000", "100"):
        if b.startswith(mul) and len(b) > len(mul):
            return b[len(mul):], int(mul)
        if b.endswith(mul) and len(b) > len(mul):
            return b[:-len(mul)], int(mul)
    return b, 1


class Inst:
    """一个可交易合约的明确身份。字段全部来自交易所接口，没有一个是推断的。"""

    __slots__ = ("exchange", "symbol", "base", "underlying", "quote", "settle",
                 "kind", "multiplier", "min_qty", "qty_step", "tick", "max_lev",
                 "qty_unit", "status")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def key(self):
        return f"{self.exchange}:{self.symbol}"

    def identity(self):
        """固定挂在分析开头的身份块——用户一眼能核对是不是他要的那个合约。"""
        lines = [
            f"交易所：{self.exchange}",
            f"交易对：{self.symbol}（{self.kind}）",
            f"标的：{self.underlying}" + (
                f"　⚠️ 合约面值 = {self.multiplier} {self.underlying}"
                f"（报价是{self.multiplier}枚的价格，别拿现货价直接套）"
                if self.multiplier > 1 else ""),
            f"结算币：{self.settle}｜计量单位：{self.qty_unit}",
        ]
        if self.min_qty:
            lines.append(f"最小下单 {_g(self.min_qty)}｜步长 {_g(self.qty_step)}"
                         + (f"｜最大杠杆 {_g(self.max_lev)}x" if self.max_lev else ""))
        return "\n".join(lines)

    def one_line(self):
        m = f"（×{self.multiplier}）" if self.multiplier > 1 else ""
        return f"{self.exchange} {self.symbol}{m} 标的{self.underlying} 结算{self.settle}"


def _g(x):
    """去掉 1.0 这种没意义的小数尾巴。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{v:g}"


# ── 拉两个所的合约清单 ────────────────────────────────────────────
async def _load_bybit():
    c = await _http()
    r = await c.get(f"{BYBIT}/v5/market/instruments-info",
                    params={"category": "linear", "limit": 1000})
    r.raise_for_status()
    d = r.json()
    if d.get("retCode") != 0:
        raise RuntimeError(f"Bybit: {d.get('retMsg')}")
    out = {}
    for x in d.get("result", {}).get("list", []):
        if x.get("status") != "Trading":
            continue
        base = (x.get("baseCoin") or "").upper()
        under, mul = _split_multiplier(base)
        lot = x.get("lotSizeFilter") or {}
        lev = x.get("leverageFilter") or {}
        inst = Inst(
            exchange="Bybit", symbol=x.get("symbol"), base=base, underlying=under,
            quote=(x.get("quoteCoin") or "").upper(),
            settle=(x.get("settleCoin") or x.get("quoteCoin") or "").upper(),
            kind=_KIND.get(x.get("contractType"), x.get("contractType") or "?"),
            multiplier=mul,
            min_qty=_num(lot.get("minOrderQty")), qty_step=_num(lot.get("qtyStep")),
            tick=_num((x.get("priceFilter") or {}).get("tickSize")),
            max_lev=_num(lev.get("maxLeverage")),
            qty_unit="币",              # Bybit 线性永续按标的币计量
            status="Trading",
        )
        out.setdefault(under, []).append(inst)
    return out


async def _load_okx():
    c = await _http()
    r = await c.get(f"{OKX}/api/v5/public/instruments", params={"instType": "SWAP"})
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0":
        raise RuntimeError(f"OKX: {d.get('msg')}")
    out = {}
    for x in d.get("data") or []:
        if x.get("state") != "live":
            continue
        iid = x.get("instId") or ""
        parts = iid.split("-")
        if len(parts) < 3:
            continue
        base = parts[0].upper()
        under, mul = _split_multiplier(base)
        # OKX 永续按「张」计量，ctVal 是每张面值——和 Bybit 的「币」不是一个单位
        ctval = _num(x.get("ctVal")) or 1.0
        inst = Inst(
            exchange="OKX", symbol=iid, base=base, underlying=under,
            quote=parts[1].upper(), settle=(x.get("settleCcy") or parts[1]).upper(),
            kind="线性永续" if x.get("ctType") == "linear" else "反向永续",
            multiplier=mul,
            min_qty=_num(x.get("minSz")), qty_step=_num(x.get("lotSz")),
            tick=_num(x.get("tickSz")), max_lev=_num(x.get("lever")),
            qty_unit=f"张（1张={_g(ctval)} {x.get('ctValCcy') or under}）",
            status="live",
        )
        out.setdefault(under, []).append(inst)
    return out


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


async def load(force=False):
    """两个所的合约清单，按标的币归档。取数失败的所留空，不整体失败——
    一个所挂了不该让另一个所的解析也用不了。"""
    now = time.time()
    if not force and _cache["ts"] and now - _cache["ts"] < CACHE_TTL:
        return _cache
    async with _lock:
        if not force and _cache["ts"] and time.time() - _cache["ts"] < CACHE_TTL:
            return _cache
        by, ok = await asyncio.gather(_load_bybit(), _load_okx(), return_exceptions=True)
        if isinstance(by, Exception):
            log.warning(f"加载 Bybit 合约清单失败: {by}")
            by = _cache["bybit"]          # 保留上一版，总比空好
        if isinstance(ok, Exception):
            log.warning(f"加载 OKX 合约清单失败: {ok}")
            ok = _cache["okx"]
        _cache.update({"ts": time.time(), "bybit": by, "okx": ok})
    return _cache


def _clean(q):
    """用户输入 → 可能的标的币。吃掉 USDT/PERP/永续/分隔符这些噪音。"""
    s = (q or "").upper().strip()
    for junk in ("-SWAP", "SWAP", "永续", "PERP", "/", "-", "_", " "):
        s = s.replace(junk, "")
    if s.endswith("USDT") and len(s) > 4:
        s = s[:-4]
    return s


async def resolve(query):
    """把用户说的代号解析成候选合约列表。

    返回 (候选[Inst], 标的币)。空列表 = 两个所都没有这个永续。
    **不做唯一性假定**：多个候选时由调用方展示给用户/模型去选。
    """
    cache = await load()
    raw = _clean(query)
    under, _mul = _split_multiplier(raw)
    hits = []
    for src in ("bybit", "okx"):
        hits += cache[src].get(under, [])
    hits.sort(key=lambda i: _rank(i, raw))
    return hits, under


def _rank(i, raw):
    """候选排序。第一名会被 sizing 等处直接采用，所以顺序本身是有语义的：

    实盘走的是 **USDT 结算的永续**，所以 USDC 永续(BTCPERP)、交割合约
    (BTCUSDT-14AUG26) 必须排在后面——否则「查 BTC 的合约规格」会拿到 USDC 那条，
    结算币都不对，算出来的保证金和爆仓价全是另一个合约的。
    """
    return (
        i.base != raw,                       # 用户写全了(1000PEPE)就优先精确匹配
        (i.settle or "") != "USDT",          # USDT 结算优先
        "永续" not in (i.kind or ""),         # 永续优先于交割
        i.exchange != "Bybit",               # 实盘在 Bybit
        i.symbol,
    )


def preferred(insts):
    """候选里最该用于实盘的那个（USDT 永续）。没有就返回 None，让调用方明说。"""
    for i in insts or []:
        if (i.settle or "") == "USDT" and "永续" in (i.kind or ""):
            return i
    return None


async def _last_price(inst):
    """取该合约最新价——用于跨所同名比对。失败返回 None，不抛。"""
    c = await _http()
    try:
        if inst.exchange == "Bybit":
            r = await c.get(f"{BYBIT}/v5/market/tickers",
                            params={"category": "linear", "symbol": inst.symbol})
            lst = (r.json().get("result") or {}).get("list") or []
            return _num(lst[0].get("lastPrice")) if lst else None
        r = await c.get(f"{OKX}/api/v5/market/ticker", params={"instId": inst.symbol})
        d = r.json().get("data") or []
        return _num(d[0].get("last")) if d else None
    except Exception as e:
        log.debug(f"取 {inst.key} 最新价失败: {e}")
        return None


async def divergence_check(insts):
    """跨所同名合约的价格偏离检查。

    同名不同项目没有权威库可查，但有个可验证的信号：真·同一个币在两个所之间
    只会有千分之几的价差(否则早被套利抹平)。偏离到 5% 以上，几乎只有一种解释——
    **它们根本不是同一个项目**。返回 (警告文本|None, {key: 价格})。
    """
    # 面值倍数不同的要先归一化到「1 枚标的」再比，否则 1000PEPE vs PEPE 必然报警
    norm = {}
    prices = {}
    todo = [i for i in insts if i.multiplier is not None]
    if len(todo) < 2:
        return None, prices
    got = await asyncio.gather(*[_last_price(i) for i in todo], return_exceptions=True)
    for inst, px in zip(todo, got):
        if isinstance(px, Exception) or not px:
            continue
        prices[inst.key] = px
        norm[inst.key] = px / (inst.multiplier or 1)
    if len(norm) < 2:
        return None, prices
    lo, hi = min(norm.values()), max(norm.values())
    if lo <= 0:
        return None, prices
    dev = (hi - lo) / lo * 100
    if dev < PRICE_DIVERGENCE:
        return None, prices
    detail = "、".join(f"{k} {_g(v)}" for k, v in sorted(norm.items(), key=lambda x: x[1]))
    return (f"🚨 *同名不同币警告*：这几个所的同名合约单价折算后相差 {dev:.0f}%"
            f"（{detail}）。同一个项目跨所不可能差这么多——**它们极可能是不同的项目**。"
            f"请先确认你要的是哪一个，再谈价位。"), prices


async def describe(query):
    """给人看的解析结果：唯一就报身份，多个就列候选，没有就说清没有。"""
    insts, under = await resolve(query)
    if not insts:
        return (f"❌ 两个所都没有 *{under}* 的 USDT 永续。\n"
                f"可能是代号写错、只在别的所上线、或已下架。\n"
                f"（本清单每 6 小时刷新一次，刚上市的新币可能还没进来）")
    warn, prices = await divergence_check(insts)
    head = f"🔎 *{under} 合约身份*　找到 {len(insts)} 个"
    if len(insts) == 1:
        body = insts[0].identity()
        px = prices.get(insts[0].key)
        if px:
            body += f"\n最新价 {_g(px)}"
        return f"{head}\n━━━━━━━━━━━━━━\n{body}"
    lines = [head, "━━━━━━━━━━━━━━",
             "⚠️ 不止一个，**必须先确认是哪个**再谈价位："]
    for i in insts:
        px = prices.get(i.key)
        lines.append(f"• {i.one_line()}" + (f"　最新 {_g(px)}" if px else ""))
    if warn:
        lines += ["", warn]
    return "\n".join(lines)


def for_ai(insts, under, warn=None, prices=None):
    """喂给模型的身份约束。

    多候选时默认是**硬要求**：先问清楚，不许自己挑一个。唯一的例外是价格已经
    核对过、确认是同一个项目——那就收敛到 USDT 永续，但仍必须点明面值与计量单位
    的差异（同一个项目在两个所"开100"完全不是一回事）。
    """
    if not insts:
        return (f"【合约身份】Bybit/OKX 都没有 {under} 的 USDT 永续合约。"
                f"不要给出该币的任何价位分析，请告诉用户代号可能写错或该币无永续。")
    # 同一个所里的多个合约版本（USDT永续 / USDC永续 / 交割）**不是**同名不同币，
    # 是同一个币的不同合约——收敛到 USDT 永续并把其余列为备注即可。
    # 但跨所同名必须保留为歧义：那才可能是两个不同项目。
    note = ""
    pref = preferred(insts)
    # 收敛条件：同一个所的多版本，或跨所但价格已核对一致(确认同一项目)
    same_ex = len({i.exchange for i in insts}) == 1
    price_confirmed = warn is None and len(prices or {}) >= 2
    if len(insts) > 1 and pref and (same_ex or price_confirmed):
        others = [i.one_line() for i in insts if i is not pref]
        insts = [pref]
        where = "在该所" if same_ex else "（跨所价格已核对一致，是同一个项目）"
        note = (f"\n注意：{pref.underlying} {where}还有其他合约版本"
                f"（{'；'.join(others[:4])}）。默认按上面这个 USDT 永续分析。"
                f"这些版本的**面值倍数与计量单位不同**，同样一句「开100」在不同版本上"
                f"差着数量级——换版本必须重新取数、重算仓位。")
    if len(insts) == 1:
        i = insts[0]
        extra = note
        if i.multiplier > 1:
            extra += (f" ⚠️ 该合约面值 = {i.multiplier} {i.underlying}，报价是 {i.multiplier} 枚的价格，"
                      f"**绝不能**拿现货单价直接套用止损距离或仓位。")
        return (f"【合约身份·已确认】{i.exchange} {i.symbol}｜{i.kind}｜标的 {i.underlying}｜"
                f"结算 {i.settle}｜计量 {i.qty_unit}｜最小下单 {_g(i.min_qty)}｜"
                f"步长 {_g(i.qty_step)}｜最大杠杆 {_g(i.max_lev)}x。{extra}\n"
                f"回答开头必须写明交易所与交易对，让用户能核对。")
    lines = [f"【合约身份·不唯一·必须先澄清】{under} 匹配到 {len(insts)} 个合约："]
    lines += [f"- {i.one_line()}" for i in insts]
    lines.append("你**不得**自行假定用户指的是哪一个。请列出候选、问用户选哪个，"
                 "在用户确认之前不要给出任何价位、进场、止损。")
    if warn:
        lines.append(warn.replace("*", ""))
    return "\n".join(lines)


# ── /sym 命令 ─────────────────────────────────────────────────────
async def sym_cmd(update, context):
    """/sym LAB —— 这个代号在各所到底对应哪个合约。"""
    from handlers.util import safe_reply
    args = context.args or []
    if not args:
        await safe_reply(update.message,
            "🔎 *合约身份查询*\n\n`/sym LAB` —— 查这个代号在 Bybit/OKX 各是哪个合约\n\n"
            "会给出：交易所、交易对、标的、结算币、计量单位（币/张）、面值倍数、"
            "最小下单量、最大杠杆。\n"
            "同一代号在多个所存在时会全部列出，并对**同名不同币**做价格偏离检测。",
            parse_mode="Markdown")
        return
    await safe_reply(update.message, f"🔎 解析 {args[0].upper()} …")
    try:
        txt = await describe(args[0])
    except Exception as e:
        log.error(f"/sym 解析失败: {e}")
        txt = f"解析失败：{str(e)[:80]}"
    await safe_reply(update.message, txt, parse_mode="Markdown")
