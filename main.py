"""
shopaking - food ordering site (Flask). Settings live in config.py.
Run: pip install flask  ->  python main.py
"""
import os, sys, json, time, hmac, math, secrets, threading, html, urllib.request
from datetime import datetime, timezone
from flask import (Flask, request, session, redirect, render_template_string,
                   jsonify, abort, send_from_directory)

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import config as _cfg


def cfg(k, d=""):
    """Environment variable wins over config.py (use env vars on Vercel for passwords)."""
    v = os.environ.get(k)
    return v if v not in (None, "") else getattr(_cfg, k, d)


SITE = cfg("SITE_NAME", "shopaking")
CUR = cfg("CURRENCY", "Rs")
D_BASE = float(cfg("DELIVERY_BASE_FEE", 50))
D_KM = float(cfg("DELIVERY_PER_KM", 50))
D_MAX = float(cfg("MAX_DELIVERY_KM", 25))
ROAD = float(cfg("ROAD_FACTOR", 1.3))
S_LAT, S_LNG = float(cfg("STORE_LAT", 0) or 0), float(cfg("STORE_LNG", 0) or 0)
ADMIN_PASSWORD = cfg("ADMIN_PASSWORD", "changeme123")
RIDER_PASSWORD = str(cfg("RIDER_PASSWORD", ""))
RIDER_NAME = cfg("RIDER_NAME", "Your rider")
RIDER_PHONE = "".join(c for c in str(cfg("RIDER_PHONE", "")) if c.isdigit() or c == "+")

# ---- storage: local files by default; on Vercel use Upstash Redis (data) + Vercel Blob (uploads)
ON_VERCEL = bool(os.environ.get("VERCEL"))
KV_URL = (os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
USE_KV = bool(KV_URL and KV_TOKEN)
BLOB_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
TMP = "/tmp" if ON_VERCEL else BASE          # Vercel's disk is read-only except /tmp (and /tmp is temporary)
DATA_FILE = os.path.join(TMP, "data.json")
UPLOAD_DIR = os.path.join(TMP, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
if ON_VERCEL and not USE_KV:
    print("WARNING: no Redis database connected - orders and menu will NOT be saved between requests.")


def kv(*cmd):
    req = urllib.request.Request(KV_URL, data=json.dumps(list(cmd)).encode(), headers={
        "Authorization": "Bearer " + KV_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r).get("result")


class _Lock:
    """Same as threading.Lock locally; a short Redis lock when running on several serverless instances."""
    def __init__(self):
        self.t = threading.Lock()
        self.tok = None

    def __enter__(self):
        self.t.acquire()
        if USE_KV:
            self.tok = secrets.token_hex(8)
            start = time.time()
            try:
                while kv("SET", "shop_lock", self.tok, "NX", "EX", 15) is None:
                    if time.time() - start > 10:
                        break
                    time.sleep(0.1)
            except Exception as e:
                print("lock error:", e)

    def __exit__(self, *a):
        try:
            if USE_KV and kv("GET", "shop_lock") == self.tok:
                kv("DEL", "shop_lock")
        except Exception as e:
            print("unlock error:", e)
        finally:
            self.t.release()


LOCK = _Lock()

DEFAULT = {
    "open": True, "next_item": 6, "next_order": 1,
    "items": [
        {"id": 1, "name": "Regular Fries", "price": 60, "emoji": "🍟", "desc": "Classic, golden and salted.", "stock": True, "soon": False, "image": ""},
        {"id": 2, "name": "Special Fries", "price": 120, "emoji": "🍟", "desc": "Our loaded house special.", "stock": True, "soon": False, "image": ""},
        {"id": 3, "name": "Masala Challenge Fries", "price": 450, "emoji": "🌶️", "desc": "Brutally spicy. Finish the box to win.", "stock": True, "soon": False, "image": ""},
        {"id": 4, "name": "Flavor Fries", "price": 150, "emoji": "🧂", "desc": "Fries tossed in your choice of flavor dust.", "stock": True, "soon": False, "image": ""},
        {"id": 5, "name": "Shawarma", "price": 250, "emoji": "🌯", "desc": "Wrapped hot, sauced generously.", "stock": True, "soon": False, "image": ""},
    ],
    "orders": [], "tickets": [], "secret": secrets.token_hex(32),
}


def load():
    if USE_KV:
        raw = kv("GET", "shop_data")
        if not raw:
            kv("SET", "shop_data", json.dumps(DEFAULT, ensure_ascii=False), "NX")
            raw = kv("GET", "shop_data")
        d = json.loads(raw)
        d.setdefault("tickets", [])
        return d
    if not os.path.exists(DATA_FILE):
        save(DEFAULT)
        return json.loads(json.dumps(DEFAULT))
    with open(DATA_FILE, encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("tickets", [])
    return d


def save(d):
    if USE_KV:
        d["orders"] = d["orders"][-500:]      # keep the stored data small
        kv("SET", "shop_data", json.dumps(d, ensure_ascii=False))
        return
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


app = Flask(__name__)
app.secret_key = cfg("SECRET_KEY") or load()["secret"]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=ON_VERCEL,
                  MAX_CONTENT_LENGTH=6 * 1024 * 1024)

# ---------------------------------------------------------------- helpers
def is_admin():
    return session.get("admin") is True


def csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST" and request.path.startswith(("/admin", "/rider")):
        tok = request.headers.get("X-CSRF") or request.form.get("csrf", "")
        if not hmac.compare_digest(tok.encode(), session.get("csrf", "-").encode()):
            abort(400)


@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    return r


def ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr) or "?").split(",")[0].strip()


def clean(s, n):
    return " ".join(str(s or "").split())[:n]


def hav(a, b, c, d):
    p1, p2 = math.radians(a), math.radians(c)
    x = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(x))


def post_webhook(url, payload):
    if not url:
        return

    def run():
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
                "Content-Type": "application/json", "User-Agent": f"DiscordBot ({SITE}, 1.0)"})
            urllib.request.urlopen(req, timeout=6).read()
        except Exception as e:
            print("webhook error:", e)
    if ON_VERCEL:
        run()          # serverless freezes background threads once the response is sent
    else:
        threading.Thread(target=run, daemon=True).start()


def embed(title, fields, color):
    return {"username": SITE, "allowed_mentions": {"parse": []}, "embeds": [{
        "title": title, "color": color, "footer": {"text": SITE},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [{"name": n, "value": (str(v) or "-")[:1000], "inline": i} for n, v, i in fields]}]}


def order_payload(o):
    items = "\n".join(f"{i['qty']}x {i['name']} - {CUR} {i['price'] * i['qty']}" for i in o["items"])
    kind = "Pickup" if o["mode"] == "pickup" else f"Delivery ({o['km']} km)"
    f = [("Customer", f"{o['name']}\n{o['phone']}", True), ("Type", kind, True),
         ("Address", o["address"] or "-", False), ("Items", items, False),
         ("Subtotal", f"{CUR} {o['subtotal']}", True), ("Delivery", f"{CUR} {o['fee']}", True),
         ("TOTAL", f"**{CUR} {o['total']}**", True)]
    if o["note"]:
        f.append(("Note", o["note"], False))
    if o.get("lat") is not None:
        f.append(("Map", f"https://maps.google.com/?q={o['lat']},{o['lng']}", False))
    return embed(f"New order #{o['id']}", f, 0xFFC62B)


def up_url(name):
    return name if str(name).startswith("http") else "/uploads/" + name


app.add_template_global(up_url, "up_url")


