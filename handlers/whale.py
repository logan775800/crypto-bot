import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes

# 多链配置：链名 -> (RPC, 原生币符号, 默认大额阈值)
CHAINS = {
    "eth": {
        "rpc": "https://ethereum-rpc.publicnode.com",
        "coin": "ETH", "threshold": 100, "name": "以太坊"
    },
    "bsc": {
        "rpc": "https://bsc-rpc.publicnode.com",
        "coin": "BNB", "threshold": 100, "name": "BSC币安链"
    },
    "polygon": {
        "rpc": "https://polygon-bor-rpc.publicnode.com",
        "coin": "MATIC", "threshold": 50000, "name": "Polygon"
    },
    "arb": {
        "rpc": "https://arbitrum-one-rpc.publicnode.com",
        "coin": "ETH", "threshold": 50, "name": "Arbitrum"
    },
}

async def _rpc(rpc_url, method, params):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(rpc_url, json={
            "jsonrpc": "2.0", "method": method, "params": params, "id": 1
        })
        resp.raise_for_status()
        return resp.json().get("result")

def wei_to_coin(wei_hex):
    return int(wei_hex, 16) / 1e18

async def _scan_chain(chain_key, threshold=None):
    """扫描某链最新区块的大额转账，返回文本"""
    cfg = CHAINS[chain_key]
    coin = cfg["coin"]
    if threshold is None:
        threshold = cfg["threshold"]
    block = await _rpc(cfg["rpc"], "eth_getBlockByNumber", ["latest", True])
    if not block:
        return f"获取 {cfg['name']} 区块失败"
    block_num = int(block["number"], 16)
    txs = block.get("transactions", [])
    big = []
    for tx in txs:
        try:
            v = wei_to_coin(tx["value"])
        except (KeyError, ValueError):
            continue
        if v >= threshold:
            big.append({"from": tx["from"], "to": tx.get("to") or "合约创建", "amt": v})
    if not big:
        return (f"🐋 {cfg['name']} 区块 #{block_num}\n"
                f"无 >{threshold:g} {coin} 的转账 (共{len(txs)}笔)\n"
                f"可降低阈值，如 /whale {chain_key} 10")
    big.sort(key=lambda x: x["amt"], reverse=True)
    lines = [f"🐋 *{cfg['name']} 巨鲸转账*\n区块 #{block_num} | 共{len(txs)}笔\n"]
    for b in big[:10]:
        sf = b["from"][:6] + "..." + b["from"][-4:]
        st = b["to"][:6] + "..." + b["to"][-4:] if b["to"].startswith("0x") else b["to"]
        lines.append(f"💰 {b['amt']:,.2f} {coin}\n   {sf} → {st}")
    if len(big) > 10:
        lines.append(f"\n...还有{len(big)-10}笔")
    lines.append(f"\n(仅{coin}原生转账，不含代币)")
    return "\n".join(lines)

# /whale [链] [阈值]
async def whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    chain_key = "eth"  # 默认ETH
    threshold = None
    if args:
        # 第一个参数：如果是链名，用它；否则当阈值（兼容旧用法 /whale 10）
        if args[0].lower() in CHAINS:
            chain_key = args[0].lower()
            if len(args) > 1:
                try:
                    threshold = float(args[1])
                except ValueError:
                    pass
        else:
            # 旧用法 /whale 10 = ETH链阈值10
            try:
                threshold = float(args[0])
            except ValueError:
                pass
    cfg = CHAINS[chain_key]
    th = threshold if threshold is not None else cfg["threshold"]
    await update.message.reply_text(
        f"🐋 扫描 {cfg['name']} 最新区块 >{th:g} {cfg['coin']} 的大额转账..."
    )
    try:
        text = await _scan_chain(chain_key, threshold)
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"巨鲸监控出错({chain_key}): {e}")
        await update.message.reply_text("查询失败，请稍后再试")

# 供按钮调用（默认ETH）
async def build_whale_text(threshold=100):
    try:
        return await _scan_chain("eth", threshold)
    except Exception as e:
        logging.error(f"build_whale_text出错: {e}")
        return "查询失败"
