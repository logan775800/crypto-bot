"""Gate.io 数据源，镜像币安专区的全部七个功能（新币/涨幅/合约涨幅/费率/多空比/爆仓/合约行情）。

为什么值得单独接一个 Gate：小币和新币它上得最早最全——919 个 USDT 永续里
有一大批是币安 Bybit 都还没有的。所以「币安查不到」不等于「没这个合约」。

接口全部是 V4 公开端点（无需 key），字段名是 2026-08-14 实探过的，不是照记忆写的：
  现货行情  /spot/tickers                 currency_pair last change_percentage quote_volume
  合约行情  /futures/usdt/tickers         contract last change_percentage funding_rate
                                          mark_price index_price total_size quanto_multiplier
  合约详情  /futures/usdt/contracts[/名]  funding_interval funding_next_apply launch_time
                                          contract_type in_delisting is_pre_market
  合约统计  /futures/usdt/contract_stats  lsr_account lsr_taker top_lsr_account
                                          long_liq_usd short_liq_usd open_interest_usd

两处 Gate 比币安强的地方，顺手就吃到了：
  • 爆仓：币安关掉了公开爆仓接口（那个按钮只能提示去 OKX 看），Gate 的
    contract_stats 每根 K 线都带多空爆仓金额，能真出 24h 爆仓分布；
  • 持仓量/大户多空比：一次调用全有。
"""
import asyncio
import datetime
import logging
import re
import time

import httpx

from handlers.quickprice import fmt_price
from handlers.util import escape_md

log = logging.getLogger(__name__)

BASE = "https://api.gateio.ws/api/v4"

# Gate 用 contract_type 区分品类：空串才是加密货币，其余是代币化股票/指数等。
# 919 个合约里 346 个是股票（多为韩股，如 NAVER/KODEX200），一天能上几十个。
# 不过滤的话涨幅榜和新币榜会被这些淹没——做合约的人要的不是韩股。
KIND_CN = {"stocks": "股票", "indices": "指数", "metals": "贵金属",
           "commodities": "大宗商品", "forex": "外汇"}

MIN_QUOTE_VOL = 100_000     # 与币安专区同一条成交额门槛，滤掉没人交易的僵尸盘
TOP_N = 15
NEW_N = 10

# 杠杆代币（3L/3S/5L/5S = 3倍/5倍多空 ETF）：现货 2085 个 USDT 交易对里有 394 个是这个。
# 它们天天霸榜（一个 +165% 的 SNXX3L 只是标的涨了 55%），当成"币"看会亏死——
# 有磨损、有再平衡、长期必然归零。币安专区排的是 UP/DOWN/BULL/BEAR，Gate 换了命名而已。
_LEV_RE = re.compile(r"\d+[LS]$")
_LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")


def _is_leveraged(base):
    """WELL3 / API3 / PEPE2 这类以数字结尾的真币不能误伤：必须是「数字+L/S」结尾。"""
    return bool(_LEV_RE.search(base)) or any(base.endswith(x) for x in _LEV_SUFFIX)


# 现货这边的股票代币比合约还难认：接口没有品类字段，名字也五花八门。
# 实测（2026-08-14，2085 个 USDT 交易对）要三层才干净：
#   1. 名字里写明是 ETF/杠杆份额的 —— QQQG「NASDAQ 100 Index ETF」、
#      SOXLG「Direxion Daily Semiconductor Bull 3X ETF」，18 个，零误伤；
#   2. 和合约同名的 —— AAPLX / TSLAX / PAXG，靠 contract_type 反查；
#   3. 「尾 G」股票镜像 —— SNDKG=SanDisk、MSFTG=Microsoft，62 个。
# 第 3 层单独用会误伤 MOG（Mog Coin，因为 MO 是奥驰亚的股票代码），
# 所以名字里带 Coin/Token 这类加密词的一律放行。
# 只认 Tokenized 这个显式标记，不能顺手把 Ondo 也匹配上——
# SKHYON 是「SK Hynix Ondo Tokenized」（股票），USDY 是「Ondo US Dollar Yield」（真币）。
_ETFISH = re.compile(r"\b(\d+X|ETF|Bull|Bear|Shares|Index|Tokenized)\b", re.I)
_CRYPTOISH = re.compile(
    r"\b(coin|token|protocol|network|finance|chain|dao|inu|meme|swap|labs?)\b", re.I)


