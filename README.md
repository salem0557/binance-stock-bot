SalemBot - Freqtrade on Railway
================================

خطوات التشغيل:

1) GitHub: سوّ مستودع خاص (Private) باسم salem-bot
   وأضف الملفات الأربعة تحت بنفس المسارات بالضبط
   (Add file > Create new file، واكتب المسار كامل مثل user_data/strategies/SalemTrend.py)

2) Railway: سجّل بحساب GitHub > New Project > Deploy from GitHub repo > salem-bot

3) Settings > Region: اختر EU West (Amsterdam)
   لا تختار أمريكا، لأن Binance يحجب السيرفرات الأمريكية

4) أضف Volume وخلّ مساره: /data

5) Variables (المتغيرات) حسب المرحلة:

   المرحلة 1 - اختبار تاريخي:
     MODE = backtest
   النتيجة تطلع في Deploy Logs. صوّرها وأرسلها لي.

   المرحلة 2 - تداول وهمي (أسبوعين) - هذا الوضع الافتراضي لو ما حطيت MODE:
     MODE = dryrun
     TG_TOKEN = توكن البوت من @BotFather
     TG_CHAT_ID = رقمك من @userinfobot

   المرحلة 3 - تداول حقيقي:
     MODE = live
     BINANCE_KEY = مفتاح API
     BINANCE_SECRET = المفتاح السري
   مفتاح Binance: فعّل Spot Trading فقط، واقفل السحب (Withdrawals)

مهم: البوت يتداول بكل رصيد USDT الموجود في الحساب.
حط في الحساب (أو حساب فرعي) المبلغ اللي تبي البوت يتداول فيه بس.

أوامر تيليجرام:
  /status   الصفقات المفتوحة
  /profit   الأرباح
  /stopentry  يوقف الدخول في صفقات جديدة
  /forceexit all  يقفل كل الصفقات
  /stop     يوقف البوت

SalemGrid (بوت شبكة سريع - وهمي)
================================
يشتغل تلقائياً مع وضع dryrun جنب البوت الأساسي، بأسعار Binance الحقيقية وفلوس وهمية (200 USDT).
الهدف: 20 صفقة أو أكثر باليوم. ما يحتاج مفتاح Binance.
الإعدادات في grid/params.json، والمراجعة اليومية (grid/daily_review.py) تعدلها كل ليلة
وتكتب وش صار ووش تغير في grid/journal.md
تيليجرام: ملخص آخر كل يوم + تنبيه عند وقف الخسارة (GRID_TG_TRADES = on يرسل كل صفقة)
GRID = off يوقف بوت الشبكة. السجل الكامل في /data/grid_fills.csv
