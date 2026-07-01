# Facebook ⇄ Telegram — بوت نسخ ونشر مع مراجعة

ينسخ المنشورات من **قناة تلغرام** (حتى لو لست أدمن فيها) عبر **حسابك الشخصي**،
ثم يرسلها إلى **بوت مراجعة** فيه أزرار، وبعد موافقة الأدمن يُنشر على **صفحة فيسبوك**.
مصمّم ليعمل 24/7 على **Raspberry Pi 5**.

```
قناة تلغرام ─▶ حسابك الشخصي (Telethon) ─▶ بوت المراجعة (أزرار) ─▶ موافقة الأدمن ─▶ صفحة فيسبوك
```

> 📘 **للإعداد خطوة بخطوة (ربط حسابك الشخصي + صفحة فيسبوك):** افتح **[SETUP_AR.md](SETUP_AR.md)**.

## المميزات
- يقرأ من أي قناة عبر حساب شخصي (لا يحتاج أن تكون أدمن في القناة).
- **أكثر من قناة مصدر**، تُدار من داخل البوت: `/addsource` و `/delsource` و `/sources`.
- يدعم النص + الصور + الفيديو.
- أزرار مراجعة: **✅ نشر** / **📄 نشر النص فقط** / **✏️ تعديل النص** / **❌ تجاهل**.
- صلاحيات: فقط الأدمنون في `ADMIN_IDS` يقدرون يضغطون الأزرار.

## أوامر البوت
| الأمر | الوظيفة |
|------|---------|
| `/sources` | عرض القنوات المصدر الحالية |
| `/addsource @قناة` | إضافة قناة مصدر (لازم حسابك عضو فيها) |
| `/delsource @قناة` | حذف قناة مصدر |
| `/id` | معرفة معرّف المحادثة ومعرّفك (للإعداد) |

## ما تحتاجه قبل البدء
1. **Telegram API**: من <https://my.telegram.org> → API development tools → `API_ID` و `API_HASH`.
2. **بوت مراجعة**: من [@BotFather](https://t.me/BotFather) → `BOT_TOKEN`.
3. **قروب مراجعة**: أنشئ قروب، أضف الأدمنين والبوت، وارفع البوت أدمن في القروب.
4. **Facebook**: صفحتك + **Page Access Token** طويل الأمد (انظر الأسفل) + `FB_PAGE_ID`.

## التثبيت (Raspberry Pi 5 / أي لينكس)
```bash
git clone <repo-url> Facebook-telegram
cd Facebook-telegram
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env          # عبّئ القيم
```

### معرفة معرّفات المحادثة والأدمنين
شغّل البوت مرة، وفي قروب المراجعة أرسل `/id` — سيعطيك `chat id` (للـ `REVIEW_CHAT_ID`)
و `your id` (لكل أدمن تضيفه في `ADMIN_IDS`).

### أول تشغيل (تسجيل دخول الحساب الشخصي)
شغّله **يدوياً أول مرة** لأنه سيطلب رقم الهاتف ورمز التحقق لإنشاء ملف الجلسة:
```bash
source venv/bin/activate
python main.py
```
بعد إنشاء `user_session.session` ما راح يطلب تسجيل دخول مرة ثانية.

## التشغيل الدائم كخدمة على الـ Pi
```bash
sudo cp deploy/telegram-fb-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/telegram-fb-bot.service   # عدّل User و المسارات
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-fb-bot
sudo systemctl status telegram-fb-bot
journalctl -u telegram-fb-bot -f                         # متابعة السجل
```

## كيف تحصل على Page Access Token طويل الأمد (مختصر)
1. أنشئ تطبيقاً في <https://developers.facebook.com>.
2. من **Graph API Explorer** اختر صفحتك وفعّل صلاحيتي `pages_manage_posts` و `pages_read_engagement`.
3. ولّد **User Token** قصير، ثم بدّله إلى **long-lived user token**، ثم اطلب
   `/me/accounts` لتحصل على **Page Access Token** الدائم للصفحة.
4. ضع المعرّف في `FB_PAGE_ID` والتوكن في `FB_PAGE_TOKEN`.

> ملاحظة: بما أنها صفحتك وأنت أدمن فيها، تقدر تنشر في وضع التطوير بدون مراجعة معقّدة من فيسبوك.

## تنبيهات مهمة
- ⚠️ **شروط تلغرام**: أتمتة حساب شخصي (Userbot) مخالفة نظرياً لشروط تلغرام وقد تؤدي لحظر الحساب.
  استخدم **رقماً/حساباً ثانوياً** وليس حسابك الرئيسي، ولا تنسخ بسرعة مفرطة.
- ⚠️ **حقوق المحتوى**: أنت تنشر محتوى شخص آخر — احترم حقوق النشر وأضف مصدراً عند اللزوم.
- لا ترفع ملف `.env` ولا ملفات `*.session` إلى GitHub (مستثناة في `.gitignore`).

## الملفات
| الملف | الوظيفة |
|------|---------|
| `main.py` | المنطق الرئيسي (قراءة، مراجعة بالأزرار، نشر) |
| `facebook.py` | النشر على فيسبوك عبر Graph API |
| `config.py` | تحميل الإعدادات من `.env` |
| `.env.example` | قالب الإعدادات |
| `deploy/telegram-fb-bot.service` | خدمة systemd للتشغيل الدائم |
