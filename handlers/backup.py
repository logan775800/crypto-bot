import os
import json
import shutil
import time
import logging
import datetime
from telegram.ext import ContextTypes
from config import DATA_FILE
from storage import data, save_data

# 备份目录跟着 DATA_FILE 走：测试里 DATA_FILE 被隔离到临时文件，
# 备份目录也必须跟着走，否则测试会往生产的 backups/ 里写东西
BACKUP_DIR = os.path.join(os.path.dirname(DATA_FILE) or ".", "backups")
KEEP_DAYS = 7  # 保留最近7天备份

# 定时备份（被job_queue调用）
async def auto_backup(context: ContextTypes.DEFAULT_TYPE):
    try:
        if not os.path.exists(DATA_FILE):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        # 带日期的备份文件名
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        backup_path = os.path.join(BACKUP_DIR, f"data_{date_str}.json")
        shutil.copy2(DATA_FILE, backup_path)
        logging.info(f"数据已备份: {backup_path}")

        # 清理超过KEEP_DAYS天的旧备份
        now = datetime.datetime.now()
        for fname in os.listdir(BACKUP_DIR):
            if not fname.startswith("data_") or not fname.endswith(".json"):
                continue
            fpath = os.path.join(BACKUP_DIR, fname)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))
            if (now - mtime).days > KEEP_DAYS:
                os.remove(fpath)
                logging.info(f"清理旧备份: {fname}")
    except Exception as e:
        logging.error(f"备份出错: {e}")

# 手动备份命令（管理员用）
# ── 恢复 ────────────────────────────────────────────────────────
# 有自动备份却没有恢复入口，等于备份只在"我知道去翻文件"时才有用。
# 2026-08-07 部署覆盖 data.json 那次，就是靠手工才能救。

# 恢复时**只覆盖这些字段**的模式：订阅和配置是最常被误清的，
# 而 vtrade 历史、审计日志、去重记录这些是只增的，用老备份盖回去等于倒退。
SUBS_FIELDS = (
    "contract_watch", "contract_min_tier", "pump_watch", "event_subs",
    "market_watch", "broadcast_chats", "news_subs", "unlock_subs",
    "summary_subs", "analysis_subs", "weekly_subs", "risk_profile",
    "watchpct", "alerts", "ti_alerts", "cond_alerts", "gas_subs",
    "arb_subs", "fex_subs", "whale_addr", "whale_min", "holding_watch",
)


def list_backups():
    """[(文件名, 路径, 大小, 各类订阅条数)]，新的在前。"""
    if not os.path.exists(BACKUP_DIR):
        return []
    out = []
    for fname in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not (fname.startswith("data_") and fname.endswith(".json")):
            continue
        path = os.path.join(BACKUP_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            out.append((fname, path, 0, f"读不出来：{str(e)[:30]}"))
            continue
        bits = []
        for k in ("contract_watch", "pump_watch", "event_subs", "risk_profile",
                  "weekly_subs"):
            v = d.get(k)
            if v:
                bits.append(f"{k}×{len(v)}")
        out.append((fname, path, os.path.getsize(path), "、".join(bits) or "无订阅"))
    return out


def do_restore(path, subs_only=True):
    """把备份恢复进**内存里的 data**，再落盘。

    必须改内存而不是只改文件：运行中的进程持有 data 字典，
    只改文件的话下一次 save_data 就把它盖回去了。
    恢复前先把当前状态另存一份，免得恢复错了没有回头路。
    """
    with open(path, encoding="utf-8") as f:
        old = json.load(f)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    safety = os.path.join(BACKUP_DIR,
                          f"before_restore_{int(time.time())}.json")
    with open(safety, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    changed = []
    if subs_only:
        for k in SUBS_FIELDS:
            if k in old and old[k] != data.get(k):
                data[k] = old[k]
                changed.append(k)
    else:
        data.clear()
        data.update(old)
        changed = list(old.keys())
    # 老备份可能缺后来新增的字段，补齐后再落盘
    from storage import apply_defaults
    apply_defaults()
    save_data()
    return changed, safety


async def restore_cmd(update, context):
    """/restore —— 列备份；/restore <日期> [full] confirm —— 恢复。"""
    from handlers.util import safe_reply
    from config import is_admin
    if not is_admin(update.effective_user.id):
        await safe_reply(update.message, "仅管理员")
        return
    args = [a.lower() for a in (context.args or [])]
    backups = list_backups()
    if not args:
        if not backups:
            await safe_reply(update.message, "还没有任何备份文件")
            return
        lines = ["💾 *可用备份*"]
        for fname, _p, size, summary in backups[:10]:
            date = fname.replace("data_", "").replace(".json", "")
            lines.append(f"`{date}`　{size/1024:.0f}KB　{summary}")
        lines += ["", "恢复订阅/配置（推荐）：`/restore 20260806 confirm`",
                  "整份覆盖（危险）：`/restore 20260806 full confirm`", "",
                  "_默认只恢复订阅与配置类字段。虚拟盘历史、审计日志这些是只增的，"
                  "用老备份盖回去等于倒退，所以不在默认范围内。_",
                  "_恢复前会自动把当前状态另存一份，恢复错了还能回头。_"]
        await safe_reply(update.message, "\n".join(lines), parse_mode="Markdown")
        return

    key = args[0]
    match = [b for b in backups if key in b[0]]
    if not match:
        await safe_reply(update.message, f"找不到备份 `{key}`，先发 /restore 看列表",
                         parse_mode="Markdown")
        return
    full = "full" in args
    if "confirm" not in args:
        await safe_reply(update.message,
            f"⚠️ 将从 `{match[0][0]}` 恢复"
            f"{'**整份数据**（覆盖所有字段）' if full else '订阅与配置字段'}。\n"
            f"确认请加 confirm：\n`/restore {key}{' full' if full else ''} confirm`",
            parse_mode="Markdown")
        return
    try:
        changed, safety = do_restore(match[0][1], subs_only=not full)
    except Exception as e:
        logging.error(f"恢复失败: {e}")
        await safe_reply(update.message, f"恢复失败：{str(e)[:80]}")
        return
    await safe_reply(update.message,
        f"✅ 已从 `{match[0][0]}` 恢复 {len(changed)} 个字段\n"
        + ("　" + "、".join(changed[:12]) + ("…" if len(changed) > 12 else "") if changed
           else "　（内容与当前一致，无改动）")
        + f"\n\n恢复前的状态已另存为 `{os.path.basename(safety)}`",
        parse_mode="Markdown")


async def backup_now(update, context):
    try:
        await auto_backup(context)
        # 统计备份文件
        if os.path.exists(BACKUP_DIR):
            files = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("data_")])
            await update.message.reply_text(
                f"✅ 备份完成\n现有备份: {len(files)}个\n最新: {files[-1] if files else '无'}"
            )
        else:
            await update.message.reply_text("✅ 备份完成")
    except Exception as e:
        logging.error(f"手动备份出错: {e}")
        await update.message.reply_text(f"备份失败：{str(e)[:80]}")
