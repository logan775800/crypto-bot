"""群内 @机器人 自由对话（复用现有 AI 中转站，多轮上下文）。

触发：
  • 群里 @机器人 或 回复机器人的任意消息 → 自由对话（自动阻止后续当币名查价）
  • 群里/私聊发图片（截图看盘、持仓截图）→ 走视觉，让 AI 直接看图
  • 任意场景 /ask 你的问题
每个会话保留最近若干轮上下文，做到连续对话。纯对话，不查实时数据（会引导用命令查）。
"""
import re
import base64
import time
import logging
from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop

from config import AI_API_KEY, AI_BASE_URL
from handlers.util import safe_reply
from handlers.ai import ask_ai_messages, ask_ai_tools

log = logging.getLogger(__name__)

# AI 输出的是 GitHub 风 markdown(## 标题 / **粗体**)，转成 Telegram 旧版 Markdown（单星号粗体、无标题）
_HDR = re.compile(r'(?m)^[ \t]{0,3}#{1,6}[ \t]*(.+?)[ \t]*$')
_BOLD = re.compile(r'\*\*(.+?)\*\*')


def _md_to_tg(t):
    return _BOLD.sub(r'*\1*', _HDR.sub(r'*\1*', t))


def _strip_md(t):
    return _HDR.sub(r'\1', t).replace('**', '').replace('*', '')


async def _send(msg, text):
    """发 AI 回复：不引用原消息(直接发群里)，markdown 渲染失败则降级为去标记纯文本。"""
    try:
        await msg.reply_text(_md_to_tg(text), parse_mode="Markdown", do_quote=False)
    except Exception as e:
        log.warning(f"AI回复Markdown渲染失败，降级纯文本: {e}")
        try:
            await msg.reply_text(_strip_md(text), do_quote=False)
        except Exception as e2:
            log.error(f"AI回复发送失败: {e2}")

MAX_TURNS = 10        # 保留最近 10 轮（20 条）上下文
AI_DAILY_LIMIT = 40   # 每人每日 AI 调用上限（管理员不限），防群里刷爆中转站额度

MAX_IMG_BYTES = 4 * 1024 * 1024   # 单图上限：base64 后约 5.3MB，再大中转站容易 413
PHOTO_TTL = 300                   # 「先发图、后 @提问」的图片保留秒数


# ── 视觉：把 Telegram 图片取成 data URI 喂给中转站 ────────────────────
def _pick_photo(msg):
    """从消息里挑一张能用的图，返回 (file_id, mime)。没有则 (None, None)。

    photo 是 Telegram 压缩过的多尺寸列表，挑不超限的最大张；
    document 覆盖「以文件方式发送」的原图截图（更清晰，看K线/持仓数字更准）。
    """
    if not msg:
        return None, None
    if msg.photo:
        for ps in sorted(msg.photo, key=lambda p: p.file_size or 0, reverse=True):
            if (ps.file_size or 0) <= MAX_IMG_BYTES:
                return ps.file_id, "image/jpeg"
        return msg.photo[0].file_id, "image/jpeg"   # 全都超限就退到最小那张
    doc = msg.document
    if doc and (doc.mime_type or "").startswith("image/") \
            and (doc.file_size or 0) <= MAX_IMG_BYTES:
        return doc.file_id, doc.mime_type
    return None, None


async def _fetch_image(context, file_id, mime):
    """下载并转 data URI。失败返回 None（宁可退化成纯文字回答，也别整条报错）。"""
    try:
        f = await context.bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
    except Exception as e:
        log.warning(f"下载图片失败: {e}")
        return None
    if len(data) > MAX_IMG_BYTES:
        log.warning(f"图片过大({len(data)}B)，跳过")
        return None
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _remember_photo(context, uid, file_id, mime):
    """群里常见「先甩图，下一条才 @机器人提问」——把图暂存，供紧接着的提问引用。"""
    cache = context.chat_data.setdefault("recent_photo", {})
    cache[str(uid)] = {"file_id": file_id, "mime": mime, "ts": time.time()}


def _recall_photo(context, uid):
    cache = context.chat_data.get("recent_photo") or {}
    ent = cache.get(str(uid))
    if not ent or time.time() - ent["ts"] > PHOTO_TTL:
        return None, None
    return ent["file_id"], ent["mime"]


async def _images_for(update, context, msg):
    """给这条提问找配图：本条自带 > 回复的那条 > 同人近 5 分钟内发过的。"""
    uid = update.effective_user.id if update.effective_user else 0
    fid, mime = _pick_photo(msg)
    if not fid:
        fid, mime = _pick_photo(msg.reply_to_message if msg else None)
    if not fid:
        fid, mime = _recall_photo(context, uid)
    if not fid:
        return []
    url = await _fetch_image(context, fid, mime)
    return [url] if url else []