def store_file(name, raw):
    """Save an upload. Returns the name (local disk) or a full URL (Vercel Blob)."""
    if BLOB_TOKEN:
        ctype = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "webm": "audio/webm",
                 "ogg": "audio/ogg", "m4a": "audio/mp4", "mp3": "audio/mpeg", "wav": "audio/wav"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        req = urllib.request.Request("https://blob.vercel-storage.com/shop/" + name, data=raw, method="PUT", headers={
            "authorization": "Bearer " + BLOB_TOKEN, "x-api-version": "7", "access": "public",
            "x-content-type": ctype, "x-add-random-suffix": "0", "x-cache-control-max-age": "31536000"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)["url"]
        except Exception as e:
            print("blob upload error:", e)
            raise ValueError("Upload failed. Try again.")
    if ON_VERCEL:
        raise ValueError("Uploads are not set up yet (connect a Vercel Blob store).")
    with open(os.path.join(UPLOAD_DIR, name), "wb") as out:
        out.write(raw)
    return name


def save_image(f):
    if not f or not f.filename:
        raise ValueError("Choose an image first.")
    raw = f.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Image is over 4 MB.")
    if raw.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif raw[:4] == b"GIF8":
        ext = ".gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = ".webp"
    else:
        raise ValueError("Use a JPG, PNG, WEBP or GIF image.")
    return store_file(secrets.token_hex(10) + ext, raw)


def drop_image(name):
    if not name:
        return
    if name.startswith("http"):
        if BLOB_TOKEN:
            try:
                req = urllib.request.Request("https://blob.vercel-storage.com/delete", data=json.dumps({"urls": [name]}).encode(),
                                             method="POST", headers={"authorization": "Bearer " + BLOB_TOKEN, "x-api-version": "7",
                                                                     "content-type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
            except Exception as e:
                print("blob delete error:", e)
        return
    p = os.path.join(UPLOAD_DIR, os.path.basename(name))
    if os.path.isfile(p):
        os.remove(p)


def pub(it):
    return {"id": it["id"], "name": it["name"], "price": it["price"], "emoji": it.get("emoji", "🍽️"),
            "desc": it.get("desc", ""), "stock": it.get("stock", True), "soon": it.get("soon", False),
            "image": up_url(it["image"]) if it.get("image") else ""}


def public_cfg():
    return {"site": SITE, "cur": CUR, "base": D_BASE, "perKm": D_KM, "maxKm": D_MAX, "road": ROAD,
            "storeLat": S_LAT, "storeLng": S_LNG, "tagline": cfg("TAGLINE"),
            "wa": "".join(c for c in str(cfg("SUPPORT_WHATSAPP")) if c.isdigit()),
            "phone": cfg("SUPPORT_PHONE"), "email": cfg("SUPPORT_EMAIL"), "hours": cfg("SUPPORT_HOURS"),
            "riderName": RIDER_NAME, "riderPhone": RIDER_PHONE}

# ---------------------------------------------------------------- shared css
BASE_CSS = r"""
:root{--ink:#1b1340;--mut:#6b6591;--royal:#4b2bd6;--royal2:#6a4cff;--sun:#ffc933;--mist:#f4f1ff;--card:#fff;--line:#e6e1fb;--ok:#12b76a;--bad:#f04438;
--spring:cubic-bezier(.34,1.56,.64,1);--out:cubic-bezier(.22,1,.36,1);--disp:'Unbounded','Arial Black',system-ui,sans-serif;
--red:var(--royal);--gold:var(--sun);--deep:#2a1a8f;--paper:var(--mist);--ease:var(--spring)}
*{box-sizing:border-box;margin:0}
[hidden]{display:none!important}
body{font-family:'Figtree',system-ui,sans-serif;color:var(--ink);background:var(--mist);min-height:100vh;overflow-x:hidden;-webkit-tap-highlight-color:transparent}
h1,h2,h3,.disp{font-family:var(--disp);font-weight:600;letter-spacing:-.02em}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none}
:focus-visible{outline:3px solid var(--royal2);outline-offset:2px}
.btn{background:var(--sun);color:var(--ink);padding:.8rem 1.3rem;border-radius:16px;font-weight:800;box-shadow:0 10px 22px -10px #ffb400;transition:transform .25s var(--spring)}
.btn:hover{transform:translateY(-2px)}.btn:active{transform:scale(.97)}.btn[disabled]{opacity:.5;pointer-events:none}
.mini{background:var(--sun);padding:.45rem .8rem;border-radius:10px;font-weight:700;color:var(--ink)}
.mini.red{background:#ffe1de;color:#a3190f}
.rain{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden}
.rain span{position:absolute;top:-60px;opacity:.12;animation:fall linear infinite}
@keyframes fall{to{transform:translateY(115vh) rotate(var(--r))}}
.bg-move{position:fixed;inset:-20%;z-index:0;pointer-events:none;background:radial-gradient(40% 40% at 20% 20%,#6a4cff33,transparent),radial-gradient(35% 35% at 80% 70%,#ffc93344,transparent)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""
FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700&family=Figtree:wght@400;500;700;800&display=swap" rel="stylesheet">'
RAIN_JS = """
(function(){const r=document.querySelector('.rain');if(!r)return;const e=['🍟','🍟','🌯','🌶️','🧂'];
for(let i=0;i<12;i++){const s=document.createElement('span');s.textContent=e[i%e.length];
s.style.left=Math.random()*100+'%';s.style.fontSize=(22+Math.random()*30)+'px';
s.style.animationDuration=(10+Math.random()*12)+'s';s.style.animationDelay=(-Math.random()*20)+'s';
s.style.setProperty('--r',(Math.random()*720-360)+'deg');r.appendChild(s)}})();
"""

# shared by the customer page and the rider console: top bar, sheets, chat, toast
CHAT_CSS = r"""
.top{position:sticky;top:0;z-index:30;display:flex;align-items:center;justify-content:space-between;gap:.6rem;padding:.7rem 1rem;padding-top:calc(.7rem + env(safe-area-inset-top));background:#f4f1ffcc;backdrop-filter:blur(18px) saturate(1.6);-webkit-backdrop-filter:blur(18px) saturate(1.6);border-bottom:1px solid transparent;transition:border-color .3s}
.top.sc{border-color:var(--line)}
.brand{display:flex;align-items:center;gap:.5rem;font-family:var(--disp);font-weight:700;font-size:1.05rem;letter-spacing:-.04em;color:var(--ink);text-decoration:none}
.crown{display:grid;place-items:center;width:2.1rem;height:2.1rem;border-radius:12px;background:var(--sun);font-size:1.1rem;rotate:-8deg}
.tools{display:flex;align-items:center;gap:.4rem}
.chip{text-decoration:none;position:relative;display:inline-flex;align-items:center;gap:.4rem;background:#fff;border:1px solid var(--line);border-radius:99px;padding:.4rem .75rem;font-weight:700;font-size:.85rem;color:var(--ink)}
.chip.ic{width:2.1rem;height:2.1rem;padding:0;justify-content:center}
.chip .d{width:.55rem;height:.55rem;border-radius:50%;background:var(--ok);animation:pulse 2s infinite}
.chip.off .d{background:var(--bad);animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 #12b76a88}70%,100%{box-shadow:0 0 0 8px #12b76a00}}
.ub{position:absolute;top:-6px;right:-4px;min-width:1.25rem;height:1.25rem;padding:0 .3rem;border-radius:99px;background:var(--bad);color:#fff;font-size:.7rem;font-weight:800;display:grid;place-items:center;animation:bump .45s var(--spring)}
@keyframes bump{50%{transform:scale(1.5)}}
body.so{overflow:hidden}
.scrim{position:fixed;inset:0;z-index:50;background:#1b134099;backdrop-filter:blur(5px);-webkit-backdrop-filter:blur(5px);opacity:0;pointer-events:none;transition:opacity .45s var(--out)}
body.so .scrim{opacity:1;pointer-events:auto}
.sheet{position:fixed;left:50%;bottom:0;z-index:60;width:min(540px,100%);max-height:92vh;max-height:92dvh;overflow-y:auto;overscroll-behavior:contain;background:var(--card);border-radius:30px 30px 0 0;padding:.6rem 1.2rem calc(1.4rem + env(safe-area-inset-bottom));transform:translate(-50%,105%);visibility:hidden;transition:transform .7s var(--out),visibility 0s .7s}
.sheet.open{transform:translate(-50%,0);visibility:visible;transition:transform .7s var(--out),visibility 0s}
.grab{width:42px;height:5px;border-radius:9px;background:var(--line);margin:.2rem auto .9rem}
.sh-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:.9rem}
.sh-h h2{font-size:1.25rem}
.x{display:grid;place-items:center;width:2.3rem;height:2.3rem;border-radius:50%;background:var(--mist);font-size:1.05rem;text-decoration:none;color:var(--ink);flex:none;transition:transform .3s var(--spring)}
.x:active{transform:scale(.86)}
.chatsheet{display:flex;flex-direction:column;height:88vh;height:88dvh;overflow:hidden;padding-bottom:calc(.6rem + env(safe-area-inset-bottom))}
.ch{display:flex;align-items:center;gap:.7rem;padding:.2rem 0 .7rem;border-bottom:1px solid var(--line)}
.av{display:grid;place-items:center;width:2.8rem;height:2.8rem;border-radius:50%;background:linear-gradient(135deg,var(--sun),#ffa800);font-size:1.4rem;flex:none}
.who{flex:1;min-width:0}.who b{display:block}.who small{color:var(--mut)}
.chatbox{flex:1;min-height:0;display:flex;flex-direction:column}
.cm{flex:1;min-height:0;overflow-y:auto;padding:.8rem .1rem;display:flex;flex-direction:column;gap:.45rem}
.cm:empty::before{content:"Say hi! Messages and voice notes show up here.";margin:auto;color:var(--mut);text-align:center;max-width:15em}
.mg{max-width:82%;padding:.6rem .85rem;border-radius:18px;line-height:1.35;font-size:.95rem;overflow-wrap:anywhere;animation:msg .45s var(--spring)}
.mg.me{align-self:flex-end;background:var(--royal);color:#fff;border-bottom-right-radius:6px}
.mg.th{align-self:flex-start;background:var(--mist);border-bottom-left-radius:6px}
.mg small{display:block;opacity:.6;font-size:.68rem;margin-top:.2rem}
@keyframes msg{from{transform:translateY(16px) scale(.9);opacity:0}}
.vp{display:flex;align-items:center;gap:.6rem;min-width:200px}
.vp .pp{width:2.2rem;height:2.2rem;border-radius:50%;background:#fff;color:var(--royal);font-size:.8rem;flex:none}
.th .vp .pp{background:var(--royal);color:#fff}
.vp .bar{flex:1;height:5px;border-radius:9px;background:#ffffff55;overflow:hidden}.th .vp .bar{background:var(--line)}
.vp .bar i{display:block;height:100%;width:0;background:#fff}.th .vp .bar i{background:var(--royal)}
.vp span{font-size:.8rem;font-weight:700}
.cb{padding-top:.7rem;border-top:1px solid var(--line)}
.ct,.crec{display:flex;align-items:center;gap:.45rem}
.ti{flex:1;min-width:0;padding:.75rem 1rem;border-radius:99px;border:1.5px solid var(--line);background:var(--mist)}
.ti:focus{outline:0;border-color:var(--royal);background:#fff}
.ib{display:grid;place-items:center;width:2.8rem;height:2.8rem;border-radius:50%;flex:none;background:var(--mist);font-size:1.1rem;transition:transform .25s var(--spring)}
.ib:active{transform:scale(.85)}
.ib.pri{background:var(--royal);color:#fff}
.crec{display:none}.recing .crec{display:flex}.recing .ct{display:none}
.rdot{width:.8rem;height:.8rem;border-radius:50%;background:var(--bad);animation:blink 1s infinite}
@keyframes blink{50%{opacity:.25}}
.rt{font-weight:800;font-variant-numeric:tabular-nums;min-width:2.6rem}
.wave{display:flex;gap:3px;align-items:center;height:30px;flex:1;overflow:hidden}
.wave i{width:3px;border-radius:3px;background:var(--royal);animation:wv .9s ease-in-out infinite alternate}
@keyframes wv{from{transform:scaleY(.25)}to{transform:scaleY(1)}}
.shut{display:none;text-align:center;color:var(--mut);padding:.6rem}
.shutc .cb{display:none}.shutc .shut{display:block}
#toast{position:fixed;left:50%;bottom:calc(1.5rem + env(safe-area-inset-bottom));transform:translate(-50%,160px);background:var(--ink);color:#fff;padding:.8rem 1.2rem;border-radius:16px;z-index:300;transition:transform .5s var(--spring);max-width:90%;text-align:center}
#toast.show{transform:translate(-50%,0)}
"""

CHAT_JS = r"""
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),3200)}
async function post(url,body){const f=body instanceof FormData,h=f?{}:{'Content-Type':'application/json'};if(window.CSRF)h['X-CSRF']=window.CSRF;
 const r=await fetch(url,{method:'POST',headers:h,body:f?body:JSON.stringify(body)});let j={};try{j=await r.json()}catch(e){}
 if(!r.ok)throw new Error(j.error||'Something went wrong');return j}
function chatView(root,api){
 root.innerHTML='<div class="cm"></div><div class="cb"><div class="ct"><button class="ib mic" aria-label="Record a voice note">🎙️</button><input class="ti" placeholder="Type a message" maxlength="500" autocomplete="off"><button class="ib pri snd" aria-label="Send">➤</button></div><div class="crec"><button class="ib rcan" aria-label="Cancel recording">🗑️</button><i class="rdot"></i><span class="rt">0:00</span><div class="wave"></div><button class="ib pri rsd" aria-label="Send voice note">➤</button></div><input type="file" class="cf" accept="audio/*" capture hidden></div><p class="shut">This order is finished, so the chat is closed.</p>';
 const q=s=>root.querySelector(s),box=q('.cm');
 let n=0,timer=null,mr=null,chunks=[],t0=0,tt=null,cancel=false,audio=null,loading=false;
 for(let i=0;i<24;i++){const b=document.createElement('i');b.style.animationDelay=(-Math.random())+'s';b.style.height=(30+Math.random()*70)+'%';q('.wave').appendChild(b)}
 const fmt=s=>Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0');
 const clock=ts=>new Date(ts*1000).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
 function add(m){const d=document.createElement('div');d.className='mg '+(m.f===api.me?'me':'th');
  d.innerHTML=(m.k==='v'?`<div class="vp" data-src="${esc(m.u)}" data-d="${m.d}"><button class="pp" aria-label="Play voice note">▶</button><div class="bar"><i></i></div><span>${m.d?fmt(m.d):'Voice'}</span></div>`:esc(m.x))+`<small>${clock(m.t)}</small>`;box.appendChild(d)}
 async function pull(){if(loading)return;loading=true;try{const j=await api.fetch(n);const was=box.scrollHeight-box.scrollTop-box.clientHeight<80||!n;
  j.msgs.forEach(add);if(j.msgs.length){if(n>0&&j.msgs.some(m=>m.f!==api.me)&&navigator.vibrate)navigator.vibrate(50);n+=j.msgs.length;if(was)box.scrollTop=box.scrollHeight}
  root.classList.toggle('shutc',!!j.closed)}catch(e){}loading=false}
 box.addEventListener('click',e=>{const p=e.target.closest('.pp');if(!p)return;const vp=p.closest('.vp'),bar=vp.querySelector('.bar i');
  if(audio&&audio._vp===vp){audio.paused?audio.play():audio.pause();return}
  if(audio){audio.pause();audio._vp.querySelector('.pp').textContent='▶'}
  audio=new Audio(vp.dataset.src);audio._vp=vp;
  audio.ontimeupdate=()=>{bar.style.width=(audio.currentTime/(isFinite(audio.duration)&&audio.duration?audio.duration:(+vp.dataset.d||1))*100)+'%'};
  audio.onplay=()=>p.textContent='⏸';audio.onpause=()=>p.textContent='▶';audio.onended=()=>{p.textContent='▶';bar.style.width='0'};
  audio.play().catch(()=>toast('Could not play that voice note.'))});
 async function sendText(){const ti=q('.ti'),v=ti.value.trim();if(!v)return;ti.value='';try{await api.text(v);await pull()}catch(e){ti.value=v;toast(e.message)}}
 function tickRec(){const s=(Date.now()-t0)/1000;q('.rt').textContent=fmt(s);if(s>=60)stopRec(false)}
 async function startRec(){
  if(!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia&&window.isSecureContext&&window.MediaRecorder)){q('.cf').click();return}
  try{const st=await navigator.mediaDevices.getUserMedia({audio:true});
   const mime=['audio/webm;codecs=opus','audio/webm','audio/mp4','audio/ogg'].find(m=>MediaRecorder.isTypeSupported(m))||'';
   mr=new MediaRecorder(st,mime?{mimeType:mime}:undefined);chunks=[];cancel=false;
   mr.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
   mr.onstop=async()=>{st.getTracks().forEach(t=>t.stop());clearInterval(tt);root.classList.remove('recing');
    const dur=Math.round((Date.now()-t0)/1000);if(cancel||!chunks.length||dur<1)return;
    try{await api.voice(new Blob(chunks,{type:mr.mimeType||'audio/webm'}),dur);await pull()}catch(e){toast(e.message)}};
   mr.start();t0=Date.now();root.classList.add('recing');q('.rt').textContent='0:00';tt=setInterval(tickRec,250)}
  catch(e){toast('Microphone is blocked. Allow it in your browser settings, or type a message.')}}
 function stopRec(c){cancel=c;if(mr&&mr.state==='recording')mr.stop();else{clearInterval(tt);root.classList.remove('recing')}}
 q('.cf').onchange=async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;if(f.size>2.5e6)return toast('That recording is too large. Keep it under a minute.');
  try{await api.voice(f,0);await pull()}catch(er){toast(er.message)}};
 q('.mic').onclick=startRec;q('.rcan').onclick=()=>stopRec(true);q('.rsd').onclick=()=>stopRec(false);q('.snd').onclick=sendText;
 q('.ti').onkeydown=e=>{if(e.key==='Enter')sendText()};
 return{on(){pull();clearInterval(timer);timer=setInterval(pull,3000)},off(){clearInterval(timer);stopRec(true);if(audio)audio.pause()},mic(){startRec()},pull}}
"""

# ---------------------------------------------------------------- customer page
MENU_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f4f1ff">
<title>__NAME__ | Fries and shawarma, delivered</title>__FONTS__
<style>__CSS__
body::before{content:"";position:fixed;inset:0 0 auto 0;height:480px;pointer-events:none;background:radial-gradient(60% 90% at 85% 0,#6a4cff2e,transparent),radial-gradient(45% 70% at 0 10%,#ffc93338,transparent)}
.wrap{position:relative;max-width:1040px;margin:0 auto;padding:.4rem 1rem 9rem}
.promo{position:relative;overflow:hidden;display:grid;grid-template-columns:1fr auto;gap:.8rem;align-items:end;border-radius:32px;padding:1.6rem 1.3rem;color:#fff;background:linear-gradient(135deg,#3a1fc0 0%,#5b3bf0 55%,#7d5bff 100%);box-shadow:0 30px 50px -28px #4b2bd6}
.promo::after{content:"";position:absolute;right:-60px;top:-80px;width:260px;height:260px;border-radius:50%;background:radial-gradient(circle,#ffc93366,transparent 70%)}
.promo h1{position:relative;z-index:1;font-weight:700;font-size:clamp(1.9rem,7.5vw,3.5rem);line-height:1.04;letter-spacing:-.045em}
.promo h1 span{display:inline-block;animation:rise .9s var(--out) backwards}
@keyframes rise{from{transform:translateY(60%);opacity:0;filter:blur(6px)}}
.promo p{position:relative;z-index:1;margin-top:.8rem;opacity:.85;max-width:26em}
.pchips{position:relative;z-index:1;display:flex;gap:.45rem;flex-wrap:wrap;margin-top:1.1rem}
.pchips span{background:#ffffff22;border:1px solid #ffffff30;padding:.4rem .75rem;border-radius:99px;font-weight:700;font-size:.85rem;animation:rise .9s var(--out) .5s backwards}
.plate{position:relative;z-index:1;width:6.4rem;height:7.4rem}
.e{position:absolute;display:block;filter:drop-shadow(0 14px 12px #1b134066);animation:bob 5s ease-in-out infinite alternate}
.e1{font-size:4.8rem;right:0;bottom:0;rotate:-8deg;translate:calc(var(--px,0)*14px) calc(var(--py,0)*10px)}
.e2{font-size:3rem;left:-.3rem;top:0;rotate:10deg;animation-delay:-2s;translate:calc(var(--px,0)*-18px) calc(var(--py,0)*-12px)}
.e3{font-size:2.1rem;left:.3rem;bottom:.2rem;animation-delay:-3.4s}
@keyframes bob{to{transform:translateY(-12px) rotate(4deg)}}
@media(min-width:720px){.promo{padding:2.8rem 2.6rem}.plate{width:15rem;height:12rem}.e1{font-size:10rem}.e2{font-size:5.5rem}.e3{font-size:3.6rem}}
.sh{display:flex;align-items:baseline;justify-content:space-between;margin:2rem .3rem 1rem}
.sh h2{font-size:1.5rem}.sh span{color:var(--mut);font-weight:700}
.banner{display:none;margin-bottom:1rem;background:#fff3cd;border:1px solid #ffe08a;padding:.9rem 1.1rem;border-radius:18px;font-weight:700}
body.closed .banner{display:block;animation:rise .6s var(--out)}
.list{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fill,minmax(min(100%,430px),1fr))}
.skel{height:136px;border-radius:26px;background:linear-gradient(100deg,#ece8ff 30%,#f9f7ff 50%,#ece8ff 70%);background-size:300% 100%;animation:shim 1.4s infinite}
@keyframes shim{from{background-position:100% 0}to{background-position:-100% 0}}
.it{display:flex;gap:.9rem;background:var(--card);border-radius:26px;padding:.8rem;border:1.5px solid var(--line);box-shadow:0 14px 30px -22px #4b2bd6;transition:box-shadow .35s var(--out),transform .35s var(--out),border-color .3s}
.it:hover{transform:translateY(-3px);box-shadow:0 22px 40px -22px #4b2bd6}
.it.has{border-color:var(--royal);box-shadow:0 0 0 4px #4b2bd61a,0 18px 34px -20px #4b2bd6}
.rv{opacity:0;translate:0 28px}.rv.in{opacity:1;translate:none;transition:opacity .8s var(--out),translate .8s var(--out);transition-delay:calc(var(--i)*70ms)}
.ph{position:relative;flex:none;width:7.2rem;height:7.2rem;border-radius:20px;overflow:hidden;display:grid;place-items:center;background:linear-gradient(145deg,#fff1c2,#ffd35c)}
.ph img{width:100%;height:100%;object-fit:cover;transition:transform .9s var(--out)}
.it:hover .ph img{transform:scale(1.1)}
.em{font-size:3.4rem;transition:transform .5s var(--spring)}.it:hover .em{transform:scale(1.15) rotate(-8deg)}
.tg{position:absolute;left:6px;top:6px;background:var(--ink);color:#fff;font-size:.68rem;font-weight:800;padding:.2rem .5rem;border-radius:8px}
.tg.soon{background:var(--royal)}
.it.soon .ph,.it.out .ph{filter:grayscale(.6) opacity(.85)}
.bd{display:flex;flex-direction:column;min-width:0;flex:1}
.bd h3{font-family:'Figtree',system-ui,sans-serif;font-weight:800;font-size:1.05rem;letter-spacing:-.01em}
.bd p{font-size:.88rem;color:var(--mut);margin-top:.15rem;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ft{margin-top:auto;display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding-top:.5rem}
.pr{font-family:var(--disp);font-weight:600;font-size:1rem;letter-spacing:-.03em}
.ctl{min-height:2.6rem;display:flex;align-items:center}
.add{background:var(--ink);color:#fff;border-radius:99px;padding:.6rem 1.25rem;font-weight:800;transition:transform .25s var(--spring),background .2s}
.add:hover{background:var(--royal)}.add:active{transform:scale(.92)}
.add.soon{background:repeating-linear-gradient(-45deg,#eee9ff 0 8px,#e0d9ff 8px 16px);background-size:23px 23px;color:var(--royal);animation:stripes 1s linear infinite;pointer-events:none}
@keyframes stripes{to{background-position:23px 0}}
.add.off{background:#e9e6f5;color:var(--mut);pointer-events:none}
.step{display:inline-flex;align-items:center;background:var(--royal);color:#fff;border-radius:99px;padding:.2rem;animation:morph .55s var(--spring)}
.step.nm{animation:none}
.step button{width:2.2rem;height:2.2rem;border-radius:50%;background:#ffffff26;color:#fff;font-size:1.25rem;font-weight:700;line-height:1;transition:transform .2s var(--spring),background .2s}
.step button:hover{background:#ffffff44}.step button:active{transform:scale(.8)}
.q{display:inline-block;min-width:2rem;text-align:center;font-weight:800;font-variant-numeric:tabular-nums}
.q.tick{animation:tick .4s var(--spring)}
@keyframes morph{from{transform:scale(.45);opacity:0}}
@keyframes tick{from{transform:translateY(60%) scale(.5);opacity:0}}
.fly{position:fixed;z-index:99;font-size:2rem;pointer-events:none;border-radius:50%;object-fit:cover}
#bar{position:fixed;left:50%;bottom:calc(1rem + env(safe-area-inset-bottom));z-index:40;width:min(520px,calc(100% - 1.6rem));display:flex;align-items:center;justify-content:space-between;gap:1rem;background:var(--ink);color:#fff;border-radius:24px;padding:.7rem .7rem .7rem 1rem;box-shadow:0 22px 44px -14px #1b1340dd;transform:translate(-50%,200%);transition:transform .8s var(--spring);cursor:pointer}
#bar.show{transform:translate(-50%,0)}
#bar .bl{display:flex;align-items:center;gap:.7rem;font-weight:800}
#bar .n{display:grid;place-items:center;min-width:2rem;height:2rem;border-radius:11px;background:#ffffff22}
#bar .go{background:var(--sun);color:var(--ink);padding:.65rem 1.05rem;border-radius:16px;font-weight:800}
.ln{display:flex;align-items:center;gap:.7rem;padding:.7rem 0;border-bottom:1px dashed var(--line)}
.lt{display:grid;place-items:center;width:3rem;height:3rem;border-radius:14px;background:#fff1c2;font-size:1.6rem;overflow:hidden;flex:none}.lt img{width:100%;height:100%;object-fit:cover}
.lm{flex:1;min-width:0}.lm b{display:block}.lm small{color:var(--mut)}.lp{font-weight:800;min-width:4.2rem;text-align:right}
.empty{color:var(--mut);padding:1rem 0}
.seg{position:relative;display:grid;grid-template-columns:1fr 1fr;background:var(--mist);border-radius:16px;padding:4px;margin:1rem 0 .8rem}
.seg .thumb{position:absolute;top:4px;bottom:4px;left:4px;width:calc(50% - 4px);background:#fff;border-radius:13px;box-shadow:0 4px 10px -4px #4b2bd655;transition:transform .5s var(--spring)}
.seg[data-m=pickup] .thumb{transform:translateX(100%)}
.seg button{position:relative;padding:.65rem;font-weight:800}
.fi{display:block;width:100%;padding:.85rem 1rem;border-radius:16px;border:1.5px solid var(--line);background:var(--mist);margin-bottom:.55rem;transition:border-color .2s,box-shadow .2s,background .2s}
.fi:focus{outline:0;border-color:var(--royal);background:#fff;box-shadow:0 0 0 4px #4b2bd61f}
.collapse{display:grid;grid-template-rows:1fr;transition:grid-template-rows .55s var(--out)}
.collapse .inner{overflow:hidden;min-height:0}
#fields[data-m=pickup] .collapse{grid-template-rows:0fr}
.kmrow{display:flex;gap:.5rem;align-items:flex-start}.kmrow .fi{flex:1}
.kmrow .mini{padding:.85rem .9rem;border-radius:14px;white-space:nowrap;background:#ebe5ff;color:var(--royal)}
#map{height:240px;border-radius:18px;margin:.1rem 0 .4rem;background:var(--mist);border:1px solid var(--line);isolation:isolate}
.pin{font-size:1.9rem;line-height:1;filter:drop-shadow(0 3px 3px #0004)}
.hint{display:block;color:var(--mut);margin:-.1rem .2rem .8rem;font-size:.85rem}
.sum{margin:.6rem 0 1rem;padding:.9rem 1rem;background:var(--mist);border-radius:18px}
.sum div{display:flex;justify-content:space-between;gap:1rem;padding:.2rem 0}.sum small{color:var(--mut)}
.sum .tot{font-size:1.2rem;font-weight:800;border-top:1.5px dashed var(--line);margin-top:.4rem;padding-top:.6rem}
.cta{display:block;width:100%;background:var(--sun);color:var(--ink);padding:1.05rem;border-radius:20px;font-weight:800;font-size:1.05rem;box-shadow:0 14px 26px -12px #ffb400;transition:transform .25s var(--spring)}
.cta:active{transform:scale(.97)}.cta[disabled]{opacity:.5;pointer-events:none}
.big{font-family:var(--disp);font-weight:600;font-size:1.45rem;letter-spacing:-.04em;line-height:1.15}
.tl{position:relative;display:grid;grid-template-columns:repeat(4,1fr);margin:1.6rem 0 1.4rem}
.tl::before{content:"";position:absolute;left:12.5%;right:12.5%;top:1rem;height:4px;border-radius:9px;background:var(--line)}
.tl .fb{position:absolute;left:12.5%;top:1rem;height:4px;border-radius:9px;background:linear-gradient(90deg,var(--royal),var(--royal2));width:calc(75% * var(--tp,0));animation:grow 1.2s var(--out) backwards}
@keyframes grow{from{width:0}}
.tl .s{position:relative;z-index:1;display:flex;flex-direction:column;align-items:center;gap:.45rem;text-align:center;font-size:.74rem;font-weight:700;color:var(--mut)}
.tl .s i{display:grid;place-items:center;width:2.1rem;height:2.1rem;border-radius:50%;background:#fff;border:3px solid var(--line);font-style:normal;font-size:.8rem;transition:all .5s var(--spring)}
.tl .s.on{color:var(--ink)}.tl .s.on i{background:var(--royal);border-color:var(--royal);color:#fff}
.tl .s.cur i{animation:ring 1.8s infinite}
@keyframes ring{0%{box-shadow:0 0 0 0 #4b2bd666}70%,100%{box-shadow:0 0 0 10px #4b2bd600}}
.tsum{background:var(--mist);border-radius:18px;padding:.9rem 1rem;color:var(--mut);font-size:.92rem}.tsum b{color:var(--ink)}
.acts{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:.6rem;margin-top:1rem}
.act{position:relative;display:flex;flex-direction:column;align-items:center;gap:.25rem;padding:.95rem .5rem;border-radius:20px;background:var(--mist);font-weight:800;font-size:.9rem;text-decoration:none;color:var(--ink);transition:transform .25s var(--spring),background .2s}
.act:hover{background:#ebe5ff}.act:active{transform:scale(.94)}.act em{font-style:normal;font-size:1.6rem}
.links{display:flex;gap:.5rem;flex-wrap:wrap;margin:.2rem 0 1rem}.links a{text-decoration:none}
.muted{color:var(--mut);margin-bottom:.8rem}
.conf{position:fixed;z-index:200;pointer-events:none;left:50%;top:40%;width:10px;height:14px;border-radius:2px}
</style></head>
<body>
<header class="top" id="top"><a class="brand" href="/"><span class="crown">👑</span>__NAME__</a>
<div class="tools"><button class="chip" id="pill" hidden></button><span class="chip" id="st"><i class="d"></i><span id="stT">Open</span></span><button class="chip ic" id="helpB" aria-label="Help and support">?</button></div></header>
<main class="wrap">
<section class="promo"><div><h1 id="h1"></h1><p id="sub"></p><div class="pchips"><span>🛵 Delivery</span><span>🏪 Pickup</span><span id="feeChip"></span></div></div>
<div class="plate" id="plate"><span class="e e1">🍟</span><span class="e e2">🌯</span><span class="e e3">🌶️</span></div></section>
<div class="sh"><h2>Menu</h2><span id="cnt"></span></div>
<div class="banner">We are closed right now. You can look around, but ordering is paused until we reopen.</div>
<section class="list" id="list"><div class="skel"></div><div class="skel"></div><div class="skel"></div></section>
</main>
<div id="bar" role="button" tabindex="0" aria-label="View cart"><span class="bl"><span class="n" id="bn">0</span>View cart</span><span class="go" id="bt"></span></div>
<div class="scrim" id="scrim"></div>

<section class="sheet" id="sh-cart" aria-label="Your order"><div class="grab"></div>
<div id="cv-order"><div class="sh-h"><h2>Your order</h2><button class="x" data-close aria-label="Close">✕</button></div>
<div id="lines"></div>
<div class="seg" id="seg" data-m="delivery"><i class="thumb"></i><button data-mode="delivery">🛵 Delivery</button><button data-mode="pickup">🏪 Pickup</button></div>
<div id="fields" data-m="delivery"><input class="fi" id="fn" placeholder="Your name" maxlength="60" autocomplete="name"><input class="fi" id="fp" placeholder="Phone number" maxlength="20" inputmode="tel" autocomplete="tel">
<div class="collapse"><div class="inner"><textarea class="fi" id="fa" rows="2" placeholder="Delivery address" maxlength="200"></textarea>
<div id="mapbox" hidden><div class="kmrow"><button class="mini" id="loc" type="button" hidden>📍 Use my location</button></div><div id="map"></div><small class="hint" id="mapInfo">Tap the map to drop a pin on your delivery spot (drag it to adjust).</small></div>
<small class="hint" id="hint"></small></div></div>
<input class="fi" id="fo" placeholder="Notes (optional)" maxlength="150"></div>
<div class="sum"><div><span>Subtotal</span><b id="sSub"></b></div><div><span>Delivery <small id="sKm"></small></span><b id="sFee"></b></div><div class="tot"><span>Total</span><b id="sTot"></b></div></div>
<button class="cta" id="go">Place order</button></div>
<div id="cv-track" hidden></div></section>

<section class="sheet chatsheet" id="sh-chat" aria-label="Chat with your rider"><div class="ch"><button class="x" data-back aria-label="Back">←</button><span class="av">🛵</span>
<div class="who"><b id="rn"></b><small>Message or send a voice note</small></div><a class="x" id="rcall" hidden aria-label="Call rider">📞</a></div><div class="chatbox" id="chatbox"></div></section>

<section class="sheet" id="sh-help" aria-label="Help"><div class="grab"></div><div class="sh-h"><h2>Need help?</h2><button class="x" data-close aria-label="Close">✕</button></div>
<p class="muted" id="supHi"></p><div class="links" id="supLinks"></div>
<input class="fi" id="sn" placeholder="Your name" maxlength="60"><input class="fi" id="sc" placeholder="Phone or email" maxlength="60">
<textarea class="fi" id="sm" rows="3" placeholder="How can we help?" maxlength="800"></textarea><button class="cta" id="sSend">Send message</button></section>
<div id="toast"></div>
<script>
__CHATJS__
const CFG=__CFG__;
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const rs=n=>CFG.cur+' '+Number(n).toLocaleString();
const clamp=v=>Math.max(0,Math.min(1,v));
const reduce=matchMedia('(prefers-reduced-motion:reduce)').matches;
let menu={open:true,items:[]},cart={},mode='delivery',sig='',first=true,busy=false,my=null,trk=null,view='order',chat=null,trkSig='';
const form={name:'',phone:'',address:'',note:'',km:'',lat:null,lng:null};
try{my=JSON.parse(localStorage.getItem('sk_order')||'null')}catch(e){}
const saveMy=()=>{try{my?localStorage.setItem('sk_order',JSON.stringify(my)):localStorage.removeItem('sk_order')}catch(e){}};
$('#h1').innerHTML='Crispy, hot, at your door.'.split(' ').map((w,i)=>`<span style="animation-delay:${.1+i*.09}s">${w}</span>`).join(' ');
$('#sub').textContent=CFG.tagline||'';
$('#feeChip').textContent=rs(CFG.base)+' + '+rs(CFG.perKm)+'/km';
$('#hint').textContent='Delivery '+rs(CFG.base)+' + '+rs(CFG.perKm)+' per km, up to '+CFG.maxKm+' km.';
$('#rn').textContent=CFG.riderName||'Your rider';
addEventListener('scroll',()=>$('#top').classList.toggle('sc',scrollY>8),{passive:true});
addEventListener('pointermove',e=>{const p=$('#plate');p.style.setProperty('--px',(e.clientX/innerWidth-.5).toFixed(3));p.style.setProperty('--py',(e.clientY/innerHeight-.5).toFixed(3))});

// ---------- menu
const stepHTML=q=>`<div class="step"><button data-act="dec" aria-label="Remove one">&minus;</button><span class="q">${q}</span><button data-act="inc" aria-label="Add one">+</button></div>`;
const stateOf=it=>it.soon?'soon':!it.stock?'out':!menu.open?'closed':'ok';
function ctl(it){const st=stateOf(it),q=cart[it.id]||0;
 if(st==='soon')return '<button class="add soon" disabled>Coming soon</button>';
 if(st==='out')return '<button class="add off" disabled>Sold out</button>';
 if(st==='closed')return '<button class="add off" disabled>Closed</button>';
 return q?stepHTML(q):'<button class="add" data-act="add">Add</button>'}
function itemHTML(it,i){const st=stateOf(it);
 return `<article class="it ${st} ${cart[it.id]?'has':''} ${first?'rv':''}" data-id="${it.id}" style="--i:${i}"><div class="ph">${it.image?`<img src="${esc(it.image)}" alt="${esc(it.name)}" loading="lazy">`:`<span class="em">${esc(it.emoji)}</span>`}${it.soon?'<span class="tg soon">Coming soon</span>':!it.stock?'<span class="tg">Sold out</span>':''}</div>
 <div class="bd"><h3>${esc(it.name)}</h3><p>${esc(it.desc)}</p><div class="ft"><span class="pr">${rs(it.price)}</span><div class="ctl">${ctl(it)}</div></div></div></article>`}
const io=new IntersectionObserver(es=>es.forEach(e=>{if(!e.isIntersecting)return;const el=e.target;el.classList.add('in');io.unobserve(el);setTimeout(()=>el.classList.remove('rv','in'),1000+(+el.style.getPropertyValue('--i'))*70)}),{threshold:.1});
function render(){
 document.body.classList.toggle('closed',!menu.open);$('#st').classList.toggle('off',!menu.open);$('#stT').textContent=menu.open?'Open':'Closed';
 $('#cnt').textContent=menu.items.length+' items';
 $('#list').innerHTML=menu.items.map(itemHTML).join('');
 if(first){$$('#list .rv').forEach(c=>io.observe(c));first=false}
 bar();
 if($('#sh-cart').classList.contains('open')&&view==='order'){if(!Object.keys(cart).length)sheet(null);else{renderLines();summary()}}}
async function refresh(force){try{const r=await fetch('/api/menu');const j=await r.json();const s=JSON.stringify(j);if(s===sig&&!force)return;sig=s;menu=j;
 let gone=false;for(const id in cart){const it=menu.items.find(i=>i.id==id);if(!it||!it.stock||it.soon){delete cart[id];gone=true}}
 if(gone)toast('Some items in your cart are no longer available.');render()}catch(e){}}
const totals=()=>{let n=0,sub=0;for(const id in cart){const it=menu.items.find(i=>i.id==id);if(it){n+=cart[id];sub+=it.price*cart[id]}}return{n,sub}};
function tween(el,to){if(!el)return;const from=el._v||0;el._v=to;const t=performance.now();(function s(x){const p=clamp((x-t)/550),e=1-Math.pow(1-p,3);el.textContent=rs(Math.round(from+(to-from)*e));if(p<1)requestAnimationFrame(s)})(t)}
function bump(el,n){el.textContent=n;el.classList.remove('tick');void el.offsetWidth;el.classList.add('tick')}
function bar(){const t=totals(),b=$('#bar');b.classList.toggle('show',t.n>0&&!document.body.classList.contains('so'));if($('#bn').textContent!=t.n)bump($('#bn'),t.n);tween($('#bt'),t.sub)}
function change(id,d){const it=menu.items.find(i=>i.id==id);if(!it)return;const old=cart[id]||0,n=Math.max(0,Math.min(20,old+d));if(n===old)return;
 n?cart[id]=n:delete cart[id];
 const card=$(`#list [data-id="${id}"]`);if(card){card.classList.toggle('has',n>0);const c=card.querySelector('.ctl'),q=c.querySelector('.q');
  if(!q||!n||!old)c.innerHTML=ctl(it);else bump(q,n)}
 bar();if($('#sh-cart').classList.contains('open')&&view==='order'){if(!Object.keys(cart).length)sheet(null);else{renderLines();summary()}}}
function fly(id,btn){if(reduce)return;const it=menu.items.find(i=>i.id==id),a=btn.getBoundingClientRect();let f;
 if(it.image){f=document.createElement('img');f.src=it.image;f.style.width=f.style.height='44px'}else{f=document.createElement('div');f.textContent=it.emoji}
 f.className='fly';f.style.left=a.left+'px';f.style.top=a.top+'px';document.body.appendChild(f);
 const dx=innerWidth/2-a.left,dy=innerHeight-60-a.top;
 f.animate([{transform:'translate(0,0) scale(1)'},{transform:`translate(${dx*.35}px,-120px) scale(1.4) rotate(160deg)`,offset:.45},{transform:`translate(${dx}px,${dy}px) scale(.3) rotate(340deg)`,opacity:.4}],{duration:800,easing:'cubic-bezier(.4,0,.6,1)'}).onfinish=()=>f.remove()}

// ---------- sheets
function sheet(id){$$('.sheet').forEach(s=>s.classList.toggle('open',s.id===id));document.body.classList.toggle('so',!!id);bar()}
function closeAll(){if(chat){chat.off();chat=null}
 if(view==='track'&&trk&&(trk.status==='done'||trk.status==='cancelled')&&$('#sh-cart').classList.contains('open')){my=null;trk=null;saveMy();updatePill()}
 sheet(null)}
function renderLines(){const ids=Object.keys(cart);
 $('#lines').innerHTML=ids.map(id=>{const it=menu.items.find(i=>i.id==id);return `<div class="ln" data-id="${id}"><span class="lt">${it.image?`<img src="${esc(it.image)}" alt="">`:esc(it.emoji)}</span><div class="lm"><b>${esc(it.name)}</b><small>${rs(it.price)} each</small></div>${stepHTML(cart[id]).replace('class="step"','class="step nm"')}<span class="lp">${rs(it.price*cart[id])}</span></div>`}).join('')||'<p class="empty">Your cart is empty.</p>'}
const HASMAP=!!(CFG.storeLat||CFG.storeLng);
function calc(){const sub=totals().sub,km=HASMAP?parseFloat(form.km):0,fee=mode==='pickup'?0:(km>=0?Math.round(CFG.base+CFG.perKm*km):null);return{sub,km,fee,tot:sub+(fee||0)}}
function summary(){const c=calc();tween($('#sSub'),c.sub);const f=$('#sFee');
 if(c.fee===null){f.textContent='Pick on map';f._v=0}else tween(f,c.fee);
 $('#sKm').textContent=mode==='pickup'?'(pickup)':(HASMAP&&c.km>=0?`(${CFG.base} + ${CFG.perKm} x ${c.km} km)`:'');tween($('#sTot'),c.tot);
 $('#go').textContent=menu.open?'Place order  -  '+rs(c.tot):'Store is closed';$('#go').disabled=!menu.open}
function openCart(){if(!totals().n){if(my)openTrack();return}view='order';$('#cv-order').hidden=false;$('#cv-track').hidden=true;
 $('#fn').value=form.name;$('#fp').value=form.phone;$('#fa').value=form.address;$('#fo').value=form.note;
 $('#seg').dataset.m=mode;$('#fields').dataset.m=mode;renderLines();summary();sheet('sh-cart');if(mode==='delivery')setTimeout(initMap,750)}
function openTrack(){if(!my)return;view='track';$('#cv-order').hidden=true;$('#cv-track').hidden=false;trkSig='';renderTrack();sheet('sh-cart')}
function renderTrack(){const t=trk;if(!t)return;const pick=t.mode==='pickup';
 const steps=pick?['Received','Preparing','Ready','Picked up']:['Received','Preparing','On the way','Delivered'];
 const idx={new:0,preparing:1,ready:pick?2:1,out:2,done:3}[t.status];
 const head={new:'Order received',preparing:'Preparing your food',ready:pick?'Ready for pickup':'Packed, waiting for the rider',out:pick?'Ready for pickup':'On the way to you',done:pick?'Picked up. Enjoy!':'Delivered. Enjoy!',cancelled:'Order cancelled'}[t.status]||'';
 const un=Math.max(0,(t.nr||0)-(my.seenr||0));const sg=t.status+'|'+un+'|'+t.canChat;if(sg===trkSig)return;trkSig=sg;
 const tel=CFG.riderPhone?`<a class="act" href="tel:${esc(CFG.riderPhone)}"><em>📞</em>Call</a>`:'';
 $('#cv-track').innerHTML=`<div class="sh-h"><h2>Order #${t.id}</h2><button class="x" data-close aria-label="Close">✕</button></div><div class="big">${esc(head)}</div>
 ${t.status==='cancelled'?'':`<div class="tl" style="--tp:${idx/3}"><b class="fb"></b>${steps.map((s,i)=>`<div class="s ${i<=idx?'on':''} ${i===idx?'cur':''}"><i>${i<idx?'✓':i+1}</i>${s}</div>`).join('')}</div>`}
 <div class="tsum">${esc(t.items.join(', '))}<br><b>Total ${rs(t.total)}</b> - pay on ${pick?'pickup':'delivery'}</div>
 ${t.canChat?`<div class="acts"><button class="act" data-openchat><em>💬</em>Chat${un?`<span class="ub">${un}</span>`:''}</button><button class="act" data-openchat data-voice><em>🎙️</em>Voice note</button>${tel}</div>`:''}`}
function updatePill(){const p=$('#pill');if(!my||!trk){p.hidden=true;return}
 const un=Math.max(0,(trk.nr||0)-(my.seenr||0));const lab={new:'Received',preparing:'Preparing',ready:'Ready',out:trk.mode==='pickup'?'Ready':'On the way',done:trk.mode==='pickup'?'Picked up':'Delivered',cancelled:'Cancelled'}[trk.status]||'';
 p.hidden=false;p.innerHTML=`🛵 ${lab}${un?`<span class="ub">${un}</span>`:''}`}
async function pollTrack(){if(!my)return;try{const r=await fetch(`/api/track?id=${my.id}&token=${encodeURIComponent(my.token)}&since=999999`);
 if(r.status===404){my=null;trk=null;saveMy();updatePill();return}trk=await r.json();updatePill();if(view==='track'&&$('#sh-cart').classList.contains('open'))renderTrack()}catch(e){}}
function openChat(voice){if(!my)return;
 chat=chatView($('#chatbox'),{me:'c',
  fetch:async s=>{const r=await fetch(`/api/track?id=${my.id}&token=${encodeURIComponent(my.token)}&since=${s}`);if(!r.ok)throw 0;const j=await r.json();trk=j;my.seenr=j.nr;saveMy();updatePill();return{msgs:j.msgs,closed:!j.canChat}},
  text:t=>post('/api/chat',{id:my.id,token:my.token,text:t}),
  voice:(b,d)=>{const f=new FormData();f.append('id',my.id);f.append('token',my.token);f.append('dur',d);f.append('audio',b,'voice.webm');return post('/api/chat/voice',f)}});
 sheet('sh-chat');chat.on();if(voice)setTimeout(()=>chat.mic(),550)}
function backFromChat(){if(chat){chat.off();chat=null}trkSig='';openTrack()}

// ---------- events
document.addEventListener('click',e=>{const t=e.target;
 if(t.closest('[data-close]')||t.id==='scrim'){closeAll();return}
 if(t.closest('[data-back]')){backFromChat();return}
 const oc=t.closest('[data-openchat]');if(oc){openChat(oc.hasAttribute('data-voice'));return}
 const m=t.closest('[data-mode]');if(m){mode=m.dataset.mode;$('#seg').dataset.m=mode;$('#fields').dataset.m=mode;summary();if(mode==='delivery')setTimeout(initMap,650);return}
 const b=t.closest('[data-act]');if(!b)return;const host=b.closest('[data-id]');if(!host)return;const id=host.dataset.id,a=b.dataset.act;
 if(a==='add')fly(id,b);change(id,a==='dec'?-1:1)});
addEventListener('keydown',e=>{if(e.key==='Escape')closeAll()});
$('#bar').onclick=openCart;$('#bar').onkeydown=e=>{if(e.key==='Enter'||e.key===' ')openCart()};
$('#pill').onclick=openTrack;$('#helpB').onclick=()=>sheet('sh-help');
const fmap={fn:'name',fp:'phone',fa:'address',fo:'note'};
Object.keys(fmap).forEach(id=>$('#'+id).addEventListener('input',e=>{form[fmap[id]]=e.target.value}));
if(CFG.riderPhone){$('#rcall').hidden=false;$('#rcall').href='tel:'+CFG.riderPhone}
// ---------- delivery distance (OpenStreetMap + Leaflet, no API key)
function hav(a,b,c,d){const r=Math.PI/180,p=(c-a)*r/2,q=(d-b)*r/2,x=Math.sin(p)**2+Math.cos(a*r)*Math.cos(c*r)*Math.sin(q)**2;return 12742*Math.asin(Math.sqrt(x))}
let lmap=null,lpin=null;
if(HASMAP)$('#mapbox').hidden=false;
function loadLeaflet(){if(window.L)return Promise.resolve();
 return new Promise((ok,no)=>{const l=document.createElement('link');l.rel='stylesheet';l.href='https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css';document.head.appendChild(l);
  const s=document.createElement('script');s.src='https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js';s.onload=ok;s.onerror=no;document.head.appendChild(s)})}
const emo=(e,sz)=>L.divIcon({className:'',html:`<div class="pin">${e}</div>`,iconSize:[sz,sz],iconAnchor:[sz/2,sz-2]});
async function initMap(){if(!HASMAP)return;
 if(lmap){lmap.invalidateSize();return}
 try{await loadLeaflet()}catch(e){return toast('The map could not load. Check your connection.')}
 lmap=L.map('map',{zoomControl:true,attributionControl:true}).setView([CFG.storeLat,CFG.storeLng],13);
 L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(lmap);
 L.marker([CFG.storeLat,CFG.storeLng],{icon:emo('🏪',32),interactive:false}).addTo(lmap);
 lmap.on('click',e=>setPin(e.latlng.lat,e.latlng.lng));
 if(form.lat!=null)setPin(form.lat,form.lng);
 setTimeout(()=>lmap.invalidateSize(),200)}
function setPin(lat,lng){
 if(!lpin){lpin=L.marker([lat,lng],{icon:emo('📍',32),draggable:true}).addTo(lmap);lpin.on('dragend',()=>{const p=lpin.getLatLng();setPin(p.lat,p.lng)})}
 else lpin.setLatLng([lat,lng]);
 const km=Math.round(hav(CFG.storeLat,CFG.storeLng,lat,lng)*CFG.road*10)/10;
 form.km=String(km);form.lat=lat;form.lng=lng;
 $('#mapInfo').textContent=km>CFG.maxKm?`About ${km} km - too far, we deliver up to ${CFG.maxKm} km.`:`About ${km} km from the shop.`;summary()}
if(window.isSecureContext&&HASMAP){$('#loc').hidden=false;$('#loc').onclick=()=>{const b=$('#loc');b.textContent='Locating...';
 navigator.geolocation.getCurrentPosition(p=>{b.textContent='📍 Use my location';if(!lmap)return;lmap.setView([p.coords.latitude,p.coords.longitude],16);setPin(p.coords.latitude,p.coords.longitude)},
 ()=>{b.textContent='📍 Use my location';toast('Could not get your location. Tap the map instead.')},{enableHighAccuracy:true,timeout:10000})}}
// ---------- place order
async function order(){if(busy)return;const c=calc();
 if(!form.name.trim()||!form.phone.trim())return toast('Add your name and phone number.');
 if(mode==='delivery'){if(!form.address.trim())return toast('Add your delivery address.');if(HASMAP&&!(c.km>=0))return toast('Tap the map to set your delivery location.');if(c.km>CFG.maxKm)return toast('We deliver up to '+CFG.maxKm+' km.')}
 busy=true;const go=$('#go');go.disabled=true;
 try{const j=await post('/api/order',{name:form.name,phone:form.phone,address:mode==='pickup'?'':form.address,note:form.note,mode,km:c.km,lat:form.lat,lng:form.lng,items:Object.entries(cart).map(([id,qty])=>({id:+id,qty}))});
  my={id:j.id,token:j.token,seenr:0};trk=j;saveMy();cart={};form.note='';render();updatePill();openTrack();boom()}
 catch(e){toast(e.message);refresh(true)}finally{busy=false;go.disabled=false}}
$('#go').onclick=order;
function boom(){if(reduce)return;const cols=['#ffc933','#4b2bd6','#6a4cff','#ffffff','#12b76a'];
 for(let i=0;i<80;i++){const c=document.createElement('div');c.className='conf';
  if(i%7===0){c.textContent='🍟';c.style.cssText+=';width:auto;height:auto;font-size:1.6rem'}else c.style.background=cols[i%cols.length];
  document.body.appendChild(c);const a=Math.random()*Math.PI*2,v=120+Math.random()*380,dx=Math.cos(a)*v,dy=Math.sin(a)*v-160,r=Math.random()*900-450;
  c.animate([{transform:'translate(0,0) rotate(0) scale(.3)'},{transform:`translate(${dx}px,${dy}px) rotate(${r/2}deg) scale(1)`,offset:.45},{transform:`translate(${dx*1.25}px,${dy+620}px) rotate(${r}deg)`,opacity:0}],{duration:1700+Math.random()*900,easing:'cubic-bezier(.2,.6,.4,1)'}).onfinish=()=>c.remove()}}
// ---------- support
$('#supHi').textContent='Questions about the menu or delivery? Message us.'+(CFG.hours?' We are around: '+CFG.hours+'.':'');
const L=[];if(CFG.wa)L.push(`<a class="mini" target="_blank" rel="noopener" href="https://wa.me/${CFG.wa}">WhatsApp</a>`);
if(CFG.phone)L.push(`<a class="mini" href="tel:${esc(CFG.phone)}">Call</a>`);if(CFG.email)L.push(`<a class="mini" href="mailto:${esc(CFG.email)}">Email</a>`);$('#supLinks').innerHTML=L.join('');
$('#sSend').onclick=async()=>{const b=$('#sSend');b.disabled=true;
 try{await post('/api/support',{name:$('#sn').value,contact:$('#sc').value,message:$('#sm').value});toast('Message sent. We will reply soon.');$('#sm').value='';sheet(null)}
 catch(e){toast(e.message)}b.disabled=false};
refresh();setInterval(refresh,15000);pollTrack();setInterval(pollTrack,6000);
</script></body></html>"""

# ---------------------------------------------------------------- rider console
RIDER_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#f4f1ff">
<title>Rider console | __NAME__</title>__FONTS__
<style>__CSS__
.wrap{max-width:760px;margin:0 auto;padding:1rem 1rem 4rem}
.oc{background:#fff;border:1.5px solid var(--line);border-radius:26px;padding:1rem;margin-bottom:.9rem;box-shadow:0 14px 30px -22px #4b2bd6;animation:rise .6s var(--out) backwards}
@keyframes rise{from{transform:translateY(24px);opacity:0}}
.oh{display:flex;justify-content:space-between;align-items:center;gap:.6rem;font-size:1.05rem}
.stt{font-size:.78rem;font-weight:800;padding:.25rem .65rem;border-radius:99px;background:#ffe9a8}
.s-new{background:#ffd0c8}.s-ready{background:#c9f2d3}.s-out{background:#cfe0ff}
.ad{margin-top:.5rem;font-weight:700}.mt{color:var(--mut);margin-top:.2rem;font-size:.92rem}
.oa{display:flex;gap:.45rem;flex-wrap:wrap;margin-top:.7rem}
.act{position:relative;display:inline-flex;align-items:center;gap:.4rem;padding:.6rem .9rem;border-radius:14px;background:var(--mist);font-weight:800;font-size:.9rem;text-decoration:none;color:var(--ink)}
.sts .mini{padding:.55rem .85rem;border-radius:12px}
.empty{text-align:center;color:var(--mut);padding:3rem 1rem}
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem}.hdr h2{font-size:1.4rem}
</style></head><body>
<header class="top sc"><a class="brand" href="/rider"><span class="crown">🛵</span>Rider</a>
<div class="tools"><span class="chip"><i class="d"></i>Live</span><a class="chip" href="/admin">Admin</a>
<form method="post" action="/rider/logout" style="display:inline"><input type="hidden" name="csrf" value="__CSRF__"><button class="chip">Log out</button></form></div></header>
<main class="wrap"><div class="hdr"><h2>Active orders</h2></div><div id="orders"></div></main>
<div class="scrim" id="scrim"></div>
<section class="sheet chatsheet" id="sh-chat" aria-label="Customer chat"><div class="ch"><button class="x" data-back aria-label="Close">←</button><span class="av">👤</span>
<div class="who"><b id="cn"></b><small>Customer</small></div><a class="x" id="ccall" aria-label="Call customer">📞</a></div><div class="chatbox" id="chatbox"></div></section>
<div id="toast"></div>
<script>
__CHATJS__
window.CSRF="__CSRF__";const CFG=__CFG__;
const $=s=>document.querySelector(s);
const rs=n=>CFG.cur+' '+Number(n).toLocaleString();
const LBL={new:'New',preparing:'Preparing',ready:'Ready',out:'On the way'};
const seen=id=>+(localStorage.getItem('rs_'+id)||0);
let orders=[],chat=null,known=new Set(),first=true,lastUn=0;
function beep(){try{const a=new (window.AudioContext||window.webkitAudioContext)(),o=a.createOscillator(),g=a.createGain();o.connect(g);g.connect(a.destination);o.frequency.value=880;g.gain.setValueAtTime(.15,a.currentTime);g.gain.exponentialRampToValueAtTime(.001,a.currentTime+.4);o.start();o.stop(a.currentTime+.4)}catch(e){}}
function card(o){const un=Math.max(0,o.nc-seen(o.id));
 return `<article class="oc" data-id="${o.id}"><div class="oh"><b>#${o.id} ${esc(o.name)}</b><span class="stt s-${o.status}">${LBL[o.status]||o.status}</span></div>
 <p class="ad">${o.mode==='pickup'?'Pickup at the shop':esc(o.address)}</p><p class="mt">${esc(o.items)} - ${rs(o.total)}${o.km?' - '+o.km+' km':''}</p>
 <div class="oa"><a class="act" href="tel:${esc(o.phone)}">📞 Call</a>${o.lat!=null?`<a class="act" target="_blank" rel="noopener" href="https://maps.google.com/?q=${o.lat},${o.lng}">📍 Map</a>`:''}<button class="act" data-chat="${o.id}">💬 Chat${un?`<span class="ub">${un}</span>`:''}</button></div>
 <div class="oa sts">${[['preparing','Preparing'],['ready','Ready'],['out','On the way'],['done','Done']].filter(s=>s[0]!==o.status&&!(o.mode==='pickup'&&s[0]==='out')).map(s=>`<button class="mini" data-st="${s[0]}">${s[1]}</button>`).join('')}</div></article>`}
async function load(){try{const r=await fetch('/rider/api/orders');if(r.status===403){location.reload();return}const j=await r.json();
 const un=j.orders.reduce((a,o)=>a+Math.max(0,o.nc-seen(o.id)),0);
 if(!first&&(j.orders.some(o=>!known.has(o.id))||un>lastUn)){beep();navigator.vibrate&&navigator.vibrate(120)}
 known=new Set(j.orders.map(o=>o.id));lastUn=un;first=false;orders=j.orders;
 if(!chat)$('#orders').innerHTML=orders.map(card).join('')||'<p class="empty">No active orders right now. New ones show up here.</p>'}catch(e){}}
function openChat(id){const o=orders.find(x=>x.id==id);$('#cn').textContent='#'+id+' '+(o?o.name:'');$('#ccall').href='tel:'+(o?o.phone:'');
 chat=chatView($('#chatbox'),{me:'r',
  fetch:async s=>{const r=await fetch(`/rider/api/chat?id=${id}&since=${s}`);if(!r.ok)throw 0;const j=await r.json();localStorage.setItem('rs_'+id,j.nc);return{msgs:j.msgs,closed:j.closed}},
  text:t=>post('/rider/api/send',{id,text:t}),
  voice:(b,d)=>{const f=new FormData();f.append('id',id);f.append('dur',d);f.append('audio',b,'voice.webm');return post('/rider/api/voice',f)}});
 document.getElementById('sh-chat').classList.add('open');document.body.classList.add('so');chat.on()}
function closeChat(){if(chat){chat.off();chat=null}document.getElementById('sh-chat').classList.remove('open');document.body.classList.remove('so');load()}
document.addEventListener('click',async e=>{const t=e.target;
 if(t.closest('[data-back]')||t.id==='scrim'){closeChat();return}
 const c=t.closest('[data-chat]');if(c){openChat(c.dataset.chat);return}
 const s=t.closest('[data-st]');if(s){const id=+s.closest('.oc').dataset.id;try{await post('/rider/api/status',{id,status:s.dataset.st});toast('Order #'+id+' updated');first=true;load()}catch(er){toast(er.message)}}});
load();setInterval(load,5000);
</script></body></html>"""

# ---------------------------------------------------------------- admin pages
ADMIN_LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin login</title>{{ fonts|safe }}
<style>{{ css|safe }}
body{display:grid;place-items:center;padding:1rem}
.box{position:relative;z-index:1;background:var(--paper);padding:2.2rem;border-radius:26px;width:min(390px,100%);box-shadow:0 30px 60px -24px #4b2bd6aa;animation:pop .8s var(--ease) both}
@keyframes pop{from{transform:scale(.5) rotate(-8deg);opacity:0}}
h1{color:var(--red);font-size:2rem;margin-bottom:.4rem}
input{width:100%;padding:.85rem;border-radius:12px;border:2px solid #0002;margin:1rem 0}
input:focus{border-color:var(--red);outline:0}
.err{background:#ffd9d4;padding:.6rem .8rem;border-radius:10px;margin-top:.8rem;animation:shake .4s}
@keyframes shake{25%{transform:translateX(-8px)}75%{transform:translateX(8px)}}
.key{font-size:3rem;display:inline-block;animation:wob 3s ease-in-out infinite}
@keyframes wob{50%{transform:rotate(14deg) scale(1.1)}}
</style></head><body><div class="bg-move"></div><div class="rain"></div>
<form class="box" method="post" action="{{ action }}">
<span class="key">🔐</span><h1>{{ title }}</h1><p>{{ blurb }}</p>
<input type="hidden" name="csrf" value="{{ csrf }}">
<input type="password" name="password" placeholder="Password" required autofocus autocomplete="current-password">
<button class="btn" style="width:100%">Sign in</button>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
</form><script>{{ rain|safe }}</script></body></html>"""

ADMIN_DASH = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin - {{ site }}</title>{{ fonts|safe }}
<style>{{ css|safe }}
body{background:var(--mist);color:var(--ink)}
.top{position:sticky;top:0;z-index:5;background:var(--red);color:#fff;display:flex;justify-content:space-between;align-items:center;padding:.9rem 1.2rem}
.top h1{color:var(--gold);font-size:1.3rem}
.wrap{max-width:1050px;margin:0 auto;padding:1.2rem}
.panel{background:#fff;border-radius:20px;padding:1.3rem;margin-bottom:1.3rem;box-shadow:0 5px 0 #0001;opacity:0;animation:up .7s var(--ease) forwards}
@keyframes up{from{transform:translateY(40px) scale(.97);opacity:0}to{opacity:1}}
.panel:nth-child(2){animation-delay:.08s}.panel:nth-child(3){animation-delay:.16s}.panel:nth-child(4){animation-delay:.24s}.panel:nth-child(5){animation-delay:.32s}.panel:nth-child(6){animation-delay:.4s}.panel:nth-child(7){animation-delay:.48s}
h2{color:var(--red);font-size:1.3rem;margin-bottom:.9rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem}
.stat{background:var(--paper);border-radius:16px;padding:1rem;transition:transform .3s var(--ease)}.stat:hover{transform:translateY(-5px) rotate(-1deg)}
.stat b{display:block;font-size:1.9rem;color:var(--red)}
.store{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}
.sw{position:relative;width:78px;height:40px;border-radius:99px;background:#999;transition:background .3s;padding:0}
.sw::after{content:"";position:absolute;top:4px;left:4px;width:32px;height:32px;border-radius:50%;background:#fff;transition:transform .4s var(--ease)}
.sw.on{background:#2fbf5b}.sw.on::after{transform:translateX(38px)}
.sw.big{width:120px;height:58px}.sw.big::after{width:48px;height:48px}.sw.big.on::after{transform:translateX(62px)}
table{width:100%;border-collapse:collapse}td,th{padding:.6rem .4rem;text-align:left;border-bottom:1px solid #0001;vertical-align:middle}
tbody tr{animation:up .5s var(--ease) both}
.scroll{overflow-x:auto}
input,select{padding:.6rem .7rem;border-radius:10px;border:2px solid #0002;background:#fff;max-width:100%}
input:focus{border-color:var(--red);outline:0}
.th{width:52px;height:52px;object-fit:cover;border-radius:10px}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.7rem;align-items:center}
.tag{padding:.2rem .6rem;border-radius:99px;font-size:.8rem;font-weight:700;background:#ffe9a8}
.tag.new{background:#ffd0c8}.tag.ready{background:#c9f2d3}.tag.done{background:#ddd}.tag.out{background:#cfe0ff}
.order{border:2px solid #0001;border-radius:14px;padding:.9rem;margin-bottom:.8rem;animation:up .5s var(--ease) both}
.order.new{border-color:var(--red);animation:up .5s var(--ease) both,glow 2s ease-in-out infinite}
@keyframes glow{50%{box-shadow:0 0 0 6px #d62d2022}}
.msg{background:var(--gold);padding:.7rem 1rem;border-radius:12px;margin-bottom:1rem;font-weight:700;animation:up .5s var(--ease)}
.tk{border-left:5px solid var(--gold);background:var(--paper);padding:.7rem .9rem;border-radius:0 12px 12px 0;margin-bottom:.6rem}
</style></head><body>
<div class="top"><h1 class="disp">{{ site }} admin</h1>
<span><a href="/" style="color:#fff;margin-right:1rem">View site</a><a href="/rider" style="color:#fff;margin-right:1rem">Rider console</a>
<form method="post" action="/admin/logout" style="display:inline"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="mini">Log out</button></form></span></div>
<div class="wrap">
{% with m = request.args.get('m') %}{% if m %}<div class="msg">{{ m }}</div>{% endif %}{% endwith %}

<section class="panel"><h2>Today</h2><div class="stats">
<div class="stat"><b class="cnt" data-n="{{ new_orders }}">0</b>New orders</div>
<div class="stat"><b class="cnt" data-n="{{ d.orders|length }}">0</b>Total orders</div>
<div class="stat"><b class="cnt" data-n="{{ revenue }}">0</b>Revenue ({{ cur }})</div>
<div class="stat"><b class="cnt" data-n="{{ out_count }}">0</b>Sold out</div>
<div class="stat"><b class="cnt" data-n="{{ d.tickets|length }}">0</b>Support messages</div></div></section>

<section class="panel"><div class="store"><div><h2 style="margin:0">Store status</h2>
<p>{{ 'Open: customers can order.' if d.open else 'Closed: ordering is paused on the site.' }}</p></div>
<form method="post" action="/admin/store"><input type="hidden" name="csrf" value="{{ csrf }}">
<button class="sw big {{ 'on' if d.open }}" aria-label="Toggle store open or closed"></button></form></div></section>

<section class="panel"><h2>Orders</h2>
{% for o in d.orders|reverse %}<div class="order {{ o.status }}" style="animation-delay:{{ loop.index0 * 0.05 }}s">
<b>#{{ o.id }} - {{ o.name }}</b> <span class="tag {{ o.status }}">{{ o.status }}</span>
<div>{{ o.phone }}{% if o.address %} - {{ o.address }}{% endif %}</div>
<div>{{ 'Pickup' if o.get('mode') == 'pickup' else 'Delivery' }}{% if o.get('km') %} - {{ o.km }} km{% endif %} - fee {{ cur }} {{ o.get('fee', 0) }}
{% if o.get('lat') is not none %} - <a href="https://maps.google.com/?q={{ o.lat }},{{ o.lng }}" target="_blank" rel="noopener">Map</a>{% endif %}</div>
<div>{% for i in o['items'] %}{{ i.qty }}x {{ i.name }}{{ ', ' if not loop.last }}{% endfor %} - <b>{{ cur }} {{ o.total }}</b></div>
{% if o.note %}<div><i>Note: {{ o.note }}</i></div>{% endif %}
<form method="post" action="/admin/order/{{ o.id }}" style="margin-top:.6rem;display:flex;gap:.4rem;flex-wrap:wrap">
<input type="hidden" name="csrf" value="{{ csrf }}">
{% for s in ['new','preparing','ready','out','done','cancelled'] %}{% if s != o.status %}<button class="mini {{ 'red' if s=='cancelled' }}" name="status" value="{{ s }}">{{ s }}</button>{% endif %}{% endfor %}
</form></div>{% else %}<p>No orders yet. They'll appear here and in Discord.</p>{% endfor %}
{% if d.orders %}<form method="post" action="/admin/orders/clear" onsubmit="return confirm('Delete all orders?')"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="mini red">Clear all orders</button></form>{% endif %}
</section>

<section class="panel"><h2>Menu items</h2><div class="scroll"><table><thead><tr><th>Photo</th><th>Name</th><th>Price</th><th>In stock</th><th>Coming soon</th><th>Image</th><th></th></tr></thead><tbody>
{% for it in d['items'] %}<tr style="animation-delay:{{ loop.index0 * 0.06 }}s">
<td>{% if it.image %}<img class="th" src="{{ up_url(it.image) }}" alt="">{% else %}<span style="font-size:1.8rem">{{ it.emoji }}</span>{% endif %}</td><td>{{ it.name }}</td>
<td><form method="post" action="/admin/item/{{ it.id }}/price" style="display:flex;gap:.4rem"><input type="hidden" name="csrf" value="{{ csrf }}">
<input name="price" type="number" min="1" max="100000" value="{{ it.price }}" style="width:90px"><button class="mini">Save</button></form></td>
<td><form method="post" action="/admin/item/{{ it.id }}/stock"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="sw {{ 'on' if it.stock }}" aria-label="Toggle stock for {{ it.name }}"></button></form></td>
<td><form method="post" action="/admin/item/{{ it.id }}/soon"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="sw {{ 'on' if it.soon }}" aria-label="Toggle coming soon for {{ it.name }}"></button></form></td>
<td><form method="post" enctype="multipart/form-data" action="/admin/item/{{ it.id }}/image" style="display:flex;gap:.4rem;align-items:center"><input type="hidden" name="csrf" value="{{ csrf }}">
<input type="file" name="image" accept="image/*" style="max-width:150px"><button class="mini">Upload</button>
{% if it.image %}<button class="mini red" formaction="/admin/item/{{ it.id }}/rmimage" formnovalidate>Remove</button>{% endif %}</form></td>
<td><form method="post" action="/admin/item/{{ it.id }}/delete" onsubmit="return confirm('Delete this item?')"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="mini red">Delete</button></form></td></tr>{% endfor %}
</tbody></table></div></section>

<section class="panel"><h2>Add an item</h2>
<form method="post" action="/admin/item/add" enctype="multipart/form-data" class="form"><input type="hidden" name="csrf" value="{{ csrf }}">
<input name="name" placeholder="Name" required maxlength="50"><input name="price" type="number" min="1" max="100000" placeholder="Price ({{ cur }})" required>
<input name="emoji" placeholder="Emoji (e.g. 🍔)" maxlength="4"><input name="desc" placeholder="Short description" maxlength="90">
<input type="file" name="image" accept="image/*"><label><input type="checkbox" name="soon"> Mark as coming soon</label>
<button class="btn">Add to menu</button></form></section>

<section class="panel"><h2>Support messages</h2>
{% for t in d.tickets|reverse %}<div class="tk"><b>{{ t.name }}</b> - {{ t.contact }}<br>{{ t.message }}</div>{% else %}<p>No messages yet.</p>{% endfor %}
{% if d.tickets %}<form method="post" action="/admin/tickets/clear"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="mini red">Clear messages</button></form>{% endif %}</section>
</div>
<script>
document.querySelectorAll('.cnt').forEach(e=>{const n=+e.dataset.n,t0=performance.now();
(function s(t){const p=Math.min((t-t0)/1000,1);e.textContent=Math.round(n*(1-Math.pow(1-p,3))).toLocaleString();if(p<1)requestAnimationFrame(s)})(t0)});
setInterval(()=>{if(!document.activeElement||document.activeElement===document.body)location.reload()},30000);
</script></body></html>"""

# ---------------------------------------------------------------- public routes
@app.route("/")
def index():
    cj = json.dumps(public_cfg()).replace("<", "\\u003c")
    return (MENU_PAGE.replace("__NAME__", html.escape(SITE)).replace("__FONTS__", FONTS)
            .replace("__CSS__", BASE_CSS + CHAT_CSS).replace("__CHATJS__", CHAT_JS).replace("__CFG__", cj))


@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name, max_age=86400)


@app.route("/api/menu")
def api_menu():
    d = load()
    r = jsonify(open=d["open"], items=[pub(i) for i in d["items"]])
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/api/order", methods=["POST"])
def api_order():
    data = request.get_json(silent=True) or {}
    name, phone = clean(data.get("name"), 60), clean(data.get("phone"), 20)
    address, note = clean(data.get("address"), 200), clean(data.get("note"), 150)
    mode = "pickup" if data.get("mode") == "pickup" else "delivery"
    if not (name and phone):
        return jsonify(error="Please add your name and phone number."), 400
    if sum(c.isdigit() for c in phone) < 7:
        return jsonify(error="Please enter a valid phone number."), 400
    km, lat, lng, fee = 0.0, None, None, 0
    if mode == "delivery":
        if not address:
            return jsonify(error="Please add your delivery address."), 400
        if S_LAT or S_LNG:
            try:
                la, lo = float(data.get("lat")), float(data.get("lng"))
                if not (math.isfinite(la) and math.isfinite(lo) and abs(la) <= 90 and abs(lo) <= 180):
                    raise ValueError
            except (TypeError, ValueError):
                return jsonify(error="Please pick your delivery location on the map."), 400
            lat, lng = la, lo
            km = round(hav(S_LAT, S_LNG, la, lo) * ROAD, 1)
            if km > D_MAX:
                return jsonify(error=f"Sorry, we only deliver up to {D_MAX:g} km."), 400
            fee = int(round(D_BASE + D_KM * km))
        else:
            fee = int(round(D_BASE))   # no shop coordinates set: flat delivery fee
    with LOCK:
        d = load()
        if not d["open"]:
            return jsonify(error="Sorry, we're closed right now."), 400
        lines, sub = [], 0
        for row in (data.get("items") or [])[:30]:
            try:
                iid, qty = int(row.get("id")), int(row.get("qty"))
            except (TypeError, ValueError, AttributeError):
                return jsonify(error="Invalid cart."), 400
            it = next((i for i in d["items"] if i["id"] == iid), None)
            if not it or not 1 <= qty <= 20:
                return jsonify(error="An item in your cart is no longer available."), 400
            if it.get("soon"):
                return jsonify(error=f"{it['name']} is coming soon."), 400
            if not it.get("stock", True):
                return jsonify(error=f"{it['name']} just sold out."), 400
            lines.append({"name": it["name"], "qty": qty, "price": it["price"]})
            sub += it["price"] * qty
        if not lines:
            return jsonify(error="Your cart is empty."), 400
        oid = d["next_order"]
        d["next_order"] += 1
        o = {"id": oid, "name": name, "phone": phone, "address": address, "note": note, "items": lines,
             "mode": mode, "km": km, "lat": lat, "lng": lng, "subtotal": sub, "fee": fee,
             "total": sub + fee, "status": "new", "time": int(time.time()),
             "token": secrets.token_hex(12), "chat": []}
        d["orders"].append(o)
        save(d)
    post_webhook(cfg("DISCORD_ORDERS_WEBHOOK"), order_payload(o))
    return jsonify(token=o["token"], **track_payload(o))


SUP_HITS = {}


@app.route("/api/support", methods=["POST"])
def api_support():
    data = request.get_json(silent=True) or {}
    name, contact, msg = clean(data.get("name"), 60), clean(data.get("contact"), 60), clean(data.get("message"), 800)
    if not name or not contact or len(msg) < 5:
        return jsonify(error="Please fill in your name, contact and message."), 400
    now, k = time.time(), ip()
    hits = [t for t in SUP_HITS.get(k, []) if now - t < 120]
    if len(hits) >= 3:
        return jsonify(error="Too many messages. Please wait a minute."), 429
    SUP_HITS[k] = hits + [now]
    with LOCK:
        d = load()
        d["tickets"].append({"name": name, "contact": contact, "message": msg, "time": int(now)})
        d["tickets"] = d["tickets"][-100:]
        save(d)
    post_webhook(cfg("DISCORD_SUPPORT_WEBHOOK") or cfg("DISCORD_ORDERS_WEBHOOK"),
                 embed("Support message", [("From", name, True), ("Contact", contact, True), ("Message", msg, False)], 0xD62D20))
    return jsonify(ok=True)

# ---------------------------------------------------------------- admin routes
ATTEMPTS = {}


@app.route("/admin")
def admin():
    if not is_admin():
        return render_template_string(ADMIN_LOGIN, css=BASE_CSS, fonts=FONTS, rain=RAIN_JS, site=SITE,
                                      csrf=csrf(), error=request.args.get("e"), action="/admin/login",
                                      title="Staff only", blurb="Enter the admin password to manage " + SITE + ".")
    d = load()
    return render_template_string(
        ADMIN_DASH, d=d, css=BASE_CSS, fonts=FONTS, csrf=csrf(), site=SITE, cur=CUR,
        new_orders=sum(o["status"] == "new" for o in d["orders"]),
        revenue=sum(o["total"] for o in d["orders"] if o["status"] != "cancelled"),
        out_count=sum(not i.get("stock", True) for i in d["items"]))


@app.route("/admin/login", methods=["POST"])
def admin_login():
    now, key = time.time(), ip()
    n, first = ATTEMPTS.get(key, (0, now))
    if now - first > 300:
        n, first = 0, now
    if n >= 5:
        return redirect("/admin?e=Too many attempts. Wait 5 minutes.")
    if hmac.compare_digest(request.form.get("password", "").encode(), str(ADMIN_PASSWORD).encode()):
        ATTEMPTS.pop(key, None)
        session.clear()
        session["admin"] = True
        return redirect("/admin")
    ATTEMPTS[key] = (n + 1, first)
    return redirect("/admin?e=Wrong password.")


def guard():
    if not is_admin():
        abort(403)


def done(msg):
    return redirect(f"/admin?m={msg}")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect("/admin")


@app.route("/admin/store", methods=["POST"])
def admin_store():
    guard()
    with LOCK:
        d = load()
        d["open"] = not d["open"]
        save(d)
    return done("Store is now " + ("open." if d["open"] else "closed."))


@app.route("/admin/item/add", methods=["POST"])
def admin_add():
    guard()
    name = clean(request.form.get("name"), 50)
    try:
        price = int(request.form.get("price", ""))
    except ValueError:
        price = 0
    if not name or not 1 <= price <= 100000:
        return done("Enter a name and a valid price.")
    img = ""
    f = request.files.get("image")
    if f and f.filename:
        try:
            img = save_image(f)
        except ValueError as e:
            return done(str(e))
    with LOCK:
        d = load()
        d["items"].append({"id": d["next_item"], "name": name, "price": price,
                           "emoji": clean(request.form.get("emoji"), 4) or "🍽️",
                           "desc": clean(request.form.get("desc"), 90), "stock": True,
                           "soon": bool(request.form.get("soon")), "image": img})
        d["next_item"] += 1
        save(d)
    return done(f"Added {name}.")


@app.route("/admin/item/<int:iid>/<action>", methods=["POST"])
def admin_item(iid, action):
    guard()
    new_img = None
    if action == "image":
        try:
            new_img = save_image(request.files.get("image"))
        except ValueError as e:
            return done(str(e))
    with LOCK:
        d = load()
        it = next((i for i in d["items"] if i["id"] == iid), None)
        if not it:
            if new_img:
                drop_image(new_img)
            return done("Item not found.")
        if action == "stock":
            it["stock"] = not it.get("stock", True)
            msg = f"{it['name']} is now {'in stock' if it['stock'] else 'sold out'}."
        elif action == "soon":
            it["soon"] = not it.get("soon", False)
            msg = f"{it['name']}: coming soon {'on' if it['soon'] else 'off'}."
        elif action == "price":
            try:
                p = int(request.form.get("price", ""))
            except ValueError:
                p = 0
            if not 1 <= p <= 100000:
                return done("Invalid price.")
            it["price"] = p
            msg = f"{it['name']} now costs {CUR} {p}."
        elif action == "image":
            drop_image(it.get("image"))
            it["image"] = new_img
            msg = f"Photo updated for {it['name']}."
        elif action == "rmimage":
            drop_image(it.get("image"))
            it["image"] = ""
            msg = f"Photo removed from {it['name']}."
        elif action == "delete":
            drop_image(it.get("image"))
            d["items"].remove(it)
            msg = f"Deleted {it['name']}."
        else:
            abort(404)
        save(d)
    return done(msg)


@app.route("/admin/order/<int:oid>", methods=["POST"])
def admin_order(oid):
    guard()
    s = request.form.get("status")
    if s not in ("new", "preparing", "ready", "out", "done", "cancelled"):
        abort(400)
    with LOCK:
        d = load()
        for o in d["orders"]:
            if o["id"] == oid:
                o["status"] = s
        save(d)
    return done(f"Order #{oid} marked {s}.")


@app.route("/admin/orders/clear", methods=["POST"])
def admin_clear():
    guard()
    with LOCK:
        d = load()
        d["orders"] = []
        save(d)
    return done("All orders cleared.")


@app.route("/admin/tickets/clear", methods=["POST"])
def admin_tickets_clear():
    guard()
    with LOCK:
        d = load()
        d["tickets"] = []
        save(d)
    return done("Support messages cleared.")


# ---------------------------------------------------------------- chat, voice, rider
CHAT_HITS = {}


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def find_order(d, oid):
    return next((o for o in d["orders"] if o["id"] == oid), None)


def chat_ok(o):
    return o["status"] not in ("done", "cancelled")


def add_msg(o, who, kind, text="", url="", dur=0):
    o.setdefault("chat", []).append({"f": who, "k": kind, "x": text, "u": url, "d": dur, "t": int(time.time())})


def msg_out(m):
    return {"f": m["f"], "k": m["k"], "x": m.get("x", ""), "u": up_url(m["u"]) if m.get("u") else "",
            "d": m.get("d", 0), "t": m["t"]}


def track_payload(o, since=0):
    chat = o.get("chat", [])
    return dict(id=o["id"], status=o["status"], mode=o.get("mode", "delivery"), total=o["total"],
                items=[f"{i['qty']}x {i['name']}" for i in o["items"]],
                msgs=[msg_out(m) for m in chat[since:]], n=len(chat),
                nr=sum(m["f"] == "r" for m in chat), canChat=chat_ok(o))


def save_audio(f):
    if not f or not f.filename:
        raise ValueError("No recording received.")
    raw = f.read(2500001)
    if len(raw) > 2500000:
        raise ValueError("Recording is too large. Keep it under a minute.")
    if raw.startswith(b"\x1a\x45\xdf\xa3"):
        ext = ".webm"
    elif raw.startswith(b"OggS"):
        ext = ".ogg"
    elif raw[4:8] == b"ftyp":
        ext = ".m4a"
    elif raw.startswith(b"ID3") or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        ext = ".mp3"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        ext = ".wav"
    else:
        raise ValueError("That audio format is not supported.")
    return store_file("v_" + secrets.token_hex(10) + ext, raw)


def chat_rate():
    now, k = time.time(), ip()
    h = [t for t in CHAT_HITS.get(k, []) if now - t < 60]
    if len(h) >= 25:
        return False
    CHAT_HITS[k] = h + [now]
    return True


def cust_order(d, oid, token):
    o = find_order(d, to_int(oid))
    if o and o.get("token") and hmac.compare_digest(o["token"].encode(), str(token or "").encode()):
        return o
    return None


def notify_staff(o, text, voice=None):
    f = [("Customer", o["name"], True), ("Phone", o["phone"], True)]
    f.append(("Voice note", (voice if voice.startswith("http") else request.host_url.rstrip("/") + "/uploads/" + voice), False) if voice else ("Message", text, False))
    post_webhook(cfg("DISCORD_ORDERS_WEBHOOK"), embed(f"Chat from order #{o['id']}", f, 0x4B2BD6))


def put_msg(o, who, kind, text="", url="", dur=0):
    if not chat_ok(o):
        return "This order is finished, so the chat is closed."
    if len(o.get("chat", [])) >= 300:
        return "Chat limit reached for this order."
    add_msg(o, who, kind, text, url, dur)
    return None


@app.route("/api/track")
def api_track():
    d = load()
    o = cust_order(d, request.args.get("id"), request.args.get("token"))
    if not o:
        abort(404)
    r = jsonify(**track_payload(o, max(0, to_int(request.args.get("since")) or 0)))
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    text = clean(data.get("text"), 500)
    if not text:
        return jsonify(error="Type a message first."), 400
    if not chat_rate():
        return jsonify(error="Slow down a little."), 429
    with LOCK:
        d = load()
        o = cust_order(d, data.get("id"), data.get("token"))
        if not o:
            return jsonify(error="Order not found."), 404
        err = put_msg(o, "c", "t", text)
        if err:
            return jsonify(error=err), 400
        save(d)
    notify_staff(o, text)
    return jsonify(ok=True)


@app.route("/api/chat/voice", methods=["POST"])
def api_chat_voice():
    if not chat_rate():
        return jsonify(error="Slow down a little."), 429
    with LOCK:
        d = load()
        o = cust_order(d, request.form.get("id"), request.form.get("token"))
        if not o:
            return jsonify(error="Order not found."), 404
        if not chat_ok(o):
            return jsonify(error="This order is finished, so the chat is closed."), 400
        try:
            name = save_audio(request.files.get("audio"))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        err = put_msg(o, "c", "v", url=name, dur=max(0, min(120, to_int(request.form.get("dur")) or 0)))
        if err:
            drop_image(name)
            return jsonify(error=err), 400
        save(d)
    notify_staff(o, "", voice=name)
    return jsonify(ok=True)


def rider_ok():
    return session.get("rider") is True or is_admin()


def rguard():
    if not rider_ok():
        abort(403)


@app.route("/rider")
def rider():
    if not rider_ok():
        return render_template_string(ADMIN_LOGIN, css=BASE_CSS, fonts=FONTS, rain=RAIN_JS, site=SITE, csrf=csrf(),
                                      error=request.args.get("e"), action="/rider/login", title="Rider login",
                                      blurb="Enter the rider password to see your orders.")
    cj = json.dumps(public_cfg()).replace("<", "\\u003c")
    return (RIDER_PAGE.replace("__NAME__", html.escape(SITE)).replace("__FONTS__", FONTS)
            .replace("__CSS__", BASE_CSS + CHAT_CSS).replace("__CHATJS__", CHAT_JS)
            .replace("__CFG__", cj).replace("__CSRF__", csrf()))


@app.route("/rider/login", methods=["POST"])
def rider_login():
    now, key = time.time(), "r" + ip()
    n, first = ATTEMPTS.get(key, (0, now))
    if now - first > 300:
        n, first = 0, now
    if n >= 5:
        return redirect("/rider?e=Too many attempts. Wait 5 minutes.")
    if RIDER_PASSWORD and hmac.compare_digest(request.form.get("password", "").encode(), RIDER_PASSWORD.encode()):
        ATTEMPTS.pop(key, None)
        session.clear()
        session["rider"] = True
        return redirect("/rider")
    ATTEMPTS[key] = (n + 1, first)
    return redirect("/rider?e=Wrong password.")


@app.route("/rider/logout", methods=["POST"])
def rider_logout():
    session.clear()
    return redirect("/rider")


@app.route("/rider/api/orders")
def rider_orders():
    rguard()
    d = load()
    out = []
    for o in reversed(d["orders"][-80:]):
        if o["status"] in ("done", "cancelled"):
            continue
        out.append({"id": o["id"], "name": o["name"], "phone": o["phone"], "address": o["address"], "mode": o.get("mode", "delivery"),
                    "km": o.get("km") or 0, "total": o["total"], "status": o["status"], "lat": o.get("lat"), "lng": o.get("lng"),
                    "items": ", ".join(f"{i['qty']}x {i['name']}" for i in o["items"]),
                    "nc": sum(m["f"] == "c" for m in o.get("chat", []))})
    r = jsonify(orders=out)
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/rider/api/chat")
def rider_chat():
    rguard()
    o = find_order(load(), to_int(request.args.get("id")))
    if not o:
        abort(404)
    chat = o.get("chat", [])
    since = max(0, to_int(request.args.get("since")) or 0)
    r = jsonify(msgs=[msg_out(m) for m in chat[since:]], n=len(chat), nc=sum(m["f"] == "c" for m in chat), closed=not chat_ok(o))
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/rider/api/send", methods=["POST"])
def rider_send():
    rguard()
    data = request.get_json(silent=True) or {}
    text = clean(data.get("text"), 500)
    if not text:
        return jsonify(error="Type a message first."), 400
    with LOCK:
        d = load()
        o = find_order(d, to_int(data.get("id")))
        if not o:
            return jsonify(error="Order not found."), 404
        err = put_msg(o, "r", "t", text)
        if err:
            return jsonify(error=err), 400
        save(d)
    return jsonify(ok=True)


@app.route("/rider/api/voice", methods=["POST"])
def rider_voice():
    rguard()
    with LOCK:
        d = load()
        o = find_order(d, to_int(request.form.get("id")))
        if not o:
            return jsonify(error="Order not found."), 404
        if not chat_ok(o):
            return jsonify(error="This order is finished, so the chat is closed."), 400
        try:
            name = save_audio(request.files.get("audio"))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        err = put_msg(o, "r", "v", url=name, dur=max(0, min(120, to_int(request.form.get("dur")) or 0)))
        if err:
            drop_image(name)
            return jsonify(error=err), 400
        save(d)
    return jsonify(ok=True)


@app.route("/rider/api/status", methods=["POST"])
def rider_status():
    rguard()
    data = request.get_json(silent=True) or {}
    if data.get("status") not in ("preparing", "ready", "out", "done"):
        return jsonify(error="Invalid status."), 400
    with LOCK:
        d = load()
        o = find_order(d, to_int(data.get("id")))
        if not o:
            return jsonify(error="Order not found."), 404
        o["status"] = data["status"]
        save(d)
    return jsonify(ok=True)



if __name__ == "__main__":
    port = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or cfg("PORT", 20726))
    app.run(host=cfg("HOST", "0.0.0.0"), port=port)
