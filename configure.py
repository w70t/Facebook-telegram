"""
معالج تشغيل لمرة واحدة: يأخذ القيم الأساسية الثلاث فقط ويحفظها في settings.json.
كل شيء آخر (رقم الهاتف، فيسبوك، القنوات، الأدمنون) يُضبط لاحقاً من داخل تلغرام.

    python configure.py

(كان اسمه setup.py — غُيّر لأن setuptools يعامل setup.py معاملة خاصة، فكان
 أي `pip install .` يشغّل هذا المعالج التفاعلي بالخطأ.)
"""
from settings import Settings


def ask(prompt, current=None):
    suffix = f" [{current}]" if current else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or current


def main():
    s = Settings()
    print("=== إعداد أولي لمرة واحدة ===")
    print("احصل على api_id و api_hash من: https://my.telegram.org")
    print("واحصل على توكن البوت من: @BotFather\n")

    api_id = ask("API_ID", s.get("api_id"))
    api_hash = ask("API_HASH", s.get("api_hash"))
    bot_token = ask("BOT_TOKEN", s.get("bot_token"))

    if not (api_id and api_hash and bot_token):
        print("\n❌ لازم تعبّي القيم الثلاث. أعد المحاولة.")
        return

    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        print("\n❌ API_ID لازم يكون رقماً.")
        return

    s.data["api_id"] = api_id
    s.data["api_hash"] = api_hash
    s.data["bot_token"] = bot_token
    s.save()

    print(f"\n✅ تم الحفظ في {s.path} (صلاحيات 600 — قراءة المالك فقط)")
    print("الآن شغّل البوت:  python main.py")
    print("ثم أرسل /start للبوت في تلغرام وأكمل الإعداد من هناك.")


if __name__ == "__main__":
    main()