def _noncrypto_spot_bases(contract_meta, pairs):
    """现货里所有「不是加密货币」的 base 集合。pairs 拿不到时退化成只用合约品类。"""
    fut = {_base(name) for name, c in contract_meta.items() if not _kind(c)[0]}
    out = set(fut)
    for p in pairs or []:
        base, name = p.get("base") or "", p.get("base_name") or ""
        if not base:
            continue
        if _ETFISH.search(name):
            out.add(base)
        elif (base.endswith("G") and base[:-1] in fut
                and not _CRYPTOISH.search(name)):
            out.add(base)
    return out

# 合约元数据（品类/上线时间/结算周期）1.2MB、3 秒，但新币一天才上几个，
# 每次点按钮都拉一遍纯属浪费。缓存 1 小时，新币榜最多迟一小时。
_CONTRACTS_TTL = 3600
_contracts_cache = {"at": 0.0, "data": None}
_pairs_cache = {"at": 0.0, "data": None}


async def _noop(value):
    """gather 里凑位用：合约榜不需要现货交易对，但别为此拆成两条分支。"""
    return value


async def _get(path, params=None):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()


def _pair(symbol):
    """BTC → BTC_USDT（Gate 用下划线分隔，和币安的 BTCUSDT 不一样）。

    调用方五花八门：按钮给的是裸币名 BTC，实盘那边习惯 BTCUSDT，AI 有时给
    BTC/USDT。全部归一，别让「BTCUSDT」拼成「BTCUSDTUSDT」查不到。
    """
    s = symbol.upper().strip().replace("/", "_").replace("-", "_").split("_")[0]
    if len(s) > 4 and s.endswith("USDT"):
        s = s[:-4]
    return s + "_USDT"


def _base(pair):
    return pair.rsplit("_", 1)[0]


def _f(d, key, default=0.0):
    """Gate 的数字几乎都是字符串，且可能是 null。"""
    try:
        v = d.get(key)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def contracts(force=False):
    """全部 USDT 永续的元数据，{合约名: 详情}。带 1 小时缓存。"""
    now = time.monotonic()
    if (not force and _contracts_cache["data"] is not None
            and now - _contracts_cache["at"] < _CONTRACTS_TTL):
        return _contracts_cache["data"]
    data = await _get("/futures/usdt/contracts")
    mapped = {c["name"]: c for c in data if c.get("name")}
    _contracts_cache.update(at=now, data=mapped)
    return mapped


async def spot_pairs(force=False):
    """全部现货交易对（要的是 base_name，用来认股票代币）。同样缓存 1 小时。"""
    now = time.monotonic()
    if (not force and _pairs_cache["data"] is not None
            and now - _pairs_cache["at"] < _CONTRACTS_TTL):
        return _pairs_cache["data"]
    data = await _get("/spot/currency_pairs")
    _pairs_cache.update(at=now, data=data)
    return data


def _kind(meta):
    """返回 (是否加密货币, 中文品类标注)。"""
    ct = (meta or {}).get("contract_type") or ""
    if not ct:
        return True, ""
    return False, KIND_CN.get(ct, ct)


def _err_text(exc, symbol, what):
    """Gate 对不存在的合约直接回 400。这是「没这个币」，不是「网络不通」——
    报错报错方向，用户会去折腾网络而不是换个币名。"""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 404):
        return f"Gate 未找到 {symbol} {what}"
    log.error(f"Gate {what}出错: {exc}")
    return "Gate 查询失败(可能网络不通)"


def _interval_note(seconds):
    """结算周期提示。1h 结算是最容易亏在细节上的地方——一天扣 24 次。"""
    try:
        h = int(seconds) // 3600
    except (TypeError, ValueError):
        return "", ""
    if h <= 0:
        return "", ""
    if h == 1:
        return f"{h}h", "（1 小时结算一次，是常规 8h 的 8 倍消耗，只适合快进快出）"
    if h < 8:
        return f"{h}h", f"（{24 // h} 次/天，比常规 8h 扣得快）"
    return f"{h}h", ""


