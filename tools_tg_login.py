"""一次性：用你的 Telegram 账号登录，换出一串 session，之后就不用再输验证码了。

⚠️ **这个脚本必须在能连上 Telegram 的机器上跑**（服务器上，或你自己电脑）。
开发机 D:\\Scripts 那台连不上 t.me，在那儿跑会一直超时。

## 跑之前

1. 去 https://my.telegram.org → API development tools → 建一个应用，
   拿到 **api_id**（数字）和 **api_hash**（32 位字符串）。
2. 在服务器上：

       cd /data/crypto-bot
       docker compose exec crypto-bot python tools_tg_login.py

   （或者本地装个 telethon 直接跑：pip install telethon）

3. 按提示输入 api_id、api_hash、手机号（带国家码，如 +8613800138000），
   再输入 Telegram 发给你的验证码。开了两步验证的话还要输密码。

4. 跑完会打印一串很长的 session。把三个值填进服务器的 `.env`：

       TG_API_ID=1234567
       TG_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
       TG_SESSION=<那一长串>

   然后 `docker compose up -d --force-recreate` 让它生效。

## 这串 session 是什么

**它等于你的账号登录凭证**——拿到它的人可以以你的身份读你的消息、发消息。
所以：
· `.env` 已经在 .gitignore 里，别提交、别贴给任何人（包括贴给我）；
· 不用了就去 Telegram 的「设置 → 隐私和安全 → 已登录设备」把它踢掉；
· 泄露了立刻踢设备 + 重新生成。

## 顺带

登录成功后脚本会把你**订阅的频道列表**打印出来（名字 + @用户名 + id），
直接从里面挑要搬运的那几个，不用你自己去翻。
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    print("缺 telethon，先装：pip install telethon")
    raise SystemExit(1)


async def main():
    print("=" * 62)
    print("Telegram 账号登录（一次性，换 session 用）")
    print("api_id / api_hash 去 https://my.telegram.org 申请")
    print("=" * 62)
    api_id = input("api_id（数字）: ").strip()
    api_hash = input("api_hash: ").strip()
    if not api_id.isdigit() or len(api_hash) < 16:
        print("api_id 要是纯数字，api_hash 是一长串字母数字。重新跑一次。")
        return

    async with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        me = await client.get_me()
        print(f"\n✅ 登录成功：{me.first_name or ''} @{me.username or '(无用户名)'}"
              f" id={me.id}\n")

        print("你订阅的频道（挑要搬运的，用 @用户名 或 id）：")
        print("-" * 62)
        n = 0
        async for d in client.iter_dialogs():
            e = d.entity
            if not getattr(e, "broadcast", False):    # 只列频道，不列群和私聊
                continue
            n += 1
            uname = getattr(e, "username", None)
            tag = f"@{uname}" if uname else "（私密频道，只能用 id）"
            print(f"  {d.name[:34]:<36} {tag:<26} id={e.id}")
        print("-" * 62)
        print(f"共 {n} 个频道\n")

        print("=" * 62)
        print("把下面三行填进服务器的 .env，然后 docker compose up -d --force-recreate")
        print("=" * 62)
        print(f"TG_API_ID={api_id}")
        print(f"TG_API_HASH={api_hash}")
        print(f"TG_SESSION={client.session.save()}")
        print("=" * 62)
        print("⚠️ 这串 session 等于你的账号凭证，别提交别外发。")
        print("   不用了去「设置 → 隐私和安全 → 已登录设备」踢掉。")


asyncio.run(main())