SYSTEM = (
    "你是嵌在一个 Telegram 加密行情机器人里的智能交易助手，用简体中文回答。"
    "用户是做加密杠杆永续合约的活跃交易者（主玩 Bybit/OKX）。\n\n"
    "你有一整套 Bybit 永续的实时数据工具——**别猜、别口头点评，先取数再下结论**：\n"
    "- get_klines(币, 周期)：多周期量化（EMA排列/斜率、ATR14+止损距离、RSI、摆动高低点与"
    "HH-HL/LH-LL 结构、量能倍数、VWAP、前高前低、近8根OHLCV）\n"
    "- get_oi_history(币, 周期)：OI 历史 + 价格/OI 四象限（谁在推动、是否拥挤）\n"
    "- get_funding_history(币)：资金费率历史/预测/是否极端 + 基差(永续溢价折价)\n"
    "- get_orderbook(币)：买卖盘失衡、挂单墙（真卖墙还是诱导）\n"
    "- get_recent_trades(币)：主动买卖 delta、大单方向（突破有没有承接）\n"
    "- get_market_context()：BTC/ETH 多周期 + 情绪\n"
    "- get_liquidations(币)：清算密集/挤压空间\n"
    "- get_my_account()：用户真实 Bybit 账户（权益/持仓/杠杆/爆仓价，仅管理员）\n"
    "- get_my_trade_stats(天数)：用户真实**历史成绩单**（胜率/盈亏比/期望值/最大回撤/"
    "按币·多空·持仓时长·时段拆解的盈亏）。问「我最近怎么样」「我亏在哪」「帮我复盘」"
    "「我这打法行不行」时必调，别凭感觉评价（仅管理员）\n"
    "- get_price / get_contract / get_top_movers / get_fear_greed：快速报价与榜单\n\n"
    "**分析交易计划时的标准流程**（别偷懒只调一个）：\n"
    "0) resolve_symbol(代号) 先把合约身份钉死——这一步不能跳。同一个代号在不同所"
    "可能是**完全不同的项目**；1000PEPE/SHIB1000 这类合约的报价是「1000枚」的价格，"
    "拿现货单价套止损距离会差三个数量级；Bybit 按币计量、OKX 按张计量，"
    "「开100」在两个所根本不是一回事。解析出多个候选时**必须问用户选哪个**，"
    "在他确认之前不许给任何价位。回答开头要写明「交易所 / 交易对 / 标的 / 结算币」。\n"
    "1) get_market_context() 看 BTC/ETH 风险——BTC 15m/1h 破位时，山寨多头计划要降仓或取消；\n"
    "2) get_klines 大周期(4h/1h)定方向与结构，再 get_klines 小周期(15m/5m)定执行；\n"
    "3) get_oi_history + get_funding_history 判断是谁推动、有没有拥挤/挤压风险；\n"
    "4) 需要精确进场时再 get_orderbook + get_recent_trades 看承接与挂单墙；\n"
    "5) 涉及仓位大小/该不该减仓，调 get_my_account() 按真实权益算。\n\n"
    "**盈亏比一律看净的**：给出任何带止盈的方案前调 estimate_costs，用扣掉"
    "手续费/滑点/资金费之后的净盈亏比做判断。用价格距离算的毛盈亏比只能用来对比——"
    "止损 1% 的短线单，光两次吃单手续费就能把毛 1.2:1 压到净 0.98:1，"
    "那是慢性亏损而不是策略。净盈亏比 <1 时要直接说这单不该做，"
    "并给出「入场位挪到哪里才划算」，而不是硬凑一个近的止盈。\n"
    "**给方案时要具体、可执行**：方向与理由、进场区间(挂单还是等回踩/破位确认)、"
    "止损放在哪(用 ATR 或结构失效位，别拍脑袋)、止盈分段(参考流动性/前高前低)、"
    "仓位按「单笔风险≈权益0.5%~1%÷止损距离」反推(拿到账户就用真实数字算，"
    "拿不到就给公式让用户代入)、以及这单的风险温度。\n"
    "**别做的**：不要输出没有数据支撑的价位；不要只给一句「注意风控」；"
    "不要在没查 BTC 联动时就给山寨的追多计划。\n\n"
    "**数据诚信（硬规则，优先级高于「给出完整方案」）**：\n"
    "1. 工具返回里带「⚠️ …暂不可用 / 返回空 / 不足」时，说明**这次没取到**这个维度。"
    "你必须在回答里明说缺了什么，并且**不得**给出依赖它的判断："
    "缺 OI 就别谈「谁在推动/是否拥挤」，缺订单簿就别谈「挂单墙/承接」，"
    "缺清算就别谈「挤压空间」，缺某周期K线就别给该周期的位置。\n"
    "2. **取不到 ≠ 该币没有这项数据**。绝不要说「该币没有资金费率/接口没有数据」"
    "——你手上有这些工具，取不到就是这次取数失败，如实说「暂不可用」。\n"
    "3. 每个工具结果里都带了「数据截至 HH:MM:SS」（交易所时间）。"
    "给结论时带上这个时间，让用户知道结论有多新。看到「数据滞后」提示就要提醒用户。\n"
    "4. 数据不全时，宁可给一个明确降级的结论（「因为缺 X，这单只能给到方向，"
    "精确进场位建议等数据恢复」），也不要用完整的语气输出精确价位。"
    "**假装完整比承认缺失危险得多**——用户会照着假的精确数字下单。\n"
    "5. 需要确认数据状态时调 get_data_status(币)。\n"
    "6. **没调过的工具 = 没有那份数据。** 系统会在你给最终答案前，把这一轮你实际"
    "调到的工具与结果列成「数据清单」发给你，并在你答完后**自动扫描**你有没有用到"
    "清单里没有的维度——用了就会被打回重写。所以：想谈盘口就先调 get_orderbook，"
    "想谈 OI 就先调 get_oi_history，别拿「一般来说这个位置会有买盘」凑数。"
    "凑出来的完整度是假的，用户会照着假数字下单。\n\n"
    "⚠️ 关键：需要实时数据时必须主动调工具，绝不要停下来反问「你先给个币种」。"
    "如果问题需要具体币种但用户没点名是哪个币，就**默认按 BTC** 拉数据分析，"
    "开头说明「以 BTC 为例，换币再告诉我」即可，别只抛问题就结束。"
    "如果是全市场层面的问题（谁涨得猛、现在情绪如何），就调 get_top_movers / get_fear_greed。\n\n"
    "**你能读用户上传的文件**（JSON/CSV/JSONL/文本/日志）。文件留在服务端，你用"
    "file_info（先看这个：字段结构+记录数+样例）、file_fields、file_head、"
    "file_get（按路径取）、file_search（搜关键字）、file_stats（字段统计）按需取数。"
    "**绝不要因为文件大就说读不了**——几十万条也能分析，只是要靠 file_stats 这类"
    "聚合工具而不是逐条看。要下「整体/总共/平均/最多」这类结论时必须用 file_stats，"
    "别拿 file_head 的前几条硬推整体。用户说「我传的文件/这个json/这份数据」时，"
    "直接调 file_info，别反问「请把内容粘贴过来」。\n\n"
    "**你能看图**：用户常发截图（K线截图、持仓/委托截图、别的群或频道的页面截图）。"
    "先把图里的关键信息读出来（币种、方向、杠杆、入场价、浮盈、时间周期、页面是什么），"
    "再用工具取**实时**数据核对——截图上的价格通常已经过时，"
    "不要拿截图里的旧价格当现价下结论，要说明「截图时 X，现在 Y」。"
    "图看不清或缺关键信息（比如没显示币种/周期）就直说缺什么，别猜。\n\n"
    "回答要有料、具体、带风控视角：该展开分析就展开（不用硬憋成一句话），但别啰嗦废话。"
    "聊具体买卖时用「风险温度 / 仓位管理」的口吻而非涨跌预测，可以给出你自己的倾向，"
    "末尾简短带一句「不构成投资建议」即可（不用每段都加）。"
    "也能解释这个 bot 的功能和命令怎么用：/menu 菜单，/price 查价，/dashboard 看板，"
    "/analyze 技术分析，/watchpct 波动监控，/alert 预警，/checklist 合约风控清单，"
    "/vopen 虚拟合约练手、/vpos 虚拟持仓，/trade 实盘交易台(Bybit,点按钮开平仓)。"
)

