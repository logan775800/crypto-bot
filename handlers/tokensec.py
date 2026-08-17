"""代币安全检查：合约权限、买卖税、可卖性、LP 状态、持仓集中度。

价格和 K 线只回答"这个币现在多少钱"，回答不了链上真正会让你血本无归的那些问题：
  • 能不能卖出去（蜜罐 / 貔貅盘）——买得进卖不出，价格再好看也是零；
  • 卖出税多少——见过 30% 卖出税的，涨 40% 才回本；
  • 合约方还能不能增发 / 冻结 / 拉黑你 / 改税率；
  • LP 有没有锁——没锁意味着做市方随时可以把池子抽走（rug）；
  • 前十持有者占多少——高度集中意味着一个地址就能砸穿盘子。

数据源 GoPlus（免费、无需 key）。EVM 和 Solana 是**两套完全不同的字段**，
分开解析：
  EVM    /api/v1/token_security/<chain_id>   buy_tax/sell_tax/cannot_sell_all/
                                             is_mintable/transfer_pausable/lp_holders…
  Solana /api/v1/solana/token_security       mintable/freezable/closable/transfer_fee
                                             （都是 {authority, status} 结构）

⚠️ 最重要的一条原则：**字段缺失一律当"未知"，绝不当"安全"。**
实测查「牛来」时 GoPlus 根本没返回 is_honeypot 这个字段——如果把"没说有问题"
读成"没问题"，那这个功能就是在给用户发假的安全感，比不做还糟。
"""
import logging
import time

from handlers import source as src_mod

log = logging.getLogger(__name__)

GP = "https://api.gopluslabs.io/api/v1"

# GoPlus 用链 ID，我们内部用 bsc/eth 这种 key
CHAIN_ID = {"eth": "1", "bsc": "56", "base": "8453", "arb": "42161", "tron": "tron"}

CACHE_TTL = 600         # 合约属性不会分钟级变化，10 分钟足够
_cache = {}

# 持仓集中度门槛
TOP10_WARN = 50.0       # 前十持有者占比超过这个数就要说
TOP1_WARN = 20.0
TAX_WARN = 10.0         # 单边税超过这个数，来回一趟就吃掉两成
TAX_DANGER = 20.0


def _pct(x):
    """GoPlus 的比例字段有时是 "0.0221"（小数），有时是百分数字符串。
    统一按小数处理——它的文档和实际返回都是 0~1 的小数。"""
    try:
        return float(x) * 100
    except (TypeError, ValueError):
        return None


def _flag(v):
    """GoPlus 的布尔用 "1"/"0" 字符串表示；**缺失返回 None（未知）**，
    不能当成 False——"没说有问题"不等于"没问题"。"""
    if v is None or v == "":
        return None
    return str(v) == "1"


async def _get(path, params):
    r = await src_mod.client().get(f"{GP}{path}", params=params,
                                   headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


async def check(chain_key, address):
    """→ 归一后的安全画像 dict；查不到返回 {"ok": False, "why": ...}。

    结构：
      ok            这次查询本身成不成功
      unknown       []  没拿到结论的项（要如实告诉用户，不能省略）
      dangers       []  会直接让你亏光的（蜜罐、不可卖、能增发…）
      warnings      []  需要知道但不致命的（税、集中度、LP 未锁…）
      buy_tax/sell_tax  百分数或 None
      sellable      True/False/None
      lp_locked_pct 锁仓/销毁的 LP 占比，或 None
      top10_pct     前十持有者占比，或 None
    """
    key = (chain_key, (address or "").lower())
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]

    try:
        if chain_key == "sol":
            out = _parse_solana(await _get("/solana/token_security",
                                           {"contract_addresses": address}), address)
        else:
            cid = CHAIN_ID.get(chain_key)
            if not cid:
                return {"ok": False, "why": f"这条链（{chain_key}）没有安全数据源"}
            out = _parse_evm(await _get(f"/token_security/{cid}",
                                        {"contract_addresses": address}), address)
    except Exception as e:
        log.warning(f"安全检查失败 {chain_key}/{address[:12]}: {e}")
        return {"ok": False, "why": f"安全数据源没查通：{str(e)[:60]}"}

    _cache[key] = (time.monotonic(), out)
    return out


ZERO_ADDR = "0x0000000000000000000000000000000000000000"
BURN_ADDRS = {ZERO_ADDR, "0x000000000000000000000000000000000000dead",
              "0x0000000000000000000000000000000000000001"}