# ---------- 价格（与币安同名函数一一对应，供回退/复用） ----------
async def get_price_gate(symbol):
    """现货 24h 行情，返回 {price, change, source}；查不到返回 None。"""
    try:
        d = await _get("/spot/tickers", {"currency_pair": _pair(symbol)})
        if d:
            last = _f(d[0], "last")
            if last > 0:
                return {"price": last, "change": _f(d[0], "change_percentage"),
                        "source": "Gate"}
    except Exception:
        pass
    return None


async def get_swap_ticker_gate(symbol):
    """永续合约 24h 行情，返回 {price, change}；查不到返回 None。"""
    try:
        d = await _get("/futures/usdt/tickers", {"contract": _pair(symbol)})
        if d:
            last = _f(d[0], "last")
            if last > 0:
                return {"price": last, "change": _f(d[0], "change_percentage")}
    except Exception:
        pass
    return None


async def get_funding_gate(symbol):
    """资金费率(百分比)，查不到返回 None。"""
    try:
        d = await _get("/futures/usdt/tickers", {"contract": _pair(symbol)})
        return _f(d[0], "funding_rate") * 100 if d else None
    except Exception:
        return None


# ---------- 供按钮调用的文本版本（镜像 binance.py 的 build_*） ----------
async def build_gainers_text_gt(inst_type="SPOT"):
    swap = inst_type == "SWAP"
    try:
        # 现货接口没有品类字段，认股票代币要靠合约的 contract_type + 交易对的 base_name
        data, meta, pairs = await asyncio.gather(
            _get("/futures/usdt/tickers" if swap else "/spot/tickers"),
            contracts(),
            spot_pairs() if not swap else _noop([]))
        title = "永续合约" if swap else "现货"
    except Exception as e:
        log.error(f"Gate 涨幅榜出错: {e}")
        return "Gate 查询失败(可能网络不通)"

    noncrypto = _noncrypto_spot_bases(meta, pairs)
    coins = []
    for t in data:
        pair = t.get("contract") if swap else t.get("currency_pair")
        if not pair or not pair.endswith("_USDT"):
            continue
        base = _base(pair)
        # 代币化股票占合约的三分之一，杠杆代币占现货的两成，混进来这榜就没法看了
        if base in noncrypto or _is_leveraged(base):
            continue
        if swap and not _kind(meta.get(pair))[0]:
            continue
        vol = _f(t, "volume_24h_settle") if swap else _f(t, "quote_volume")
        if vol < MIN_QUOTE_VOL:
            continue
        last = _f(t, "last")
        if last <= 0:
            continue
        coins.append({"sym": base, "price": last,
                      "change": _f(t, "change_percentage")})

    if not coins:
        return f"Gate {title}暂时没有数据"

    g = sorted(coins, key=lambda x: x["change"], reverse=True)[:TOP_N]
    lines = [f"🚀 *Gate {title} 24h涨幅榜*\n"]
    for i, c in enumerate(g, 1):
        # 用 {:+} 而不是写死加号：全市场翻绿那天涨幅榜第一名也是负的，
        # 写死会渲染成 "+-1.12%"
        lines.append(f"{i}. {escape_md(c['sym'])}: {c['change']:+.2f}%")
    lines.append(f"\n📉 *Gate {title} 24h跌幅榜*")
    for i, c in enumerate(sorted(coins, key=lambda x: x["change"])[:TOP_N], 1):
        lines.append(f"{i}. {escape_md(c['sym'])}: {c['change']:.2f}%")
    lines.append("\n(已剔除杠杆代币 3L/3S 与代币化股票等非加密标的)")
    lines.append("⚠️ 不构成投资建议")
    return "\n".join(lines)