# ── 给 AI 的数据工具（全部只读）─────────────────────────────────────
def _docfile_tools():
    """文件查询工具。docfile 出问题也不能让整个 TOOLS 定义崩掉。"""
    try:
        from handlers.docfile import TOOLS as _T
        return _T
    except Exception as e:      # pragma: no cover
        log.error(f"加载文件工具失败: {e}")
        return []


_SYM = {"symbol": {"type": "string", "description": "币种代号，如 BTC、ETH、AKE（自动补USDT）"}}
_IV = {"type": "string", "description": "周期：5m/15m/30m/1h/4h/1d",
       "enum": ["5m", "15m", "30m", "1h", "4h", "1d"]}

TOOLS = [
    # 基础
    {"type": "function", "function": {
        "name": "get_price", "description": "查某币现货价格和24h涨跌幅（快速报价用）",
        "parameters": {"type": "object", "properties": _SYM, "required": ["symbol"]}}},
    {"type": "function", "function": {
        "name": "get_contract", "description": "查某币永续合约概览：合约价、资金费率、涨跌",
        "parameters": {"type": "object", "properties": _SYM, "required": ["symbol"]}}},
    {"type": "function", "function": {
        "name": "get_top_movers", "description": "全市场24h涨幅榜/跌幅榜(前15)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_fear_greed", "description": "加密市场恐惧贪婪指数(0-100)",
        "parameters": {"type": "object", "properties": {}}}},
    # 多周期量化分析（Bybit永续）
    {"type": "function", "function": {
        "name": "get_klines",
        "description": ("Bybit永续指定周期的K线量化分析：已在服务端算好 EMA20/50/200与排列、"
                        "EMA20斜率、ATR14(含1.5×ATR止损距离)、RSI14、摆动高低点与市场结构"
                        "(HH/HL、LH/LL)、量能倍数、区间VWAP、前高前低，并附最近8根OHLCV。"
                        "做趋势/结构/止损距离判断时必用。"),
        "parameters": {"type": "object", "properties": {**_SYM, "interval": _IV},
                       "required": ["symbol", "interval"]}}},
    {"type": "function", "function": {
        "name": "get_oi_history",
        "description": ("Bybit永续持仓量(OI)历史+同期价格变化，自动给出四象限解读"
                        "(价涨OI涨=新多进场/价涨OI跌=空头回补/价跌OI涨=新空堆积/价跌OI跌=多头平仓)。"
                        "判断行情由谁推动、是否拥挤时用。"),
        "parameters": {"type": "object", "properties": {**_SYM, "interval": _IV},
                       "required": ["symbol"]}}},
    {"type": "function", "function": {
        "name": "get_funding_history",
        "description": ("Bybit永续资金费率：当前/下一期预测、历史60期均值与区间、正费率占比、"
                        "是否处于历史极端(挤多/轧空风险)，以及标记价vs指数价的基差(永续溢价/折价)。"),
        "parameters": {"type": "object", "properties": _SYM, "required": ["symbol"]}}},
    {"type": "function", "function": {
        "name": "get_orderbook",
        "description": ("Bybit永续L2订单簿(最多200档)：买卖盘总量、失衡百分比、价差、"
                        "挂单墙(单档>5倍均量)。判断前高是真卖墙还是诱导、该挂单还是追单时用。"),
        "parameters": {"type": "object", "properties": {
            **_SYM, "depth": {"type": "integer", "description": "档位数，默认200"}},
            "required": ["symbol"]}}},
    {"type": "function", "function": {
        "name": "get_recent_trades",
        "description": ("Bybit永续逐笔成交：主动买/主动卖量、净delta、大单(>10×均笔)方向。"
                        "判断突破是否有主动买盘承接、跌破后有无吸收时用。"),
        "parameters": {"type": "object", "properties": _SYM, "required": ["symbol"]}}},
    {"type": "function", "function": {
        "name": "get_market_context",
        "description": ("市场联动：BTC/ETH 的 15m/1h/4h 涨跌与EMA20位置、资金费率，加恐惧贪婪指数。"
                        "分析山寨币时必须先看这个——BTC破位时山寨多头计划要降仓/取消。"),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_liquidations",
        "description": "某币近期清算数据(OKX聚合)，用于判断挤压空间与止盈是否该放在流动性密集区前。",
        "parameters": {"type": "object", "properties": _SYM, "required": ["symbol"]}}},
    {"type": "function", "function": {
        "name": "resolve_symbol",
        "description": ("把用户说的币种代号解析成**明确的合约身份**：交易所、交易对、标的、"
                        "结算币、计量单位(币/张)、面值倍数(1000PEPE 这类)、最小下单量、最大杠杆。"
                        "**任何涉及具体价位/仓位的分析之前必须先调它**——同一个代号在不同所"
                        "可能是不同项目，面值倍数搞错止损距离会差几个数量级。"
                        "返回多个候选时不许自己挑，要问用户。"),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "用户说的代号，如 LAB、1000PEPE、AKE"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "estimate_costs",
        "description": ("算这一单**扣掉手续费/滑点/资金费之后**的真实盈亏比。"
                        "滑点按当前盘口逐档吃穿估算(会告诉你深度够不够、会不会部分成交)，"
                        "资金费按预计持仓时长累计，费率优先用账户真实值。"
                        "**给出任何带止盈的计划前都要调它**——用价格距离算的 1.8:1 "
                        "在低流动性小币或窄止损上，净值可能不到 1，甚至是负的。"),
        "parameters": {"type": "object", "properties": {
            **_SYM,
            "side": {"type": "string", "enum": ["long", "short"]},
            "entry": {"type": "number", "description": "入场价"},
            "stop": {"type": "number", "description": "止损价"},
            "tp": {"type": "number", "description": "止盈价（多个TP就用最后一个或分别算）"},
            "notional": {"type": "number", "description": "名义仓位USDT。不知道就用1000试算"},
            "hold_hours": {"type": "number",
                           "description": "预计持仓小时数，用于估资金费。日内填4，隔夜填24"}},
            "required": ["symbol", "side", "entry", "stop", "tp", "notional"]}}},
    {"type": "function", "function": {
        "name": "get_data_status",
        "description": ("体检某币各数据维度现在取不取得到（K线各周期/OI/资金费/盘口/清算），"
                        "含交易所数据时间与完整度百分比。要给完整交易计划前先调它，"
                        "或用户质疑「数据是不是实时/是不是没取到」时调。"),
        "parameters": {"type": "object", "properties": _SYM, "required": ["symbol"]}}},
    # 用户上传的文件（file_info/file_fields/file_head/file_get/file_search/file_stats）
    *_docfile_tools(),
    # 账户只读
    {"type": "function", "function": {
        "name": "get_my_virtual_positions",
        "description": ("读取用户的虚拟合约账户(模拟盘)：余额、当前虚拟持仓(方向/入场/杠杆/"
                        "保证金/理论爆仓价)、历史胜率与累计盈亏。用户问「我这单该不该平」"
                        "「我虚拟仓怎么样」时用。任何人可查自己的。"),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_my_account",
        "description": ("读取用户的 Bybit 真实账户(只读)：USDT总权益、可用保证金、当前持仓"
                        "(方向/均价/杠杆/未实现盈亏/爆仓价)。要按真实账户算仓位、单笔风险、"
                        "是否同向暴露过高、该不该减仓时用。仅管理员可用。"),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_portfolio_risk",
        "description": ("真实账户的**组合风险聚合**：每个仓的风险USDT、同向合计、"
                        "最坏情况回撤%、以及**没设止损的裸奔仓**。还给出每个仓"
                        "减仓25%/50%/全平之后风险和占用各变成多少。"
                        "用户问「该不该减仓」「我现在风险大不大」「能不能再开一单」时**必调**——"
                        "他要的是数字对比，不是再来一遍市场分析。仅管理员。"),
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string",
                       "description": "只看某个仓的减仓推演，留空=看全账户"}}}}},
    {"type": "function", "function": {
        "name": "get_my_trade_stats",
        "description": ("读取用户 Bybit 真实账户的**历史成绩单**(只读)：胜率、盈亏比、每笔期望值、"
                        "最大回撤、连亏、并按币种/多空方向/持仓时长/平仓时段拆解盈亏，含资金费净支出。"
                        "用户问「我最近怎么样」「我亏在哪」「我是不是在追高」「帮我复盘」，"
                        "或要评价他的打法/给改进建议时**必须**调这个——别凭感觉说。仅管理员可用。"),
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer",
                     "description": "回溯天数，默认30。样本太少可放大到90"}}}}},
]


