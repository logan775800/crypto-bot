"""更新日志：启动播报带上本版改了什么，`/changelog` 可查任意版本。

以前每次部署完只播一句「Bot 已启动（版本 v1.16.1）」，版本号对使用者没有信息量——
到底修了什么、哪个按钮能用了，只能靠我在聊天里说一遍，隔天就忘。

日志源是仓库根目录的 CHANGELOG.md（人写、机器读），不依赖 git：
容器里没有 git 命令，靠 `git log` 取说明在服务器上会直接抓瞎。
"""
import logging
import os
import re

log = logging.getLogger(__name__)

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "CHANGELOG.md")

# 「## v1.16.1　(2026-08-14)」——日期可有可无
_HEAD = re.compile(r"^##\s+(v\d+\.\d+\.\d+)\s*[（(]?\s*([\d-]+)?\s*[)）]?\s*$")

_cache = {"mtime": None, "data": None}


def _parse(text):
    """→ [(版本, 日期, [条目...])]，保持文件里的顺序（新版在前）。"""
    out, cur = [], None
    for line in text.splitlines():
        m = _HEAD.match(line.strip())
        if m:
            cur = (m.group(1), m.group(2) or "", [])
            out.append(cur)
            continue
        if cur is None:
            continue                      # 文件开头的说明文字
        s = line.strip()
        if s.startswith(("- ", "* ")):
            cur[2].append(s[2:].strip())
    return out


def load(force=False):
    """带 mtime 缓存：这文件每次发版才变，没必要每条命令都读盘。"""
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        return []
    if not force and _cache["mtime"] == mtime and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(PATH, encoding="utf-8") as f:
            data = _parse(f.read())
    except OSError as e:
        log.warning(f"读不到更新日志: {e}")
        return []
    _cache.update(mtime=mtime, data=data)
    return data


def notes_for(version):
    """某个版本的条目列表；没有该版本返回 []。"""
    for ver, _date, items in load():
        if ver == version:
            return items
    return []


def date_for(version):
    for ver, date, _items in load():
        if ver == version:
            return date
    return ""


def render(version, max_items=12):
    """一个版本的 Markdown 段落。没写说明时明说，不要装作没这回事。"""
    items = notes_for(version)
    date = date_for(version)
    head = f"📋 *{version}* 更新内容" + (f"　({date})" if date else "")
    if not items:
        return f"{head}\n\n（这一版没写更新说明）"
    shown = items[:max_items]
    body = "\n".join(f"• {it}" for it in shown)
    more = f"\n…还有 {len(items) - max_items} 条，看 /changelog {version}" \
        if len(items) > max_items else ""
    return f"{head}\n\n{body}{more}"


def render_recent(n=5):
    data = load()[:n]
    if not data:
        return "📋 还没有更新日志"
    blocks = []
    for ver, date, items in data:
        head = f"*{ver}*" + (f"　{date}" if date else "")
        body = "\n".join(f"• {it}" for it in items[:6])
        if len(items) > 6:
            body += f"\n… 共 {len(items)} 条"
        blocks.append(f"{head}\n{body}")
    return "📋 *最近更新*\n\n" + "\n\n".join(blocks)


def kb():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 最近 5 个版本", callback_data="cl:recent")],
        [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
    ])


async def changelog_cmd(update, context):
    """/changelog [版本] —— 不带参数看当前版本，带了看指定版本。"""
    from handlers.util import safe_reply
    from config import VERSION
    arg = (context.args[0] if context.args else "").strip()
    if arg in ("all", "recent", "最近"):
        await safe_reply(update.message, render_recent(), reply_markup=kb(),
                         parse_mode="Markdown")
        return
    ver = arg if arg.startswith("v") else (f"v{arg}" if arg else VERSION)
    text = render(ver)
    if not notes_for(ver):
        have = [v for v, _d, _i in load()][:8]
        text += "\n\n有记录的版本：" + "、".join(have)
    await safe_reply(update.message, text, reply_markup=kb(), parse_mode="Markdown")


async def on_button(query, context):
    """处理 cl:* 回调。由 menu.button_handler 转发。"""
    from handlers.util import safe_edit
    from config import VERSION
    what = query.data.split(":", 1)[1]
    text = render_recent() if what == "recent" else render(VERSION)
    await safe_edit(query, text, reply_markup=kb(), parse_mode="Markdown")


def startup_text(version):
    """启动播报正文。日志缺失时退回原来那句，绝不因为没写说明就不播报。"""
    items = notes_for(version)
    head = f"🟢 Bot 已启动/重启（版本 {version}）"
    if not items:
        return f"{head}\n所有功能已加载，开始运行"
    body = "\n".join(f"• {it}" for it in items[:8])
    more = f"\n…共 {len(items)} 条，/changelog 看全部" if len(items) > 8 else ""
    return f"{head}\n\n本次更新：\n{body}{more}\n\n所有功能已加载，开始运行"


def update_text(version):
    """发给订阅会话的更新播报。

    和 startup_text 分开写：管理员要的是「进程起来了」这个运维信号，每次重启都该发；
    群里的人不关心容器重启，只关心**行为变了什么** —— 拿「已启动/重启」去刷群，
    几次之后大家就不看了。没有更新说明就返回空，宁可不播报也不发一句废话。
    """
    items = notes_for(version)
    if not items:
        return ""
    body = "\n".join(f"• {it}" for it in items[:8])
    more = f"\n…共 {len(items)} 条，/changelog 看全部" if len(items) > 8 else ""
    return f"🆕 机器人已更新到 {version}\n\n{body}{more}"