async def build_funding_text_gt(symbol):
    pair = _pair(symbol)
    tk, meta = await asyncio.gather(
        _get("/futures/usdt/tickers", {"contract": pair}), contracts(),
        return_exceptions=True)
    if isinstance(tk, Exception):
        return _err_text(tk, symbol, "合约")
    if not tk:
        return f"Gate 未找到 {symbol} 合约"
    meta = {} if isinstance(meta, Exception) else meta

    rate = _f(tk[0], "funding_rate") * 100
    nxt = _f(tk[0], "funding_rate_indicative") * 100
    sym = escape_md(symbol.upper())
    lines = [f"💵 *{sym} 永续合约* (Gate)\n",
             f"资金费率: {rate:+.4f}% ({'偏多' if rate > 0 else '偏空'})"]
    if nxt:
        lines.append(f"下期预测: {nxt:+.4f}%")

    c = meta.get(pair) or {}
    label, note = _interval_note(c.get("funding_interval"))
    if label:
        lines.append(f"结算周期: {label}{note}")
    nxt_ts = c.get("funding_next_apply")
    if nxt_ts:
        try:
            lines.append("下次结算: "
                         + datetime.datetime.fromtimestamp(int(nxt_ts)).strftime("%m-%d %H:%M"))
        except (TypeError, ValueError, OSError):
            pass
    is_crypto, kind = _kind(c)
    if not is_crypto:
        lines.append(f"品类: 代币化{kind}（不是加密货币）")
    lines.append("\n⚠️ 不构成投资建议")
    return "\n".join(lines)


async def build_ratio_text_gt(symbol):
    try:
        d = await _get("/futures/usdt/contract_stats",
                       {"contract": _pair(symbol), "interval": "5m", "limit": 1})
    except Exception as e:
        return _err_text(e, symbol, "多空比")
    if not d:
        return f"Gate 未找到 {symbol} 多空比"

    s = d[-1]
    acct = _f(s, "lsr_account")
    taker = _f(s, "lsr_taker")
    top = _f(s, "top_lsr_account")
    sym = escape_md(symbol.upper())
    lines = [f"⚖️ *{sym} 多空比* (Gate)\n"]
    if acct:
        lines.append(f"账户多空比: {acct:.2f} ({'散户偏多' if acct > 1 else '散户偏空'})")
    if taker:
        lines.append(f"吃单多空比: {taker:.2f} ({'主动买占优' if taker > 1 else '主动卖占优'})")
    if top:
        lines.append(f"大户账户比: {top:.2f} ({'大户偏多' if top > 1 else '大户偏空'})")
    oi = _f(s, "open_interest_usd")
    if oi:
        lines.append(f"持仓量: ${oi:,.0f}")
    lines.append("\n(散户情绪常作反向参考；散户与大户背离时以大户为准)")
    lines.append("⚠️ 不构成投资建议")
    return "\n".join(lines)


async def build_liq_text_gt(symbol):
    """24h 爆仓分布。币安关了公开爆仓接口，Gate 这边是真有数的。"""
    try:
        d = await _get("/futures/usdt/contract_stats",
                       {"contract": _pair(symbol), "interval": "1h", "limit": 24})
    except Exception as e:
        return _err_text(e, symbol, "爆仓数据")
    if not d:
        return f"Gate 未找到 {symbol} 爆仓数据"

    longs = sum(_f(x, "long_liq_usd") for x in d)
    shorts = sum(_f(x, "short_liq_usd") for x in d)
    total = longs + shorts
    sym = escape_md(symbol.upper())
    if total <= 0:
        return (f"💥 *{sym} 爆仓* (Gate)\n\n近 {len(d)} 小时没有爆仓记录。\n"
                f"⚠️ 不构成投资建议")

    lines = [f"💥 *{sym} 近{len(d)}h 爆仓* (Gate)\n",
             f"多头爆仓: ${longs:,.0f} ({longs / total * 100:.0f}%)",
             f"空头爆仓: ${shorts:,.0f} ({shorts / total * 100:.0f}%)",
             f"合计: ${total:,.0f}"]
    worst = max(d, key=lambda x: _f(x, "long_liq_usd") + _f(x, "short_liq_usd"))
    wsum = _f(worst, "long_liq_usd") + _f(worst, "short_liq_usd")
    if wsum > 0:
        try:
            hh = datetime.datetime.fromtimestamp(int(worst["time"])).strftime("%m-%d %H:00")
            side = "多头" if _f(worst, "long_liq_usd") >= _f(worst, "short_liq_usd") else "空头"
            lines.append(f"\n最集中: {hh} ${wsum:,.0f}（{side}为主）")
        except (KeyError, TypeError, ValueError, OSError):
            pass
    lean = "多头被打得更狠" if longs > shorts * 1.5 else (
        "空头被打得更狠" if shorts > longs * 1.5 else "多空都在挨打")
    lines.append(f"\n{lean}（仅 Gate 一家的数据，不是全市场）")
    lines.append("⚠️ 不构成投资建议")
    return "\n".join(lines)