async def _tool_exec(name, args):
    """执行工具，返回给模型的文本结果。任何失败都返回说明字符串，不抛出。"""
    if name == "get_price":
        from api import get_price
        sym = str(args.get("symbol", "")).upper()
        r = await get_price(sym)
        if not r:
            return f"{sym}: 查不到该币现货价"
        return f"{sym} 现货 ${r['price']:,.6g}，24h {r['change']:+.2f}%"
    if name == "get_contract":
        sym = str(args.get("symbol", "")).upper()
        for src in ("okx", "binance", "bybit"):
            try:
                if src == "okx":
                    from handlers.okx import build_fprice_text
                    return await build_fprice_text(sym)
                if src == "binance":
                    from handlers.binance import build_fprice_text_bn
                    return await build_fprice_text_bn(sym)
                from handlers.bybit import build_fprice_text_by
                return await build_fprice_text_by(sym)
            except Exception:
                continue
        return f"{sym}: 三个所都查不到永续合约"
    if name == "get_top_movers":
        from api import get_top_movers
        gainers, losers = await get_top_movers(15)
        g = "、".join(f"{c['symbol']} {c['change']:+.1f}%" for c in gainers[:15])
        l = "、".join(f"{c['symbol']} {c['change']:+.1f}%" for c in losers[:15])
        return f"24h涨幅榜: {g}\n24h跌幅榜: {l}"
    if name == "get_fear_greed":
        from api import get_fear_greed
        fg = await get_fear_greed()
        return f"恐惧贪婪指数 {fg['value']}/100（{fg['classification']}）"
    # ── 多周期量化分析（Bybit 公开接口 + 服务端算指标）──
    from handlers import marketdata as md
    sym = str(args.get("symbol", "")).upper()
    iv = str(args.get("interval", "15m"))
    if name == "get_klines":
        return await md.klines_analysis(sym, iv, args.get("limit"))
    if name == "get_oi_history":
        return await md.oi_analysis(sym, iv)
    if name == "get_funding_history":
        return await md.funding_analysis(sym)
    if name == "get_orderbook":
        return await md.orderbook_analysis(sym, args.get("depth", 200))
    if name == "get_recent_trades":
        return await md.trades_analysis(sym, args.get("limit", 500))
    if name == "get_market_context":
        return await md.market_context()
    if name == "get_liquidations":
        return await md.liquidation_analysis(sym)
    if name == "get_data_status":
        from handlers import datameta
        rep = await datameta.probe(sym)
        return rep.for_ai()
    if name == "estimate_costs":
        from handlers import econ
        try:
            _a, txt = await econ.estimate(
                sym, str(args.get("side") or "long"),
                float(args["entry"]), float(args["stop"]), float(args["tp"]),
                float(args["notional"]), float(args.get("hold_hours") or 8))
            return txt
        except (KeyError, TypeError, ValueError) as e:
            return f"（净盈亏比算不了，参数不全或非法：{str(e)[:60]}）"
    if name == "resolve_symbol":
        from handlers import symbols
        q = str(args.get("query") or args.get("symbol") or "")
        insts, under = await symbols.resolve(q)
        warn, prices = None, {}
        if len(insts) > 1:
            # 跨所同名先比价：一致就说明是同一个项目，不必逼用户选；
            # 差得离谱才是真·同名不同币，那时才必须问清楚
            warn, prices = await symbols.divergence_check(insts)
        return symbols.for_ai(insts, under, warn, prices)
    return f"未知工具 {name}"


