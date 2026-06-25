"""Built-in web monitor / advisor dashboard.

In advisor mode the bot does not trade by itself — this page is the cockpit:
  * 15 ranked buy opportunities, each rated 🟢..🟢🟢🟢 by win probability.
  * Live price per symbol (refreshes every couple of seconds) with an up/down
    arrow and a flash on every tick, plus the last-hour low–high range.
  * An amount box + Buy / Sell buttons that place REAL market orders.

Routes:
  /           -> dashboard page
  /bot.json   -> status snapshot (recommendations, positions, regime)
  /prices     -> live prices + 1h high/low (fast, polled by the page)
  /logs       -> recent log lines
  /buy  POST  -> queue a real market BUY  (symbol, amount, token)
  /sell POST  -> queue a real market SELL (symbol, token)
  /health     -> "ok"

Only non-sensitive status is exposed — never API keys. The Buy/Sell actions are
guarded by MONITOR_TOKEN (set it — these spend real money).
"""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT_JSON = HERE.parent / "docs" / "stocks" / "data" / "bot.json"

LOGS = deque(maxlen=400)

_LOCK = threading.Lock()
_SELL_REQUESTS = set()
_BUY_REQUESTS = []          # list of (symbol, amount)
_LIVE = {}                  # symbol -> {price, high, low, changePct}
MONITOR_TOKEN = (os.environ.get("MONITOR_TOKEN", "") or "").strip()


def add_log(line):
    LOGS.append(line)


def set_live(data):
    """Called by the bot's price loop with fresh {symbol: {...}} stats."""
    global _LIVE
    with _LOCK:
        _LIVE = dict(data)


def request_sell(symbol):
    with _LOCK:
        _SELL_REQUESTS.add(symbol.upper())


def drain_sell_requests():
    with _LOCK:
        out = list(_SELL_REQUESTS)
        _SELL_REQUESTS.clear()
    return out


def request_buy(symbol, amount):
    with _LOCK:
        _BUY_REQUESTS.append((symbol.upper(), amount))


def drain_buy_requests():
    with _LOCK:
        out = list(_BUY_REQUESTS)
        _BUY_REQUESTS.clear()
    return out


