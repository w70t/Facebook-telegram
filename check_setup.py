"""
سكربت تحقق سريع: يتأكد أن الإعدادات صحيحة قبل التشغيل الكامل.
    python check_setup.py
"""
import asyncio
import sys

OK = "✅"
BAD = "❌"


def check_env():
    print("— فحص ملف .env —")
    try:
        import config
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} مشكلة في .env: {e}")
        return None

    placeholder = lambda v: isinstance(v, str) and v.startswith("<")
    problems = []
    for name in ("API_ID", "API_HASH", "BOT_TOKEN", "FB_PAGE_ID", "FB_PAGE_TOKEN"):
        if placeholder(getattr(config, name)):
            problems.append(name)
    if any(placeholder(c) for c in config.SOURCE_CHANNELS):
        problems.append("SOURCE_CHANNELS")

    if problems:
        print(f"{BAD} قيم لم تُعبّأ بعد: {', '.join(problems)}")
    else:
        print(f"{OK} كل القيم المطلوبة معبّأة")
    return config


async def check_telegram(config):
    print("\n— فحص اتصال تلغرام —")
    from telethon import TelegramClient

    bot = TelegramClient("bot_session", config.API_ID, config.API_HASH)
    try:
        await bot.start(bot_token=config.BOT_TOKEN)
        me = await bot.get_me()
        print(f"{OK} البوت متصل: @{me.username}")
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} فشل اتصال البوت: {e}")
    finally:
        await bot.disconnect()

    user = TelegramClient("user_session", config.API_ID, config.API_HASH)
    try:
        await user.connect()
        if await user.is_user_authorized():
            me = await user.get_me()
            print(f"{OK} الحساب الشخصي مسجّل الدخول: {me.first_name} (id {me.id})")
        else:
            print(f"{BAD} الحساب الشخصي غير مسجّل — شغّل: python main.py مرة واحدة")
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} فشل اتصال الحساب الشخصي: {e}")
    finally:
        await user.disconnect()


def check_facebook(config):
    print("\n— فحص توكن فيسبوك —")
    import requests

    try:
        r = requests.get(
            f"https://graph.facebook.com/v19.0/{config.FB_PAGE_ID}",
            params={"fields": "name", "access_token": config.FB_PAGE_TOKEN},
            timeout=30,
        )
        data = r.json()
        if "error" in data:
            print(f"{BAD} خطأ فيسبوك: {data['error'].get('message')}")
        else:
            print(f"{OK} الصفحة جاهزة للنشر: {data.get('name')}")
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} فشل الاتصال بفيسبوك: {e}")


async def main():
    config = check_env()
    if config is None:
        sys.exit(1)
    await check_telegram(config)
    check_facebook(config)
    print("\nانتهى الفحص.")


if __name__ == "__main__":
    asyncio.run(main())
