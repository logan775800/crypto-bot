import os
import shutil
import logging
import datetime
from telegram.ext import ContextTypes
from config import DATA_FILE

BACKUP_DIR = "/app/backups"
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
        await update.message.reply_text("备份失败")