async def _account_snapshot():
    """只读账户快照：权益 + 持仓（给 AI 算真实仓位/风险用）。"""
    from handlers.rtrade import _client, _fmt, _env_tag
    client = _client()
    out = [f"【真实账户 {_env_tag()}】"]
    try:
        bal = await client.wallet_balance("USDT")
        out.append(f"总权益 {bal.get('totalEquity','?')} USDT｜可用 {bal.get('totalAvailableBalance','?')} USDT"
                   f"｜未实现盈亏 {bal.get('totalPerpUPL','?')}")
    except Exception as e:
        out.append(f"查余额失败：{str(e)[:60]}")
    try:
        ps = await client.positions_all()
        if not ps:
            out.append("当前无持仓")
        else:
            total = 0.0
            for p in ps:
                upnl = float(p.get("unrealisedPnl", 0) or 0)
                total += upnl
                side = "多" if p.get("side") == "Buy" else "空"
                out.append(f"{p.get('symbol')} {side} {p.get('leverage')}x｜数量 {p.get('size')}"
                           f"｜均价 {_fmt(p.get('avgPrice'))}｜标记 {_fmt(p.get('markPrice'))}"
                           f"｜浮盈 {upnl:+.2f}｜爆仓价 {_fmt(p.get('liqPrice')) if p.get('liqPrice') else '—'}")
            out.append(f"合计浮盈 {total:+.2f} USDT")
    except Exception as e:
        out.append(f"查持仓失败：{str(e)[:60]}")
    return "\n".join(out)


async def _risk_snapshot(symbol=""):
    """组合风险 + 减仓推演，给 AI 回答「该不该减仓」用。

    同向仓位按**完全相关**聚合：BTC 一破位山寨是一起走的，
    把相关性当 0 会让账面风险看起来只有真实值的几分之一。
    """
    from handlers.rtrade import _client, _env_tag
    from handlers import sizing as sz
    from handlers import marketdata as md
    c = _client()
    bal = await c.wallet_balance("USDT")
    equity = float(bal.get("totalEquity") or 0)
    poss = await c.positions_all()
    r = sz.portfolio_risk(poss, equity)
    out = [f"【组合风险 {_env_tag()}】总权益 {equity:,.2f} USDT"]
    if not r["positions"]:
        out.append("当前无持仓 —— 没有敞口，任何新单都是第一笔风险。")
        return "\n".join(out)
    for x in r["positions"]:
        tag = "⚠️裸奔(无止损)" if x["naked"] else ""
        out.append(f"{x['symbol']} {'多' if x['side']=='long' else '空'}｜"
                   f"名义 {x['value']:,.0f}｜风险 {x['risk']:,.2f} USDT"
                   f"（{x['risk']/equity*100:.2f}%）{tag}")
    out.append(f"同向合计：多 {r['long_pct']:.2f}%｜空 {r['short_pct']:.2f}%")
    out.append(f"**最坏情况回撤 {r['worst_pct']:.2f}%**"
               f"（同向仓位按完全相关计——BTC 破位时山寨是一起走的，不存在分散）")
    if r["naked"]:
        out.append(f"⚠️ 裸奔仓（没设止损）：{'、'.join(str(s) for s in r['naked'])}"
                   f" —— 它们的风险不是计划内的那点，是到爆仓为止")
    # 减仓推演
    targets = [p for p in poss if not symbol or md.norm(symbol) == p.get("symbol")]
    for p in targets[:4]:
        rows = sz.reduce_scenarios(p, equity)
        if not rows:
            continue
        out.append(f"\n{p.get('symbol')} 减仓推演：")
        for row in rows:
            out.append(f"　减{row['pct']}% → 落袋 {row['realized']:+,.2f}｜"
                       f"剩余名义 {row['left_value']:,.0f}｜"
                       f"剩余风险 {row['left_risk']:,.2f} USDT"
                       + (f"（{row['left_risk_pct']:.2f}%）" if row['left_risk_pct'] is not None else ""))
    return "\n".join(out)


