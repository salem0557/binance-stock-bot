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

SalemGrid (بوت شبكة وهمي)
================================
يشتغل تلقائياً مع وضع dryrun جنب البوت الأساسي، بأسعار Binance الحقيقية وفلوس وهمية (200 USDT).
ما يحتاج مفتاح Binance. يرسل كل عملية شراء وبيع على تيليجرام، وملخص يومي.
السجل محفوظ في /data/grid_fills.csv

متغيرات اختيارية في Railway:
  GRID = off               يوقف بوت الشبكة
  GRID_PAIRS = BTC/USDT,ETH/USDT
  GRID_CAPITAL = 200
  GRID_LEVELS = 16
  GRID_RANGE = 0.15        النطاق +-15%
  GRID_TRAIL = 0.005       (اختياري) لا يبيع وهو طالع، ينتظر نزول 0.5% من القمة
  GRID_CRASH = 0.05        (اختياري) يوقف الشراء إذا نزل السعر 5% خلال 4 ساعات