LOCK_WORDS = ("lock", "burn", "black hole", "锁", "销毁")


def _is_locked_holder(h):
    """这份 LP 是不是锁住/销毁了。

    三个信号任一即可：数据源标了 is_locked、地址是烧币地址、tag 里写了锁仓平台。
    烧币地址判定第一版写错过——把 `0x…dEaD` 写成"结尾 24 个 0 加 dead"，
    结果 PEPE 真正烧掉的那份没被算进去。
    """
    addr = (h.get("address") or "").lower()
    tag = (h.get("tag") or "").lower()
    return bool(_flag(h.get("is_locked")) or addr in BURN_ADDRS
                or any(w in tag for w in LOCK_WORDS))


def _lp_status(lp_holders, holders, token_address):
    """→ {lp_locked_pct: 百分比 或 None}。读不懂就返回 None，不硬给一个数。

    为什么要有"读不懂"这一档：实测 PEPE 的 lp_holders 首位是**代币合约自己**
    占 99.88%，这份数据没法解释成"谁持有多少 LP"。这时报"只有 0% 锁仓、
    做市方随时能撤走"是在用一个读不懂的数吓人——和凭它说"安全"一样错。
    """
    if not lp_holders:
        return {"lp_locked_pct": None}
    top = lp_holders[0]
    if (top.get("address") or "").lower() == (token_address or "").lower():
        return {"lp_locked_pct": None}          # 数据形态不可解读
    locked = sum(_pct(h.get("percent")) or 0.0
                 for h in lp_holders if _is_locked_holder(h))
    return {"lp_locked_pct": locked}


def _pick(result, address):
    """GoPlus 的 result 以地址为 key，大小写不一定和请求一致。"""
    if not isinstance(result, dict):
        return None
    for k, v in result.items():
        if k.lower() == (address or "").lower():
            return v
    return next(iter(result.values()), None)