async def _stats_snapshot(days=30):
    """真实账户历史成绩单（给 AI 做复盘用）。复用 /rstats 的统计口径，
    只丢统计结论不丢原始几百笔——省 token，模型也算不准。"""
    from handlers.rstats import _load, build_ai_digest, MAX_DAYS
    days = max(1, min(int(days or 30), MAX_DAYS))
    trades, fund = await _load(days)
    if not trades:
        return f"【真实账户成绩单】近{days}天没有已平仓记录（样本为空，别硬下结论）"
    return "【真实账户成绩单】\n" + build_ai_digest(trades, days, fund)


def _virtual_snapshot(uid):
    """该用户的虚拟合约持仓/账户（给 AI 聊「我这单该不该平」用）。"""
    from handlers.vtrade import _acct, _pnl, _liq, START_BALANCE
    a = _acct(str(uid))
    pos = a.get("positions", {})
    hist = a.get("history", [])
    out = [f"【虚拟合约账户】可用余额 ${a.get('balance', 0):,.2f}（初始 ${START_BALANCE:,.0f}）"]
    if not pos:
        out.append("当前无虚拟持仓")
    else:
        out.append("持仓（浮盈需按当前价算，可再调 get_contract/get_klines 取现价）：")
        for sym, p in pos.items():
            out.append(f"{sym} {'多' if p['side']=='long' else '空'} {p['lev']:g}x"
                       f"｜入场 {p['entry']}｜保证金 ${p['margin']:,.2f}"
                       f"｜仓位 ${p['margin']*p['lev']:,.2f}｜理论爆仓价 {_liq(p):.8g}")
    if hist:
        wins = [h for h in hist if h["pnl"] >= 0]
        out.append(f"历史 {len(hist)} 笔｜胜率 {len(wins)/len(hist)*100:.0f}%"
                   f"｜累计盈亏 {sum(h['pnl'] for h in hist):+,.2f}")
    return "\n".join(out)


def _ai_quota_ok(context, uid):
    """AI 每人每日调用上限，防群里刷爆中转站额度。管理员不限。"""
    from config import is_admin
    if is_admin(uid):
        return True, 0
    today = time.strftime("%Y-%m-%d")
    q = context.user_data.get("ai_quota") or {}
    if q.get("date") != today:
        q = {"date": today, "count": 0}
    if q["count"] >= AI_DAILY_LIMIT:
        context.user_data["ai_quota"] = q
        return False, q["count"]
    q["count"] += 1
    context.user_data["ai_quota"] = q
    return True, q["count"]


def _make_exec(update, context, mf=None):
    """按调用者身份包一层：账户类工具需鉴权，其余走公开数据工具。

    mf：本轮数据清单。每个工具结果都过一遍记账，最终答案要按它受约束——
    模型没调过的工具就是没有那份数据，不许凭"一般来说"补全。
    """
    async def _exec(name, args):
        res = await _exec_inner(name, args)
        if mf is not None:
            try:
                mf.record(name, args, res)
                if mf.symbol is None:
                    sym = str((args or {}).get("symbol") or "").upper()
                    if sym:
                        mf.symbol = sym
            except Exception as e:      # 记账绝不能影响正常回答
                log.warning(f"数据清单记账失败: {e}")
        return res

    async def _exec_inner(name, args):
        uid = update.effective_user.id if update.effective_user else 0
        # 文件工具按会话取缓存的上传文件（同群共享，谁传的谁都能问）
        from handlers import docfile
        if name in docfile.TOOL_NAMES:
            return docfile.run_tool(update.effective_chat.id, name, args)
        if name == "get_my_account":
            from config import is_admin
            if not is_admin(uid):
                return "（无权限：只有管理员能查询真实账户）"
            try:
                return await _account_snapshot()
            except RuntimeError:
                return "（未配置 Bybit API 密钥，拿不到真实账户数据；可改用虚拟盘 get_my_virtual_positions）"
        if name == "get_my_trade_stats":
            from config import is_admin
            if not is_admin(uid):
                return "（无权限：只有管理员能查询真实账户成绩单）"
            try:
                return await _stats_snapshot(int(args.get("days") or 30))
            except RuntimeError:
                return "（未配置 Bybit API 密钥，拿不到历史成绩单）"
            except Exception as e:
                return f"（拉取成绩单失败：{str(e)[:80]}）"
        if name == "get_portfolio_risk":
            from config import is_admin
            if not is_admin(uid):
                return "（无权限：只有管理员能查询真实账户风险）"
            try:
                return await _risk_snapshot(str(args.get("symbol") or "").upper())
            except RuntimeError:
                return "（未配置 Bybit API 密钥，拿不到真实持仓）"
            except Exception as e:
                return f"（组合风险计算失败：{str(e)[:80]}）"
        if name == "get_my_virtual_positions":
            return _virtual_snapshot(uid)
        return await _tool_exec(name, args)
    return _exec


# ── 会话上下文：记住在聊哪个币、什么方向、什么价位 ────────────────
# 用户的真实说法是连着来的：「分析AKE」→「回踩能不能多」→「我已经在0.00405做多」
# → 「现在要不要减仓」。每句都重新猜币种会让后三句全部答偏。
_CTX_TTL = 3600          # 一小时没提就当换话题了，别拿隔夜的入场价当现在的
_ENTRY_PAT = re.compile(r"(?:在|@)\s*([0-9]*\.?[0-9]+)\s*(?:做多|做空|开多|开空|进|入|多|空)")
_SIDE_PAT = re.compile(r"(做多|开多|多单|抄底|long)|(做空|开空|空单|long?short|short)", re.I)
_CLEAR_PAT = re.compile(r"^\s*(/clear|清空上下文|换个?币|重新开始)\s*$")


def _ctx(context):
    c = context.chat_data.get("trade_ctx") or {}
    if c and time.time() - c.get("ts", 0) > _CTX_TTL:
        return {}
    return c