async def build_fprice_text_gt(symbol):
    pair = _pair(symbol)
    tk, meta = await asyncio.gather(
        _get("/futures/usdt/tickers", {"contract": pair}), contracts(),
        return_exceptions=True)
    if isinstance(tk, Exception):
        return _err_text(tk, symbol, "永续合约")
    if not tk:
        return f"Gate 未找到 {symbol} 永续合约"
    t = tk[0]
    meta = {} if isinstance(meta, Exception) else meta
    c = meta.get(pair) or {}

    last = _f(t, "last")
    ch = _f(t, "change_percentage")
    sym = escape_md(symbol.upper())
    emoji = "📈" if ch >= 0 else "📉"
    lines = [f"{emoji} *{sym} 永续合约* (Gate)\n",
             f"价格: ${fmt_price(last)} ({ch:+.2f}%)",
             f"24h高/低: ${fmt_price(_f(t, 'high_24h'))} / ${fmt_price(_f(t, 'low_24h'))}"]

    mark = _f(t, "mark_price")
    index = _f(t, "index_price")
    if mark and index:
        basis = (mark - index) / index * 100
        lines.append(f"标记/指数: ${fmt_price(mark)} / ${fmt_price(index)} ({basis:+.3f}%)")

    rate = _f(t, "funding_rate") * 100
    label, note = _interval_note(c.get("funding_interval"))
    lines.append(f"\n💵 资金费率: {rate:+.4f}% ({'偏多' if rate > 0 else '偏空'})"
                 + (f"　{label}结算" if label else ""))
    if note:
        lines.append(note)

    # 持仓量：Gate 的 total_size 是张数，换算 USD 要乘合约乘数再乘标记价
    size = _f(t, "total_size")
    mult = _f(c, "quanto_multiplier")
    if size and mult and mark:
        lines.append(f"📈 持仓量: ${size * mult * mark:,.0f}")
    elif size:
        lines.append(f"📈 持仓量: {size:,.0f} 张")
    vol = _f(t, "volume_24h_settle")
    if vol:
        lines.append(f"💧 24h成交额: ${vol:,.0f}")

    is_crypto, kind = _kind(c)
    if not is_crypto:
        lines.append(f"\n⚠️ 这是代币化{kind}合约，不是加密货币")
    if c.get("in_delisting"):
        lines.append("⚠️ 该合约已进入退市流程")
    lines.append("\n⚠️ 合约杠杆风险高，不构成投资建议")
    return "\n".join(lines)


async def build_new_text_gt():
    try:
        meta = await contracts()
    except Exception as e:
        log.error(f"Gate 新币榜出错: {e}")
        return "Gate 查询失败(可能网络不通)"

    fresh = [c for c in meta.values()
             if _kind(c)[0] and not c.get("in_delisting") and c.get("launch_time")]
    fresh.sort(key=lambda x: int(x["launch_time"]), reverse=True)
    if not fresh:
        return "Gate 暂无新上线合约"

    lines = ["🆕 *最近上线 Gate 合约的新币*\n"]
    now = datetime.datetime.now()
    for x in fresh[:NEW_N]:
        ld = datetime.datetime.fromtimestamp(int(x["launch_time"]))
        days = (now - ld).days
        ago = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
        extra = []
        if x.get("is_pre_market"):
            extra.append("预市")
        label, _note = _interval_note(x.get("funding_interval"))
        if label and label != "8h":
            extra.append(f"{label}结算")
        tail = f"　[{' '.join(extra)}]" if extra else ""
        lines.append(f"• {escape_md(_base(x['name']))}/USDT - "
                     f"{ld.strftime('%m-%d')} ({ago}){tail}")
    stocks = sum(1 for c in meta.values() if not _kind(c)[0])
    lines.append(f"\n(已剔除 {stocks} 个代币化股票/指数等非加密合约)")
    lines.append("⚠️ 新币风险极高！不构成投资建议")
    return "\n".join(lines)