def _parse_evm(d, address):
    r = _pick(d.get("result") or {}, address)
    if not r:
        return {"ok": False, "why": "安全数据源没有这个合约的记录（可能太新）"}

    out = {"ok": True, "chain_kind": "evm", "dangers": [], "warnings": [],
           "unknown": [], "buy_tax": None, "sell_tax": None, "sellable": None,
           "lp_locked_pct": None, "top10_pct": None, "holder_count": None}

    # ── 可卖性：链上最致命的一条 ──
    honeypot = _flag(r.get("is_honeypot"))
    cannot_sell = _flag(r.get("cannot_sell_all"))
    if honeypot is True:
        out["sellable"] = False
        out["dangers"].append("⛔ **蜜罐**：买得进卖不出，这个价格是假的")
    elif cannot_sell is True:
        out["sellable"] = False
        out["dangers"].append("⛔ **不能全部卖出**：合约限制你只能卖一部分")
    elif honeypot is False:
        out["sellable"] = True
    else:
        out["unknown"].append("可卖性（数据源没给结论，不代表安全）")

    # ── 买卖税 ──
    for field, name in (("buy_tax", "买入税"), ("sell_tax", "卖出税")):
        v = _pct(r.get(field))
        out[field] = v
        if v is None:
            out["unknown"].append(name)
        elif v >= TAX_DANGER:
            out["dangers"].append(f"⛔ {name} {v:.0f}%——来回一趟先亏掉这么多")
        elif v >= TAX_WARN:
            out["warnings"].append(f"⚠️ {name} {v:.0f}%")
    if out["buy_tax"] is not None and out["sell_tax"] is not None:
        rt = out["buy_tax"] + out["sell_tax"]
        if TAX_WARN <= rt < TAX_DANGER * 2:
            out["warnings"].append(f"⚠️ 来回成本 {rt:.0f}%——涨这么多才回本")

    # ── 所有权：要先判它，因为它决定下面那些权限还算不算数 ──
    owner = (r.get("owner_address") or "").lower()
    renounced = owner in ("", ZERO_ADDR) or owner in BURN_ADDRS
    # 但"放弃所有权"在两种情况下不作数：可升级代理（逻辑能被换掉）、
    # 存在隐藏所有者。这时权限依然是活的。
    proxy = _flag(r.get("is_proxy")) is True
    hidden = _flag(r.get("hidden_owner")) is True
    out["renounced"] = renounced and not (proxy or hidden)

    # ── 合约权限：这些是"以后还能对你做什么" ──
    # ⚠️ 权限存在 ≠ 现在能被行使。实测 PEPE 的 transfer_pausable / is_blacklisted
    # 都是 1（合约里确实有这些函数），但所有权已放弃、没有代理和隐藏所有者，
    # 没人能再调用它们。第一版把这种情况报成"合约方可以随时冻结交易"——
    # 既吓人又不准确。所以这里按"所有权是否还活着"分两档说。
    perms = [
        ("is_mintable", "可增发", "增发（你的份额会被稀释）", True),
        ("transfer_pausable", "可暂停转账", "暂停转账（交易被冻结）", True),
        ("is_blacklisted", "有黑名单", "把你的地址拉黑（直接卖不掉）", True),
        ("owner_change_balance", "可改余额", "直接修改你的余额", True),
        ("slippage_modifiable", "可改税率", "把税率改高", False),
        ("personal_slippage_modifiable", "可针对个人改税", "单独给你设一个税率", True),
        ("trading_cooldown", "有交易冷却", "让你买完要等一段时间才能卖", False),
    ]
    live = []
    dormant = []
    for field, name, what, severe in perms:
        v = _flag(r.get(field))
        if v is True:
            (live if not out["renounced"] else dormant).append((what, severe))
        elif v is None:
            out["unknown"].append(name)
    for what, severe in live:
        msg = f"{'⛔' if severe else '⚠️'} 合约方可以{what}"
        (out["dangers"] if severe else out["warnings"]).append(msg)
    if dormant:
        out["warnings"].append(
            "⚠️ 合约里存在这些函数：" + "、".join(w for w, _s in dormant)
            + "。**所有权已放弃**（且非代理、无隐藏所有者），"
              "目前没人能调用它们——但换个所有者就又活了")

    # 这两条和所有权无关，任何时候都是硬伤
    if _flag(r.get("can_take_back_ownership")) is True:
        out["dangers"].append("⛔ 所有权可以被拿回去——现在「放弃」了也不算数")
    if hidden:
        out["dangers"].append("⛔ 存在隐藏的所有者地址，「放弃所有权」是假的")
    if proxy:
        out["warnings"].append("⚠️ 这是可升级代理合约，逻辑随时可以被换掉")
    if _flag(r.get("is_open_source")) is False:
        out["dangers"].append("⛔ 合约**未开源**——没人能审，里面写了什么都不知道")

    # ── LP 状态：没锁 = 做市方随时能抽走池子 ──
    out.update(_lp_status(r.get("lp_holders") or [], (r.get("holders") or []),
                          address))
    if out["lp_locked_pct"] is None:
        out["unknown"].append("LP 锁仓情况")
    elif out["lp_locked_pct"] < 50:
        out["warnings"].append(
            f"⚠️ LP 只有 {out['lp_locked_pct']:.0f}% 锁仓/销毁——"
            f"剩下的做市方随时可以撤走池子")

    # ── 持仓集中度 ──
    hs = r.get("holders") or []
    if hs:
        tops = sorted((_pct(h.get("percent")) or 0.0 for h in hs), reverse=True)
        out["top10_pct"] = sum(tops[:10])
        if tops and tops[0] >= TOP1_WARN:
            out["warnings"].append(f"⚠️ 第一大持有者占 {tops[0]:.0f}%，"
                                   f"他一个人就能砸穿盘子")
        if out["top10_pct"] >= TOP10_WARN:
            out["warnings"].append(f"⚠️ 前十持有者共占 {out['top10_pct']:.0f}%")
    else:
        out["unknown"].append("持仓集中度")
    try:
        out["holder_count"] = int(r.get("holder_count"))
    except (TypeError, ValueError):
        pass
    return out