def update_ctx(context, text, symbol=None):
    """从用户这句话里抽出币种/方向/入场价，累积到会话上下文。

    只**补充**不覆盖：没提方向就沿用上次的，这样「现在要不要减仓」
    还知道说的是哪个仓。显式说了新的才更新。
    """
    c = dict(_ctx(context))
    if symbol:
        if c.get("symbol") and c["symbol"] != symbol:
            c = {"symbol": symbol}        # 换币了，旧的方向/入场价一律作废
        else:
            c["symbol"] = symbol
    m = _ENTRY_PAT.search(text or "")
    if m:
        try:
            c["entry"] = float(m.group(1))
        except ValueError:
            pass
    s = _SIDE_PAT.search(text or "")
    if s:
        c["side"] = "long" if s.group(1) else "short"
    if c:
        c["ts"] = time.time()
        context.chat_data["trade_ctx"] = c
    return c


def ctx_hint(context):
    """把上下文渲染成给模型的一句话。空的就不说，别凭空造出一个币。"""
    c = _ctx(context)
    if not c:
        return ""
    bits = []
    if c.get("symbol"):
        bits.append(f"币种 {c['symbol']}")
    if c.get("side"):
        bits.append("方向 " + ("做多" if c["side"] == "long" else "做空"))
    if c.get("entry"):
        bits.append(f"用户自述入场价 {c['entry']:g}")
    if not bits:
        return ""
    return ("【会话上下文】" + "、".join(bits) +
            "。用户这句话如果没点名币种，默认就是它，不要反问也不要换成 BTC。"
            "入场价是**用户自己说的**，不是行情数据——引用时要说明来源，"
            "别把它当成取数得到的成交价。")


def _guard(mf):
    """把数据清单包成 ask_ai_tools 认的校验器：(违规列表, 重写指令)。"""
    def check(text):
        bad = mf.violations(text)
        return bad, (mf.fix_prompt(bad) if bad else "")
    return check


def _history(context):
    return context.chat_data.setdefault("chat_hist", [])


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str,
                 images=None):
    if not AI_API_KEY or not AI_BASE_URL:
        await safe_reply(update.message, "AI 未配置（缺 AI_API_KEY / AI_BASE_URL）")
        return
    uid = update.effective_user.id if update.effective_user else 0
    ok, used = _ai_quota_ok(context, uid)
    if not ok:
        await safe_reply(update.message,
            f"🚦 你今天的 AI 提问已达上限（{AI_DAILY_LIMIT} 次），明天恢复。\n"
            f"（行情类命令 /price /analyze /watchpct 等不受影响）")
        return
    hist = _history(context)
    if images:
        # OpenAI 视觉格式：content 是 [文本, 图片...] 数组
        user_msg = {"role": "user", "content":
                    [{"type": "text", "text": user_text}] +
                    [{"type": "image_url", "image_url": {"url": u}} for u in images]}
    else:
        user_msg = {"role": "user", "content": user_text}
    hist.append(user_msg)
    # 只保留最近 MAX_TURNS 轮，防止上下文无限增长
    if len(hist) > MAX_TURNS * 2:
        del hist[: len(hist) - MAX_TURNS * 2]
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass
    try:
        # 优先带工具调用（能查实时数据）；工具链路异常则降级为纯对话
        try:
            from handlers.manifest import Manifest
            mf = Manifest()
            update_ctx(context, user_text)
            sys_prompt = SYSTEM + ("\n\n" + ctx_hint(context) if ctx_hint(context) else "")
            reply = await ask_ai_tools(
                hist, TOOLS, _make_exec(update, context, mf), system=sys_prompt,
                ledger=mf.ledger, guard=_guard(mf))
            # 模型解析出的币种回填进上下文，下一句「要不要减仓」就不用再猜
            if mf.symbol:
                update_ctx(context, "", mf.symbol)
            head = mf.header()
            if head:
                reply = f"{head}\n\n{reply}"
        except Exception as te:
            log.warning(f"工具对话失败，降级纯对话: {te}")
            reply = await ask_ai_messages(hist, system=SYSTEM)
    except Exception as e:
        log.error(f"群聊AI出错: {e}")
        # 出错不留脏上下文
        if hist and hist[-1]["role"] == "user":
            hist.pop()
        detail = str(e)[:160]
        tip = "\n（模型都不可用：管理员可用 `/aimodel test` 探活、`/aimodel <名字>` 切换）" \
            if "模型" in detail or "model" in detail.lower() else ""
        await safe_reply(update.message, f"AI 出错了，稍后再试：{detail}{tip}")
        return
    # 图片只在本轮上传：历史里换回纯文本占位，否则往后每轮都要重传几百KB base64
    if images:
        user_msg["content"] = user_text + "\n（用户此轮附了一张图片，见上文你的解读）"
    hist.append({"role": "assistant", "content": reply})
    await _send(update.message, reply)


async def handle_ask_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """菜单「💬 AI 助手」按钮点完后，用户发来的问题走这里（quickprice 拦截 await_ask 调用）。"""
    await _reply(update, context, text)


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ask 你的问题 —— 任意场景可用（私聊也行）。"""
    q = " ".join(context.args).strip() if context.args else ""
    if not q:
        await safe_reply(update.message,
            "用法：`/ask 你的问题`\n例：`/ask BTC 现在追多风险大不大` 或 `/ask 怎么用虚拟合约练手`",
            parse_mode="Markdown")
        return
    await _reply(update, context, q)


async def reset_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resetchat 清空当前会话的对话记忆 + 交易上下文。

    两个一起清：只清对话记忆而留着「币种/方向/入场价」，下一句还是会被
    旧的币种带偏，用户会以为没清干净。
    """
    context.chat_data.pop("chat_hist", None)
    had = context.chat_data.pop("trade_ctx", None)
    extra = ""
    if had and had.get("symbol"):
        extra = f"（原来在聊 {had['symbol']}，也一并忘了）"
    await safe_reply(update.message, f"🧹 已清空对话记忆与交易上下文，重新开始。{extra}")