PAGE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🧭 مستشار الأسهم (bStocks)</title><style>
:root{--bg:#0b0f1a;--card:#151c2c;--bd:#243049;--tx:#e8edf6;--mut:#93a0bd;--up:#16c784;--dn:#ea3943;--ac:#f7931a}
*{box-sizing:border-box;margin:0}body{background:var(--bg);color:var(--tx);font-family:-apple-system,Segoe UI,Tahoma,Arial,sans-serif;padding:14px;max-width:1040px;margin:auto;line-height:1.6}
h1{font-size:1.25rem}h2{font-size:1rem;color:var(--ac);margin:18px 0 8px}
.pill{font-size:.78rem;padding:3px 10px;border-radius:999px;border:1px solid var(--bd)}
.live{background:var(--dn);color:#fff;border-color:var(--dn)}.dryrun{background:var(--up);color:#fff}.testnet{background:#5b8def;color:#fff}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
input{background:#0a0e17;border:1px solid var(--bd);border-radius:8px;color:var(--tx);padding:7px 10px;font-size:.85rem}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:12px;overflow:hidden}
th,td{padding:8px 9px;text-align:center;border-bottom:1px solid var(--bd);font-size:.84rem;white-space:nowrap}th{background:#1b2436;color:var(--mut)}
td.sym{text-align:right;font-weight:700}
.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut)}.scroll{overflow-x:auto}
.b{border:0;border-radius:8px;padding:5px 12px;font-size:.8rem;cursor:pointer;font-weight:700}
.buy{background:var(--up);color:#04230f}.sell{background:var(--dn);color:#fff}.b:hover{opacity:.85}
tr.r3{border-right:4px solid var(--up)}tr.r2{border-right:4px solid #8bd34f}tr.r1{border-right:4px solid #5b8def}
.flash{animation:fl .6s}@keyframes fl{0%{background:#1f3a2a}100%{background:transparent}}
.flashd{animation:fld .6s}@keyframes fld{0%{background:#3a1f24}100%{background:transparent}}
pre{background:#0a0e17;border:1px solid var(--bd);border-radius:12px;padding:12px;font-size:.76rem;max-height:240px;overflow:auto;direction:ltr;text-align:left;white-space:pre-wrap}
small{color:var(--mut)}
</style></head><body>
<h1>🧭 مستشار الأسهم <span id="mode" class="pill">…</span></h1>
<div id="warn" style="color:var(--ac);margin:6px 0"></div>
<div class="bar">
<span>المبلغ (USDT):</span><input id="amt" type="number" min="1" step="1" value="12" style="width:110px">
<input id="tok" placeholder="رمز الحماية" style="width:140px">
<small>المبلغ وزر الشراء/البيع ينفّذان أمراً حقيقياً على Binance</small>
</div>
<div class="bar" id="stats"></div>

<h2>🎯 أفضل 15 فرصة شراء (الأخضر = فرصة ربح أعلى)</h2>
<div class="scroll"><table id="rec"><thead><tr>
<th>السهم</th><th>التقييم</th><th>السعر الآن</th><th>تغيّر ساعة</th><th>أدنى–أعلى (ساعة)</th><th>فرصة الربح</th><th>شراء</th></tr></thead><tbody></tbody></table></div>

<h2>صفقاتي المفتوحة</h2>
<div class="scroll"><table id="pos"><thead><tr>
<th>السهم</th><th>دخول</th><th>السعر الآن</th><th>ربح%</th><th>بيع</th></tr></thead><tbody></tbody></table></div>

<h2>السجلّ المباشر</h2><pre id="logs">…</pre>
<script>
const $=i=>document.getElementById(i),f=(n,d=2)=>n==null||isNaN(n)?'—':(+n).toLocaleString('en',{maximumFractionDigits:d});
const sg=(v,d=2)=>v==null||isNaN(v)?'—':((v>0?'+':'')+f(v,d)),cl=v=>v>0?'up':v<0?'dn':'';
const stars=r=>'🟢'.repeat(r||1);
let REC=[],POS=[],LIVE={},LAST={};
function amt(){return Math.max(1,+($('amt').value||0))}
function tok(){return $('tok').value.trim()}
async function buy(s){if(!confirm('شراء '+s+' بمبلغ '+amt()+' USDT الآن؟'))return;
 const r=await fetch('/buy?symbol='+encodeURIComponent(s)+'&amount='+amt()+'&token='+encodeURIComponent(tok()),{method:'POST'});
 const j=await r.json().catch(()=>({}));alert(r.ok&&j.ok?'📨 أُرسل أمر شراء '+s+' — تابع السجلّ':'❌ '+(j.error||'فشل (رمز الحماية؟)'));}
async function sell(s){if(!confirm('بيع '+s+' الآن؟'))return;
 const r=await fetch('/sell?symbol='+encodeURIComponent(s)+'&token='+encodeURIComponent(tok()),{method:'POST'});
 const j=await r.json().catch(()=>({}));alert(r.ok&&j.ok?'📨 أُرسل أمر بيع '+s:'❌ '+(j.error||'فشل (رمز الحماية؟)'));}
function priceCell(s){const L=LIVE[s];if(!L)return '<span class=mut>…</span>';
 const prev=LAST[s];let arrow='',cls='';if(prev!=null){if(L.price>prev){arrow='▲';cls='up'}else if(L.price<prev){arrow='▼';cls='dn'}}
 return '<span class="'+cls+'" id="p_'+s+'">'+f(L.price,4)+' '+arrow+'</span>';}
function rng(s){const L=LIVE[s];return L?('<span class=dn>'+f(L.low,4)+'</span> – <span class=up>'+f(L.high,4)+'</span>'):'—';}
function chg(s){const L=LIVE[s];return L?'<span class="'+cl(L.changePct)+'">'+sg(L.changePct)+'%</span>':'—';}
function render(){
 let b=$('rec').querySelector('tbody');
 b.innerHTML=REC.map(x=>`<tr class="r${x.rating}"><td class=sym>${x.ticker||x.symbol}</td><td>${stars(x.rating)}</td><td id="rc_${x.symbol}">${priceCell(x.symbol)}</td><td>${chg(x.symbol)}</td><td>${rng(x.symbol)}</td><td>${x.win_prob==null?('عائد '+sg(x.ret_3m)+'%'):((x.win_prob*100|0)+'%')}</td><td><button class="b buy" onclick="buy('${x.symbol}')">شراء</button></td></tr>`).join('')||'<tr><td colspan=7 class=mut>يحسب الترشيحات…</td></tr>';
 b=$('pos').querySelector('tbody');
 b.innerHTML=POS.map(p=>{const L=LIVE[p.symbol];const now=L?L.price:p.price;const pl=p.entry_price?((now/p.entry_price-1)*100):0;
  return `<tr><td class=sym>${p.symbol}</td><td>${f(p.entry_price,4)}</td><td>${priceCell(p.symbol)}</td><td class=${cl(pl)}>${sg(pl)}%</td><td><button class="b sell" onclick="sell('${p.symbol}')">بيع</button></td></tr>`}).join('')||'<tr><td colspan=5 class=mut>لا صفقات</td></tr>';
 for(const s in LIVE)LAST[s]=LIVE[s].price;
}
async function pull(){try{const d=await(await fetch('/bot.json?t='+Date.now())).json();
 const m=$('mode');m.textContent=({dryrun:'محاكاة',testnet:'تجريبي',live:'حقيقي'}[d.mode]||d.mode||'—');m.className='pill '+(d.mode||'');
 $('warn').textContent=d.mode==='live'?'⚠️ الأزرار تنفّذ أوامر بأموال حقيقية':'';
 REC=d.recommendations||[];POS=d.positions||[];const R=d.regime||{};const ac=d.account||{};
 $('stats').innerHTML=`<span class=pill>USDT متاح: ${ac.free_usdt!=null?f(ac.free_usdt):'—'}</span> <span class=pill>المحفظة: ${f(ac.total_usdt!=null?ac.total_usdt:d.equity_quote)}</span> <span class=pill>VIX: ${R.fear_greed==null?'—':R.fear_greed+' '+(R.fear_greed_label||'')}</span>`;
 render();}catch(e){}}
async function ticks(){try{LIVE=await(await fetch('/prices?t='+Date.now())).json()||{};render();}catch(e){}}
async function logs(){try{$('logs').textContent=await(await fetch('/logs?t='+Date.now())).text()}catch(e){}}
pull();ticks();logs();setInterval(pull,5000);setInterval(ticks,2000);setInterval(logs,5000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/bot.json":
            try:
                self._send(200, BOT_JSON.read_text(encoding="utf-8"),
                           "application/json; charset=utf-8")
            except Exception:
                self._send(200, "{}", "application/json")
        elif path == "/prices":
            with _LOCK:
                body = json.dumps(_LIVE)
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/logs":
            self._send(200, "\n".join(LOGS), "text/plain; charset=utf-8")
        elif path == "/health":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def _auth(self, params):
        token = params.get("token", [""])[0]
        return not MONITOR_TOKEN or token == MONITOR_TOKEN

    def do_POST(self):
        path, _, q = self.path.partition("?")
        params = urllib.parse.parse_qs(q)
        if path in ("/buy", "/sell"):
            if not self._auth(params):
                self._send(403, json.dumps({"ok": False, "error": "رمز غير صحيح"}),
                           "application/json; charset=utf-8")
                return
            symbol = (params.get("symbol", [""])[0] or "").upper()
            if not symbol:
                self._send(400, json.dumps({"ok": False, "error": "no symbol"}),
                           "application/json")
                return
            if path == "/buy":
                try:
                    amount = float(params.get("amount", ["0"])[0])
                except ValueError:
                    amount = 0.0
                if amount < 1:
                    self._send(400, json.dumps({"ok": False, "error": "مبلغ غير صالح"}),
                               "application/json; charset=utf-8")
                    return
                request_buy(symbol, amount)
            else:
                request_sell(symbol)
            self._send(200, json.dumps({"ok": True, "symbol": symbol}),
                       "application/json; charset=utf-8")
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}),
                       "application/json")

    def log_message(self, *args):
        pass


def start(port):
    srv = ThreadingHTTPServer(("0.0.0.0", int(port)), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