def _parse_solana(d, address):
    """Solana 是另一套字段：权限用 {authority, status} 表达，没有买卖税概念
    （转账费用 transfer_fee 是 Token-2022 的东西）。"""
    r = _pick(d.get("result") or {}, address)
    if not r:
        return {"ok": False, "why": "安全数据源没有这个代币的记录（可能太新）"}

    out = {"ok": True, "chain_kind": "solana", "dangers": [], "warnings": [],
           "unknown": [], "buy_tax": None, "sell_tax": None, "sellable": None,
           "lp_locked_pct": None, "top10_pct": None, "holder_count": None}

    def status(field):
        v = r.get(field)
        if isinstance(v, dict):
            return _flag(v.get("status"))
        return _flag(v)

    for field, msg in (
            ("mintable", "⛔ 铸造权限没放弃——随时可以增发把你稀释"),
            ("freezable", "⛔ 冻结权限没放弃——你的代币可能被冻住卖不掉"),
            ("closable", "⚠️ 账户可被关闭"),
            ("balance_mutable_authority", "⛔ 余额可被合约方修改"),
            ("transfer_hook", "⚠️ 带 transfer hook，转账会走额外逻辑"),
            ("non_transferable", "⛔ 不可转账"),
    ):
        v = status(field)
        if v is True:
            (out["dangers"] if msg.startswith("⛔") else out["warnings"]).append(msg)
        elif v is None:
            out["unknown"].append(field)

    fee = r.get("transfer_fee")
    if isinstance(fee, dict) and fee:
        pct = _pct(fee.get("fee_rate")) if "fee_rate" in fee else None
        out["sell_tax"] = out["buy_tax"] = pct
        if pct and pct >= TAX_WARN:
            out["warnings"].append(f"⚠️ 转账费 {pct:.0f}%")

    hs = r.get("holders") or []
    if hs:
        tops = sorted((_pct(h.get("percent")) or 0.0 for h in hs), reverse=True)
        out["top10_pct"] = sum(tops[:10])
        if tops and tops[0] >= TOP1_WARN:
            out["warnings"].append(f"⚠️ 第一大持有者占 {tops[0]:.0f}%")
        if out["top10_pct"] >= TOP10_WARN:
            out["warnings"].append(f"⚠️ 前十持有者共占 {out['top10_pct']:.0f}%")
    else:
        out["unknown"].append("持仓集中度")
    try:
        out["holder_count"] = int(r.get("holder_count"))
    except (TypeError, ValueError):
        pass
    # Solana 的 LP 锁仓 GoPlus 不给，别装作查过
    out["unknown"].append("LP 锁仓情况（Solana 数据源不提供）")
    return out


def verdict(sec):
    """一句话结论 + 表情。给按钮和列表用。"""
    if not sec or not sec.get("ok"):
        return "❓", "安全性未知"
    if sec["dangers"]:
        return "⛔", f"{len(sec['dangers'])} 项高危"
    if sec["warnings"]:
        return "⚠️", f"{len(sec['warnings'])} 项需注意"
    if sec["unknown"]:
        return "❓", "没有发现问题，但有项目查不到"
    return "✅", "常规检查未发现问题"


def render(sec, symbol=""):
    """安全检查卡片。"""
    if not sec or not sec.get("ok"):
        return (f"🔐 *{symbol} 安全检查*\n\n"
                f"❓ {sec.get('why', '查不到') if sec else '查不到'}\n"
                f"查不到 ≠ 安全——只是没有数据，自己再查一遍合约。")
    icon, one = verdict(sec)
    lines = [f"🔐 *{symbol} 安全检查*　{icon} {one}", "━━━━━━━━━━━━━━"]

    if sec.get("sellable") is True:
        lines.append("✅ 可卖出（数据源未检出蜜罐）")
    bt, st = sec.get("buy_tax"), sec.get("sell_tax")
    if bt is not None or st is not None:
        lines.append(f"💸 买入税 {bt if bt is None else f'{bt:.1f}%'}"
                     f"　卖出税 {st if st is None else f'{st:.1f}%'}")
    if sec.get("lp_locked_pct") is not None:
        lines.append(f"🔒 LP 锁仓/销毁 {sec['lp_locked_pct']:.0f}%")
    if sec.get("top10_pct") is not None:
        lines.append(f"👥 前十持有者占 {sec['top10_pct']:.0f}%"
                     + (f"　持有人 {sec['holder_count']:,}"
                        if sec.get("holder_count") else ""))
    if sec.get("renounced"):
        lines.append("✅ 所有权已放弃")

    if sec["dangers"]:
        lines.append("━━━━━━━━━━━━━━")
        lines.extend(sec["dangers"])
    if sec["warnings"]:
        lines.append("━━━━━━━━━━━━━━")
        lines.extend(sec["warnings"])
    if sec["unknown"]:
        lines.append("━━━━━━━━━━━━━━")
        lines.append("❓ 查不到结论的项：" + "、".join(sec["unknown"][:6]))
        lines.append("　 查不到 ≠ 安全，只是数据源没给。")
    lines.append("\n⚠️ 这些检查只覆盖合约层面的常见套路，"
                 "挡不住团队跑路、拉盘出货这类**行为**风险")
    return "\n".join(lines)