async def mention_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群里 @机器人 或 回复机器人的消息时触发自由对话。私聊不自动触发（用 /ask）。"""
    msg = update.message
    if not msg or not msg.text:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return  # 私聊纯文字留给查价；私聊用 /ask
    # 用户正处在引导式流程中(设监控/预警/开仓等 await_ 态)时别抢——即便他是回复机器人的提示，
    # 也要让这条走 quickprice 完成流程，否则会被当成 AI 提问，流程收不到输入。
    if any(k.startswith("await_") for k in context.user_data):
        return
    text = msg.text
    triggered = False
    # 1) 回复机器人的消息
    rt = msg.reply_to_message
    if rt and rt.from_user and rt.from_user.id == context.bot.id:
        triggered = True
    # 2) @机器人
    uname = context.bot.username
    if uname and ("@" + uname) in text:
        triggered = True
        text = text.replace("@" + uname, "").strip()
    if not triggered:
        return
    if not text:
        text = "你好"
    # 纯文字提问也可能是在问刚发的那张图（回复图片 / 先甩图再 @提问）
    images = await _images_for(update, context, msg)
    if images and not text.strip():
        text = "看看这张图"
    await _reply(update, context, text, images=images)
    # 已处理，阻止后续 quick_price 再把它当币名查价
    raise ApplicationHandlerStop


async def photo_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """收到图片：私聊直接看图；群里需 @机器人 或 回复机器人（和文字对话同规则）。

    群里没触发的图也会暂存 PHOTO_TTL 秒——用户常常先甩图、下一条才 @机器人提问。
    """
    msg = update.message
    if not msg:
        return
    # 引导式流程（设监控/开仓等 await_ 态）进行中时不抢，避免打断流程
    if any(k.startswith("await_") for k in context.user_data):
        return
    fid, mime = _pick_photo(msg)
    if not fid:
        return
    uid = update.effective_user.id if update.effective_user else 0
    _remember_photo(context, uid, fid, mime)

    caption = (msg.caption or "").strip()
    if update.effective_chat.type == "private":
        triggered = True
    else:
        triggered = False
        rt = msg.reply_to_message
        if rt and rt.from_user and rt.from_user.id == context.bot.id:
            triggered = True
        uname = context.bot.username
        if uname and ("@" + uname) in caption:
            triggered = True
            caption = caption.replace("@" + uname, "").strip()
    if not triggered:
        return   # 群里没点名，安静收着，别刷屏

    url = await _fetch_image(context, fid, mime)
    if not url:
        await safe_reply(msg, "这张图我没取到（可能太大或下载失败），换张小一点的再发一次？")
        raise ApplicationHandlerStop
    await _reply(update, context, caption or "看看这张图，告诉我这是什么、有什么关键信息。",
                 images=[url])
    raise ApplicationHandlerStop


async def doc_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """收到非图片附件（JSON/CSV/文本/日志）：下载→解析→缓存，再让 AI 用文件工具分析。

    文件不进上下文，只把结构摘要给模型，剩下的靠 file_* 工具按需查——
    1.3MB 的 result.json 直接塞进去会超窗口，而且模型逐条算也不准。
    """
    from handlers import docfile
    msg = update.message
    if not msg or not msg.document:
        return
    if any(k.startswith("await_") for k in context.user_data):
        return
    doc = msg.document
    caption = (msg.caption or "").strip()
    chat_type = update.effective_chat.type
    if chat_type == "private":
        triggered = True
    else:
        triggered = False
        rt = msg.reply_to_message
        if rt and rt.from_user and rt.from_user.id == context.bot.id:
            triggered = True
        uname = context.bot.username
        if uname and ("@" + uname) in caption:
            triggered = True
            caption = caption.replace("@" + uname, "").strip()

    if not docfile.is_supported(doc):
        if triggered:
            await safe_reply(msg, f"这个格式我还读不了（{doc.mime_type or doc.file_name}）。"
                                  f"目前支持 JSON / JSONL / CSV / TSV / TXT / LOG / MD / YAML。")
            raise ApplicationHandlerStop
        return
    if (doc.file_size or 0) > docfile.MAX_FILE_BYTES:
        if triggered:
            await safe_reply(msg, f"文件 {doc.file_size/1048576:.1f}MB，超过 Telegram "
                                  f"给机器人的 20MB 下载上限，我拿不到。麻烦拆分或先筛一遍再传。")
            raise ApplicationHandlerStop
        return

    try:
        f = await context.bot.get_file(doc.file_id)
        raw = bytes(await f.download_as_bytearray())
    except Exception as e:
        log.warning(f"下载文件失败: {e}")
        if triggered:
            await safe_reply(msg, f"文件没下载下来：{str(e)[:80]}")
            raise ApplicationHandlerStop
        return

    text, enc = docfile.decode(raw)
    entry = docfile.put(update.effective_chat.id, doc.file_name or "file",
                        len(raw), text, enc)
    log.info(f"已缓存文件 {entry['name']} {len(raw)}B kind={entry['kind']} enc={enc}")

    # 群里没点名：静静收好，等他 @ 提问时文件工具就能用了
    if not triggered:
        return

    hint = ("这是一个解析失败/非结构化的文本文件" if entry["kind"] == "text"
            else f"已解析为 {entry['kind']}")
    await _reply(update, context,
                 (caption or "分析一下我刚传的这个文件。") +
                 f"\n（系统提示：用户上传了文件 {entry['name']}，"
                 f"{len(raw)/1024:,.0f}KB，{hint}。用 file_info 等工具去读，"
                 f"不要让用户粘贴内容。）")
    raise ApplicationHandlerStop
