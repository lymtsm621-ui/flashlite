"""
⚡ Flash-Lite — التطبيق الكامل
المرحلة النهائية: حساب + محادثات + بث مباشر + صوت عربي
"""
import json
import os
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template_string, request, jsonify,
                   Response, stream_with_context, g)
from flask_cors import CORS
import auth

# ============================================================
# ⚙️ الإعدادات
# ============================================================
GOOGLE_CLIENT_ID = "694239714110-vctdhk0pdjq26lr4nm089bo6iq3l48ke.apps.googleusercontent.com"
API_KEY = os.environ.get(
    "GEMINI_API_KEY",
    "AQ.Ab8RN6Iu_GJMRfxwMvVy9rnI23MlP3sh2L0HANumgooZ18OQOg"
)
DB_PATH = os.path.join(os.path.dirname(__file__), "flashlite.db")

MODEL_QUEUE = [
    "gemini-2.0-flash-exp",
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-1.5-flash-latest",
    "gemini-1.5-flash",
]
AVAILABLE_MODEL = None

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})
auth.set_db_path(DB_PATH)
auth.set_google_client_id(GOOGLE_CLIENT_ID)


# ============================================================
# 🗄️ قاعدة البيانات
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        email TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        mode TEXT DEFAULT 'normal',
        pinned INTEGER DEFAULT 0,
        folder TEXT DEFAULT 'default',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        model TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        expires_at TIMESTAMP NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        theme TEXT DEFAULT 'light',
        auto_tts INTEGER DEFAULT 0,
        tts_rate REAL DEFAULT 1.0,
        default_mode TEXT DEFAULT 'normal',
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)""")
    conn.commit()
    conn.close()


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


# ============================================================
# 🔍 اكتشاف الموديل
# ============================================================
def detect_best_model():
    global AVAILABLE_MODEL
    if AVAILABLE_MODEL:
        return AVAILABLE_MODEL
    print("Testing Gemini models...")
    for model in MODEL_QUEUE:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}"
            payload = {"contents": [{"parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 5}}
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                          headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                json.loads(r.read().decode())
                AVAILABLE_MODEL = model
                print(f"OK: {model}")
                return model
        except Exception as e:
            print(f"FAIL {model}: {str(e)[:60]}")
    return None


# ============================================================
# 🔐 حماية
# ============================================================
def require_user(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = auth.get_current_user()
        if not user:
            return jsonify({"error": "يجب تسجيل الدخول"}), 401
        g.user = user
        return f(*args, **kwargs)
    return wrapper


app.register_blueprint(auth.auth_bp)


# ============================================================
# 💬 APIs المحادثات
# ============================================================
@app.route('/api/chats', methods=['GET'])
@require_user
def list_chats():
    db = get_db()
    rows = db.execute("""
        SELECT c.*, 
            (SELECT COUNT(*) FROM messages WHERE chat_id = c.id) as msg_count,
            (SELECT content FROM messages WHERE chat_id = c.id ORDER BY id DESC LIMIT 1) as last_msg
        FROM chats c WHERE user_id = ?
        ORDER BY pinned DESC, updated_at DESC
    """, (g.user['id'],)).fetchall()
    return jsonify({"chats": [dict(r) for r in rows]})


@app.route('/api/chats', methods=['POST'])
@require_user
def create_chat():
    data = request.get_json() or {}
    title = (data.get('title') or 'محادثة جديدة').strip()[:80]
    db = get_db()
    cur = db.execute(
        "INSERT INTO chats (user_id, title) VALUES (?, ?)",
        (g.user['id'], title)
    )
    db.commit()
    chat_id = cur.lastrowid
    return jsonify({"id": chat_id, "title": title})


@app.route('/api/chats/<int:chat_id>', methods=['GET'])
@require_user
def get_chat(chat_id):
    db = get_db()
    chat = db.execute(
        "SELECT * FROM chats WHERE id = ? AND user_id = ?",
        (chat_id, g.user['id'])
    ).fetchone()
    if not chat:
        return jsonify({"error": "غير موجود"}), 404
    msgs = db.execute(
        "SELECT id, role, content, created_at FROM messages WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,)
    ).fetchall()
    return jsonify({"chat": dict(chat), "messages": [dict(m) for m in msgs]})


@app.route('/api/chats/<int:chat_id>', methods=['DELETE'])
@require_user
def delete_chat(chat_id):
    db = get_db()
    db.execute("DELETE FROM chats WHERE id = ? AND user_id = ?", (chat_id, g.user['id']))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/chats/<int:chat_id>/pin', methods=['POST'])
@require_user
def pin_chat(chat_id):
    db = get_db()
    chat = db.execute(
        "SELECT pinned FROM chats WHERE id = ? AND user_id = ?",
        (chat_id, g.user['id'])
    ).fetchone()
    if not chat:
        return jsonify({"error": "غير موجود"}), 404
    new_state = 0 if chat['pinned'] else 1
    db.execute("UPDATE chats SET pinned = ? WHERE id = ?", (new_state, chat_id))
    db.commit()
    return jsonify({"pinned": new_state})


@app.route('/api/chats/<int:chat_id>/rename', methods=['POST'])
@require_user
def rename_chat(chat_id):
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()[:80]
    if not title:
        return jsonify({"error": "عنوان فارغ"}), 400
    db = get_db()
    db.execute(
        "UPDATE chats SET title = ? WHERE id = ? AND user_id = ?",
        (title, chat_id, g.user['id'])
    )
    db.commit()
    return jsonify({"success": True})


# ============================================================
# 🌐 الصفحة الرئيسية
# ============================================================
HOME_HTML = r"""
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Flash-Lite | معتصم علي</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css" id="hljs-theme">
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/11.1.1/marked.min.js"></script><script>
function getTok(){
  try{
    if(typeof tk !== "undefined" && tk) return tk;
    if(typeof token !== "undefined" && token) return token;
    return localStorage.getItem("fl_token") || localStorage.getItem("fl_tk") || "";
  }catch(e){
    return localStorage.getItem("fl_token") || localStorage.getItem("fl_tk") || "";
  }
}

var currentModel = "Flash-Lite";

function openMega(id){ document.getElementById(id).classList.add('on'); }
function closeMega(id){ document.getElementById(id).classList.remove('on'); }
document.addEventListener('click', function(e){
  if(e.target.classList.contains('mega-modal')) e.target.classList.remove('on');
});

function setAccent(a1, a2, el){
  document.documentElement.style.setProperty('--accent', a1);
  document.documentElement.style.setProperty('--accent-2', a2);
  localStorage.setItem('fl_accent1', a1);
  localStorage.setItem('fl_accent2', a2);
  document.querySelectorAll('.theme-dot').forEach(function(d){ d.classList.remove('sel'); });
  if(el) el.classList.add('sel');
  st2('تم تغيير اللون','ok');
}
function loadAccent(){
  var a1 = localStorage.getItem('fl_accent1');
  var a2 = localStorage.getItem('fl_accent2');
  if(a1 && a2){
    document.documentElement.style.setProperty('--accent', a1);
    document.documentElement.style.setProperty('--accent-2', a2);
  }
}

function savePersona(){
  var v = document.getElementById('personaSelect').value;
  localStorage.setItem('fl_persona', v);
  st2(v ? 'شخصية: ' + v : 'افتراضي','ok');
}
function loadPersona(){
  var v = localStorage.getItem('fl_persona') || '';
  var s = document.getElementById('personaSelect');
  if(s) s.value = v;
}

async function showStats(){
  openMega('statsModal');
  var box = document.getElementById('statsContent');
  box.innerHTML = '<div class="stat-row"><span>جاري التحميل...</span></div>';
  try{
    var r = await fetch('/api/chats', {headers:{'Authorization':'Bearer '+getTok()}});
    var d = await r.json();
    var cs = d.chats || [];
    var tm = cs.reduce(function(a,x){ return a + (x.msg_count||0); }, 0);
    var pin = cs.filter(function(x){ return x.pinned; }).length;
    var model = "Flash-Lite";
    try{
      var rs = await fetch('/api/status');
      var ds = await rs.json();
      if(ds && ds.model) model = ds.model;
      currentModel = model;
    }catch(e){}
    var today = new Date().toISOString().split('T')[0];
    var td = cs.filter(function(x){ return (x.updated_at||'').indexOf(today) === 0; }).length;
    box.innerHTML =
      '<div class="stat-row"><span>💬 المحادثات</span><b>' + cs.length + '</b></div>' +
      '<div class="stat-row"><span>✉️ الرسائل</span><b>' + tm + '</b></div>' +
      '<div class="stat-row"><span>📌 مثبتة</span><b>' + pin + '</b></div>' +
      '<div class="stat-row"><span>📅 اليوم</span><b>' + td + '</b></div>' +
      '<div class="stat-row"><span>⚡ الموديل</span><b>' + model + '</b></div>';
  }catch(e){ box.innerHTML = '<div class="stat-row"><span style="color:#ef4444">فشل: '+ (e.message||e) +'</span></div>'; }
}

async function summarizeChat(){
  if(!cid){ st2('افتح محادثة أولاً','er'); return; }
  st2('⏳ جاري التلخيص...','info');
  try{
    var r = await fetch('/api/chats/' + cid, {headers:{'Authorization':'Bearer '+getTok()}});
    var d = await r.json();
    var ms = d.messages || [];
    if(ms.length < 2){ st2('المحادثة قصيرة','er'); return; }
    var t = ms.map(function(m){ return m.role + ': ' + m.content; }).join('\n\n').slice(0, 6000);
    var inp = document.getElementById('ui');
    inp.value = 'لخص هذه المحادثة في 5 نقاط رئيسية:\n\n' + t;
    sm2();
  }catch(e){ st2('فشل','er'); }
}

function openChatSearch(){
  document.getElementById('chatSearch').classList.add('on');
  document.getElementById('chatSearchInput').focus();
}
function closeChatSearch(){
  document.getElementById('chatSearch').classList.remove('on');
  document.getElementById('chatSearchInput').value = '';
  document.getElementById('chatSearchCount').textContent = '';
  document.querySelectorAll('.msg').forEach(function(m){ m.style.outline = ''; });
}
function doChatSearch(q){
  document.querySelectorAll('.msg').forEach(function(m){ m.style.outline = ''; });
  if(!q) return;
  var found = [];
  document.querySelectorAll('.msg').forEach(function(m){
    if(m.textContent.toLowerCase().indexOf(q.toLowerCase()) > -1){
      m.style.outline = '2px solid var(--accent)';
      m.style.outlineOffset = '4px';
      found.push(m);
    }
  });
  document.getElementById('chatSearchCount').textContent = found.length + ' نتيجة';
  if(found.length) found[0].scrollIntoView({behavior:'smooth', block:'center'});
}

function exportPDF(){ window.print(); }

function openGallery(){
  openMega('galleryModal');
  var box = document.getElementById('galleryContent');
  var imgs = document.querySelectorAll('.bb img');
  if(!imgs.length){ box.innerHTML = '<p style="text-align:center;color:var(--text-2);padding:20px">لا توجد صور بعد</p>'; return; }
  var h = '';
  imgs.forEach(function(img){ h += '<img src="' + img.src + '" onclick="window.open(this.src)"/>'; });
  box.innerHTML = h;
}

document.addEventListener('keydown', function(e){
  if(e.ctrlKey && e.key === 'f'){ e.preventDefault(); openChatSearch(); }
  if(e.key === 'Escape') closeChatSearch();
});

setTimeout(function(){ loadAccent(); loadPersona(); }, 300);

</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.0.8/purify.min.js"></script>
<script src="https://accounts.google.com/gsi/client" async defer></script>
<style>
:root{--bg:#fff;--bg-soft:#f8f9fb;--text:#1a1a1a;--text-2:#6b7280;--border:#e5e7eb;--accent:#1a73e8;--accent-2:#9b72cb;--user-bubble:linear-gradient(135deg,#1a73e8,#4a90f7);--ai-bubble:#f4f6fb;--input-bg:#fff;--shadow:0 4px 24px rgba(0,0,0,.06);--shadow-lg:0 12px 40px rgba(0,0,0,.12)}
[data-theme="dark"]{--bg:#0f1117;--bg-soft:#151823;--text:#e8eaed;--text-2:#9aa0a6;--border:#2a2e3d;--accent:#4a90f7;--accent-2:#b794f6;--user-bubble:linear-gradient(135deg,#1a73e8,#6a4ce0);--ai-bubble:#1e2230;--input-bg:#1e2230;--shadow:0 4px 24px rgba(0,0,0,.3);--shadow-lg:0 12px 40px rgba(0,0,0,.5)}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden}
body{font-family:'Segoe UI','Tahoma',system-ui,sans-serif;background:var(--bg);color:var(--text)}
.hidden{display:none !important}

/* ===== شاشة الدخول ===== */
.auth-screen{position:fixed;inset:0;background:linear-gradient(135deg,#1a73e8,#9b72cb);display:flex;align-items:center;justify-content:center;padding:20px;z-index:10000;overflow-y:auto}
.auth-card{background:rgba(255,255,255,.95);backdrop-filter:blur(20px);border-radius:24px;padding:36px 28px;max-width:400px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.3);text-align:center}
.auth-card h1{color:#1a73e8;font-size:28px;margin-bottom:6px}
.auth-card .sub{color:#6b7280;font-size:14px;margin-bottom:24px}
.auth-tabs{display:flex;background:#f0f4f9;border-radius:12px;padding:4px;margin-bottom:20px}
.auth-tabs button{flex:1;padding:10px;border:none;background:none;border-radius:8px;font-weight:600;font-size:14px;cursor:pointer;color:#6b7280;font-family:inherit}
.auth-tabs button.active{background:#fff;color:#1a73e8;box-shadow:0 2px 8px rgba(0,0,0,.08)}
.auth-card input{width:100%;padding:13px 16px;border:1.5px solid #e5e7eb;border-radius:12px;font-size:15px;font-family:inherit;margin-bottom:12px;outline:none;background:#fff}
.auth-card input:focus{border-color:#1a73e8;box-shadow:0 0 0 3px rgba(26,115,232,.1)}
.auth-card .btn{width:100%;padding:14px;border:none;border-radius:12px;background:linear-gradient(135deg,#1a73e8,#9b72cb);color:#fff;font-weight:700;font-size:15px;cursor:pointer;font-family:inherit;margin-top:8px}
.auth-card .btn:active{transform:scale(.98)}
.auth-card .btn:disabled{opacity:.6;cursor:not-allowed}
.auth-msg{margin-top:14px;padding:12px;border-radius:10px;font-size:13.5px;font-weight:600}
.auth-msg.error{background:#fee2e2;color:#b91c1c}
.auth-msg.success{background:#d1fae5;color:#065f46}
.auth-footer{margin-top:20px;font-size:12px;color:#6b7280}
.auth-footer strong{background:linear-gradient(135deg,#1a73e8,#9b72cb);-webkit-background-clip:text;-webkit-text-fill-color:transparent}

/* ===== التطبيق ===== */
.app{display:flex;flex-direction:column;height:100dvh;width:100%}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:998;opacity:0;visibility:hidden;transition:.3s}
.overlay.active{opacity:1;visibility:visible}
.sidebar{position:fixed;top:0;right:0;bottom:0;width:320px;max-width:88vw;background:#fafafa;border-left:1px solid var(--border);transform:translateX(100%);transition:transform .35s;z-index:999;display:flex;flex-direction:column;box-shadow:var(--shadow-lg)}
[data-theme="dark"] .sidebar{background:#12151e}
.sidebar.open{transform:translateX(0)}
.sb-head{padding:18px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)}
.sb-logo{font-weight:800;font-size:17px;background:linear-gradient(135deg,var(--accent),var(--accent-2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sb-close{background:none;border:none;color:var(--text-2);font-size:20px;cursor:pointer;padding:6px}
.sb-user{padding:12px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.sb-avatar{width:36px;height:36px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent-2));display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700}
.sb-user-info{flex:1;min-width:0}
.sb-user-info .name{font-weight:700;font-size:14px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-user-info .stats{font-size:11px;color:var(--text-2)}
.sb-logout{background:none;border:none;color:#ef4444;cursor:pointer;padding:6px;font-size:16px}
.new-chat{margin:14px;padding:12px 16px;background:linear-gradient(135deg,var(--accent),var(--accent-2));color:#fff;border:none;border-radius:14px;font-weight:700;cursor:pointer;font-family:inherit}
.history{flex:1;overflow-y:auto;padding:6px 10px}
.h-item{display:flex;justify-content:space-between;padding:10px 12px;border-radius:10px;cursor:pointer;font-size:13.5px;color:var(--text-2);margin-bottom:2px}
.h-item:hover,.h-item.active{background:var(--bg-soft);color:var(--accent)}
.h-item.pinned{background:rgba(245,158,11,.1);color:#f59e0b}
.h-item .title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.h-item .actions{display:flex;gap:2px;opacity:0;transition:.2s}
.h-item:hover .actions{opacity:1}
.h-item .actions button{background:none;border:none;cursor:pointer;font-size:12px;padding:2px 4px;border-radius:4px}
.h-item .actions .del{color:#ef4444}
.h-item .actions .pin{color:#f59e0b}
.sb-foot{padding:14px;border-top:1px solid var(--border);text-align:center;font-size:12px;color:var(--text-2)}
.sb-foot strong{background:linear-gradient(135deg,var(--accent),var(--accent-2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}

header{padding:14px 20px;display:flex;align-items:center;justify-content:space-between;background:var(--bg);z-index:10}
.hdr-left{display:flex;align-items:center;gap:14px}
.icon-btn{background:none;border:none;color:var(--text-2);font-size:20px;cursor:pointer;width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center}
.icon-btn:hover{background:var(--bg-soft)}
.brand{font-weight:800;font-size:18px;background:linear-gradient(135deg,var(--accent),var(--accent-2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hdr-right{display:flex;gap:6px;align-items:center}
.model-badge{font-size:11px;padding:4px 10px;border-radius:12px;background:var(--bg-soft);color:var(--text-2);border:1px solid var(--border);font-weight:600}

.chat-wrap{flex:1;overflow-y:auto;padding:20px 18px 8px;min-height:0;-webkit-overflow-scrolling:touch}
.chat{max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:20px}
.msg{display:flex;gap:12px;align-items:flex-start;animation:slideIn .35s}
@keyframes slideIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.msg.user{flex-direction:row-reverse}
.avatar{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;color:#fff;flex-shrink:0}
.msg.user .avatar{background:var(--user-bubble)}
.msg.ai .avatar{background:linear-gradient(135deg,#4285f4,#9b72cb)}
.bubble{position:relative;padding:12px 16px;border-radius:18px;line-height:1.7;font-size:15px;word-break:break-word;max-width:calc(100% - 60px);box-shadow:var(--shadow)}
.msg.user .bubble{background:var(--user-bubble);color:#fff;border-top-left-radius:6px}
.msg.ai .bubble{background:var(--ai-bubble);color:var(--text);border-top-right-radius:6px}
.bubble pre{background:#0d1117;color:#e6edf3;padding:14px;border-radius:10px;overflow-x:auto;margin:10px 0;font-size:13px;direction:ltr;text-align:left}
.bubble code:not(pre code){background:rgba(127,127,127,.15);padding:2px 6px;border-radius:5px;font-size:.9em;direction:ltr;display:inline-block}
.bubble img,.bubble iframe{max-width:100%;border-radius:12px;margin-top:10px}
.bubble iframe{width:100%;height:320px;border:1px solid var(--border);background:#fff}
.bubble .meta{display:flex;justify-content:space-between;align-items:center;margin-top:8px;padding-top:6px;border-top:1px solid rgba(127,127,127,.15);font-size:11px;color:var(--text-2)}
.bubble .actions{display:flex;gap:4px}
.act-btn{background:none;border:none;color:var(--text-2);cursor:pointer;padding:4px 6px;border-radius:6px;font-size:12px}
.act-btn:hover{background:rgba(127,127,127,.15);color:var(--accent)}
.act-btn.speaking{background:rgba(16,185,129,.2);color:#10b981;animation:speakPulse 1s infinite}
@keyframes speakPulse{0%,100%{opacity:1}50%{opacity:.6}}
.typing{display:flex;gap:5px;padding:4px 0}
.typing span{width:8px;height:8px;border-radius:50%;background:var(--accent);opacity:.4;animation:blink 1.4s infinite both}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.3;transform:scale(1)}40%{opacity:1;transform:scale(1.2)}}
.streaming-cursor::after{content:'▊';color:var(--accent);animation:blinkCursor .8s infinite;font-weight:300}
@keyframes blinkCursor{0%,50%{opacity:1}51%,100%{opacity:0}}

.welcome{text-align:center;padding:30px 16px}
.welcome h1{font-size:26px;font-weight:800;margin-bottom:8px;background:linear-gradient(135deg,var(--accent),var(--accent-2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.welcome p{color:var(--text-2);margin-bottom:20px;font-size:14.5px}
.chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;max-width:680px;margin:0 auto}
.chip{padding:14px;border-radius:14px;background:var(--bg-soft);border:1px solid var(--border);cursor:pointer;text-align:right}
.chip .ico{font-size:20px;margin-bottom:4px;display:block}
.chip .ttl{font-weight:700;font-size:13.5px}

.input-zone{padding:8px 12px calc(12px + env(safe-area-inset-bottom));background:var(--bg)}
.input-card{max-width:900px;margin:0 auto;background:var(--input-bg);border:1.5px solid var(--border);border-radius:28px;box-shadow:var(--shadow);position:relative}
.input-card:focus-within{border-color:var(--accent);box-shadow:0 4px 20px rgba(26,115,232,.15)}
.input-body{padding:12px 20px 4px}
#userInput{width:100%;min-height:44px;max-height:220px;background:transparent;border:none;outline:none;color:var(--text);font-family:inherit;font-size:16px;line-height:1.5;padding:6px 0;resize:none}
#userInput::placeholder{color:var(--text-2);opacity:.8}
.action-bar{display:flex;align-items:center;justify-content:space-between;padding:6px 12px 10px;gap:8px}
.bar-left,.bar-right{display:flex;align-items:center;gap:4px}
.round-btn{width:42px;height:42px;border-radius:50%;background:none;border:none;color:var(--text);cursor:pointer;display:flex;align-items:center;justify-content:center}
.round-btn:hover{background:var(--bg-soft)}
.round-btn svg{width:22px;height:22px}
.round-btn.recording{background:#fee2e2;color:#ef4444;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,.4)}50%{box-shadow:0 0 0 10px rgba(239,68,68,0)}}
.pill{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;background:var(--bg-soft);border:1.5px solid transparent;border-radius:20px;color:var(--text);font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap}
.pill:hover{background:var(--border)}
.pill svg{width:18px;height:18px}
.pill.active{background:rgba(26,115,232,.1);border-color:var(--accent);color:var(--accent)}
.pill.active-thinking{background:rgba(155,114,203,.12);border-color:var(--accent-2);color:var(--accent-2)}
.send-btn{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent-2));color:#fff;border:none;cursor:pointer;display:none;align-items:center;justify-content:center;box-shadow:0 4px 14px rgba(26,115,232,.4)}
.send-btn.show{display:flex}
.send-btn.stop{background:linear-gradient(135deg,#ef4444,#f97316)}
.send-btn svg{width:20px;height:20px}












.file-preview{margin:0 14px 8px;padding:8px 12px;background:var(--bg-soft);border:1px solid var(--border);border-radius:12px;display:none;align-items:center;gap:10px;font-size:13px}
.file-preview.show{display:flex}
.file-preview img{width:36px;height:36px;object-fit:cover;border-radius:8px}
.file-preview .close{margin-right:auto;background:none;border:none;color:#ef4444;cursor:pointer;font-size:16px}
.toast-wrap{position:fixed;top:20px;left:20px;z-index:9999;display:flex;flex-direction:column;gap:10px}
.toast{padding:12px 18px;border-radius:12px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);box-shadow:var(--shadow-lg);font-size:13.5px;font-weight:600;display:flex;align-items:center;gap:8px;animation:toastIn .3s}
.toast.success{border-right:4px solid #10b981}
.toast.error{border-right:4px solid #ef4444}
@keyframes toastIn{from{opacity:0;transform:translateX(-30px)}to{opacity:1;transform:translateX(0)}}
@media (max-width:640px){header{padding:10px 14px}.brand{font-size:16px}.model-badge{display:none}.chat-wrap{padding:16px 12px 8px}.bubble{font-size:14.5px;padding:11px 14px}.avatar{width:32px;height:32px;font-size:13px}.welcome h1{font-size:22px}.chips{grid-template-columns:1fr 1fr}.chip{padding:12px}#userInput{font-size:16px}.pill{padding:7px 12px;font-size:13px}.round-btn{width:40px;height:40px}.send-btn{width:40px;height:40px}}

.mega-modal{position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(4px);z-index:9999;display:none;align-items:center;justify-content:center;padding:20px}
.mega-modal.on{display:flex}
.mega-box{background:var(--input-bg);border-radius:20px;padding:24px;max-width:500px;width:100%;max-height:80vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.4);position:relative;animation:pop .3s}
@keyframes pop{from{opacity:0;transform:scale(.9)}to{opacity:1;transform:scale(1)}}
.mega-box h2{margin:0 0 16px;font-size:20px;color:var(--accent)}
.mega-box .close{position:absolute;top:12px;left:12px;background:none;border:none;font-size:22px;cursor:pointer;color:var(--text-2)}
.stat-row{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);font-size:14px}
.stat-row:last-child{border:none}
.stat-row b{color:var(--accent)}
.pers-select{width:100%;padding:10px;border-radius:10px;border:1.5px solid var(--border);background:var(--bg-soft);color:var(--text);font-family:inherit;font-size:14px;margin-bottom:12px;outline:none}
.search-in-chat{position:fixed;top:70px;left:50%;transform:translateX(-50%);z-index:999;background:var(--input-bg);border:1px solid var(--border);border-radius:14px;padding:8px 12px;display:none;gap:8px;align-items:center;box-shadow:0 8px 30px rgba(0,0,0,.15)}
.search-in-chat.on{display:flex}
.search-in-chat input{border:none;background:transparent;outline:none;color:var(--text);font-family:inherit;font-size:14px;width:200px}
.search-in-chat button{background:none;border:none;color:var(--text-2);cursor:pointer;font-size:16px}
.theme-dot{width:30px;height:30px;border-radius:50%;cursor:pointer;border:2px solid transparent;transition:.2s}
.theme-dot:hover{transform:scale(1.15)}
.theme-dot.sel{border-color:var(--text);transform:scale(1.15)}
.gallery-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-top:10px}
.gallery-grid img{width:100%;height:140px;object-fit:cover;border-radius:10px;cursor:pointer;transition:.2s}
.gallery-grid img:hover{transform:scale(1.05)}
.hdr .hr{flex-wrap:wrap;gap:2px}
.hdr .hr .ib{font-size:17px;width:36px;height:36px}




.tools-menu::-webkit-scrollbar{width:4px}
.tools-menu::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

.tools-menu.show 












}




#plusBtn{transition:transform .3s,background .2s,color .2s}
#plusBtn.on{background:var(--accent);color:#fff;transform:rotate(45deg)}
@media(max-width:640px){
  
  
  
}


.tools-menu{
  position:absolute;
  bottom:calc(100% + 8px);
  left:12px;right:12px;
  background:var(--input-bg);
  border:1px solid var(--border);
  border-radius:18px;
  padding:4px;
  box-shadow:0 12px 40px rgba(0,0,0,.2);
  display:none;
  flex-direction:column;
  gap:1px;
  max-height:280px;
  overflow-y:auto;
  overflow-x:hidden;
  z-index:50;
  animation:menuIn .25s cubic-bezier(.4,0,.2,1);
}
.tools-menu.show{display:flex}
.tools-menu::-webkit-scrollbar{width:4px}
.tools-menu::-webkit-scrollbar-track{background:transparent}
.tools-menu::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
@keyframes menuIn{
  from{opacity:0;transform:translateY(15px) scale(.96)}
  to{opacity:1;transform:translateY(0) scale(1)}
}
.menu-item{
  display:flex;align-items:center;gap:8px;
  padding:6px 10px;border-radius:9px;cursor:pointer;
  color:var(--text);font-size:12px;font-weight:500;
  border:none;background:none;font-family:inherit;
  text-align:right;width:100%;min-height:34px;
  flex-shrink:0;
  transition:background .15s;
}
.menu-item:hover,.menu-item:active{background:var(--bg-soft)}
.menu-item .ic{width:26px;height:26px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0}
.menu-item .ic.blue{background:rgba(26,115,232,.12)}
.menu-item .ic.purple{background:rgba(155,114,203,.12)}
.menu-item .ic.green{background:rgba(16,185,129,.12)}
.menu-item .ic.orange{background:rgba(245,158,11,.12)}
.menu-item .ic.pink{background:rgba(236,72,153,.12)}
.menu-item .txt{display:flex;flex-direction:column;gap:0;flex:1;min-width:0;line-height:1.2}
.menu-item .txt small{color:var(--text-2);font-size:9.5px;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
#plusBtn{transition:transform .3s,background .2s,color .2s}
#plusBtn.on{background:var(--accent);color:#fff;transform:rotate(45deg)}
@media(max-width:640px){
  .tools-menu{max-height:260px;bottom:calc(100% + 6px)}
  .menu-item{padding:5px 9px;min-height:32px;font-size:11.5px}
  .menu-item .ic{width:24px;height:24px;font-size:12px}
  .menu-item .txt small{font-size:9px}
}

</style>


<style id="live-css">
#lcModal{position:fixed;inset:0;background:#000;z-index:99999;display:none;flex-direction:column}
#lcModal.on{display:flex}
.lc-header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;background:rgba(0,0,0,.75);color:#fff;font-size:14px;font-weight:700;z-index:2}
.lc-status{display:flex;align-items:center;gap:10px}
.lc-dot{width:12px;height:12px;border-radius:50%;background:#666;transition:all .3s}
.lc-dot.listening{background:#10b981;box-shadow:0 0 14px #10b981;animation:lcP 1s infinite}
.lc-dot.thinking{background:#f59e0b;box-shadow:0 0 14px #f59e0b;animation:lcP 1s infinite}
.lc-dot.speaking{background:#8b5cf6;box-shadow:0 0 14px #8b5cf6;animation:lcP .7s infinite}
.lc-dot.error{background:#ef4444}
@keyframes lcP{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.3);opacity:.6}}
.lc-x{background:rgba(255,255,255,.18);border:none;color:#fff;width:38px;height:38px;border-radius:50%;font-size:18px;cursor:pointer}
.lc-video{flex:1;width:100%;background:#000;object-fit:cover;min-height:0}
.lc-info{position:absolute;top:74px;left:14px;right:14px;background:rgba(0,0,0,.78);color:#fff;padding:14px 18px;border-radius:16px;font-size:14px;line-height:1.7;max-height:42vh;overflow-y:auto;backdrop-filter:blur(12px);z-index:3}
.lc-info .lbl{font-size:11px;opacity:.7;margin-bottom:4px;display:block}
.lc-info .you{color:#60a5fa;font-weight:700;word-wrap:break-word}
.lc-info .ai{color:#a78bfa;font-weight:700;word-wrap:break-word}
.lc-info .dv{height:1px;background:rgba(255,255,255,.1);margin:10px 0}
.lc-bottom{position:absolute;bottom:0;left:0;right:0;padding:20px 20px 30px;display:flex;flex-direction:column;align-items:center;gap:12px;background:linear-gradient(to top,rgba(0,0,0,.75),transparent);z-index:3}
.lc-hint{color:rgba(255,255,255,.9);font-size:13px;text-shadow:0 1px 4px rgba(0,0,0,.8);text-align:center}
.lc-btns{display:flex;gap:18px;align-items:center}
.lc-btn{width:64px;height:64px;border-radius:50%;border:none;color:#fff;font-size:26px;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 6px 20px rgba(0,0,0,.5);transition:transform .15s}
.lc-btn:active{transform:scale(.9)}
.lc-btn.end{background:linear-gradient(135deg,#ef4444,#dc2626)}
.lc-btn.pause{background:rgba(255,255,255,.25);backdrop-filter:blur(10px);width:52px;height:52px;font-size:20px}
.lc-btn.info{background:rgba(255,255,255,.25);backdrop-filter:blur(10px);width:52px;height:52px;font-size:20px}
</style>

<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#1a73e8">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Flash-Lite">
<link rel="icon" type="image/svg+xml" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-192.png">

<style id="bg-css">
body{
  background-attachment:fixed !important;
  background-size:cover !important;
  background-position:center !important;
  background-repeat:no-repeat !important;
}
body.has-bg::before{
  content:'';
  position:fixed;
  inset:0;
  background:rgba(255,255,255,.85);
  z-index:-1;
  pointer-events:none;
}
[data-theme="dark"] body.has-bg::before{
  background:rgba(15,17,23,.88);
}
.bg-modal{position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(6px);z-index:9999;display:none;align-items:center;justify-content:center;padding:20px}
.bg-modal.on{display:flex}
.bg-box{background:var(--input-bg);border-radius:20px;padding:24px;max-width:500px;width:100%;max-height:85vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.4);position:relative;animation:bgPop .3s}
@keyframes bgPop{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:scale(1)}}
.bg-box h2{margin:0 0 16px;font-size:19px;color:var(--accent);text-align:center}
.bg-box .bg-close{position:absolute;top:12px;left:12px;background:none;border:none;font-size:22px;cursor:pointer;color:var(--text-2)}
.bg-section{margin-bottom:18px}
.bg-section-title{font-size:12.5px;color:var(--text-2);margin-bottom:8px;font-weight:600}
.bg-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.bg-thumb{width:100%;aspect-ratio:1;border-radius:12px;cursor:pointer;border:2px solid transparent;transition:.2s;background-size:cover;background-position:center}
.bg-thumb:hover{transform:scale(1.05);border-color:var(--accent)}
.bg-thumb.sel{border-color:var(--accent);box-shadow:0 0 0 3px rgba(26,115,232,.25)}
.bg-btn{width:100%;padding:12px;border:none;border-radius:12px;font-family:inherit;font-size:14px;font-weight:700;cursor:pointer;margin-top:8px}
.bg-btn.upload{background:linear-gradient(135deg,#10b981,#059669);color:#fff}
.bg-btn.clear{background:var(--bg-soft);color:var(--text);border:1.5px solid var(--border)}
.bg-opacity{width:100%;margin:8px 0}
.bg-opacity-lbl{font-size:12px;color:var(--text-2);text-align:center;display:block;margin-bottom:4px}
</style>
</head>
<body>

<!-- شاشة الدخول -->
<div class="auth-screen" id="authScreen">
  <div class="auth-card">
    <h1>⚡ Flash-Lite</h1>
    <p class="sub">مساعدك الذكي — إعداد معتصم علي</p>
    <div class="auth-tabs">
      <button class="active" onclick="switchTab('login')" id="tabLogin">تسجيل دخول</button>
      <button onclick="switchTab('register')" id="tabRegister">حساب جديد</button>
    </div>
    <div id="loginForm">
      <input type="text" id="loginUser" placeholder="اسم المستخدم" autocomplete="username">
      <input type="password" id="loginPass" placeholder="كلمة المرور" autocomplete="current-password">
      <button class="btn" id="btnLogin" onclick="doLogin()">دخول</button>
<div style="text-align:center;margin:12px 0;color:#9ca3af;font-size:13px">- او -</div>
<div id="g_id_onload" data-client_id="694239714110-vctdhk0pdjq26lr4nm089bo6iq3l48ke.apps.googleusercontent.com" data-callback="handleGoogleLogin" data-auto_prompt="false"></div>
<div style="display:flex;justify-content:center;margin-bottom:8px">
<div class="g_id_signin" data-type="standard" data-size="large" data-theme="outline" data-text="signin_with" data-shape="rectangular"></div>
</div>
    </div>
    <div id="registerForm" class="hidden">
      <input type="text" id="regUser" placeholder="اسم المستخدم (3-30 حرف)" autocomplete="username">
      <input type="email" id="regEmail" placeholder="البريد الإلكتروني (اختياري)" autocomplete="email">
      <input type="password" id="regPass" placeholder="كلمة المرور (6 أحرف على الأقل)" autocomplete="new-password">
      <input type="password" id="regPass2" placeholder="تأكيد كلمة المرور" autocomplete="new-password">
      <button class="btn" id="btnReg" onclick="doRegister()">إنشاء الحساب</button>
    </div>
    <div id="authMsg" class="auth-msg hidden"></div>
    <div class="auth-footer">تصميم وتطوير <strong>معتصم علي</strong></div>
  </div>
</div>

<!-- التطبيق -->
<div class="app hidden" id="app">
  <div class="overlay" id="overlay" onclick="toggleSidebar()"></div>
  <aside class="sidebar" id="sidebar">
    <div class="sb-head"><span class="sb-logo">⚡ Flash-Lite</span><button class="sb-close" onclick="toggleSidebar()">✕</button></div>
    <div class="sb-user">
      <div class="sb-avatar" id="userAvatar">?</div>
      <div class="sb-user-info"><div class="name" id="userName">...</div><div class="stats" id="userStats">0 محادثة</div></div>
      <button class="sb-logout" onclick="doLogout()" title="خروج">🚪</button>
    </div>
    <button class="new-chat" onclick="newChat()">＋ محادثة جديدة</button>
    <div class="history" id="historyList"></div>
    <div class="sb-foot">تصميم <strong>معتصم علي</strong></div>
  </aside>

  <header>
    <div class="hdr-left"><button class="icon-btn" onclick="toggleSidebar()">☰</button><span class="brand">Flash-Lite</span><span class="model-badge" id="modelBadge">⚡ جاهز</span></div>
    <div class="hdr-right">
<button class="icon-btn" id="ttsBtn" onclick="toggleAutoTTS()" title="النطق">🔇</button>
<button class="icon-btn" id="themeBtn" onclick="toggleTheme()">🌙</button>
<button class="icon-btn" onclick="exportChat()" title="تصدير">⬇</button>
</div>
  </header>

  <div class="chat-wrap" id="chatWrap"><div class="chat" id="chatBox"></div></div>

  <div class="input-zone">
    <div class="input-card">
      <div class="tools-menu" id="toolsMenu">
        <button class="menu-item" onclick="fileInput.click();closeToolsMenu()"><span class="ic blue">📎</span><span class="txt">إرفاق ملف<small>PDF، TXT، كود</small></span></button>
        <button class="menu-item" onclick="imageInput.click();closeToolsMenu()"><span class="ic purple">🖼️</span><span class="txt">رفع صورة<small>PNG، JPG</small></span></button>
        <button class="menu-item" onclick="quickPrompt('🎨 صمم لي رسوماً متحركة HTML و CSS لـ ');closeToolsMenu()"><span class="ic orange">🎨</span><span class="txt">توليد أنيميشن<small>HTML + CSS</small></span></button>
        <button class="menu-item" onclick="quickPrompt('💻 اكتب لي كود Python لـ ');closeToolsMenu()"><span class="ic green">💻</span><span class="txt">كتابة كود<small>Python، JS، C++</small></span></button>
        <button class="menu-item" onclick="quickPrompt('🖼️ ارسم لي صورة عالية الدقة لـ ');closeToolsMenu()"><span class="ic pink">🖼️</span><span class="txt">توليد صورة<small>ذكاء اصطناعي</small></span></button>
<button class="menu-item" onclick="showStats();closeToolsMenu()"><span class="ic blue">📊</span><span class="txt">إحصائياتي<small>عدد المحادثات والرسائل</small></span></button>
<button class="menu-item" onclick="openMega('themesModal');closeToolsMenu()"><span class="ic purple">🎨</span><span class="txt">لون التطبيق<small>7 ألوان جاهزة</small></span></button>
<button class="menu-item" onclick="openMega('personaModal');closeToolsMenu()"><span class="ic orange">🎭</span><span class="txt">شخصية AI<small>مدرّس، مبرمج، شاعر...</small></span></button>
<button class="menu-item" onclick="openGallery();closeToolsMenu()"><span class="ic pink">🖼️</span><span class="txt">معرض الصور<small>كل الصور المولّدة</small></span></button>
<button class="menu-item" onclick="openChatSearch();closeToolsMenu()"><span class="ic green">🔍</span><span class="txt">بحث في المحادثة<small>Ctrl + F</small></span></button>
<button class="menu-item" onclick="summarizeChat();closeToolsMenu()"><span class="ic blue">📝</span><span class="txt">تلخيص المحادثة<small>5 نقاط رئيسية</small></span></button>
<button class="menu-item" onclick="exportPDF();closeToolsMenu()"><span class="ic purple">📄</span><span class="txt">طباعة / PDF<small>احفظ المحادثة</small></span></button>
<button class="menu-item" onclick="closeToolsMenu();lcStart()"><span class="ic green">🎙️</span><span class="txt">مكالمة صوتية حية<small>تحدث وسيرد تلقائياً</small></span></button>
<button class="menu-item" onclick="closeToolsMenu();bgOpen()"><span class="ic purple">🎨</span><span class="txt">خلفية التطبيق<small>12 خلفية + رفع صورة</small></span></button>
      </div>
      <div class="input-body"><textarea id="userInput" placeholder="اكتب رسالة..." rows="1" oninput="onInput()" onkeydown="onKey(event)"></textarea></div>
      <div class="file-preview" id="filePreview"><span id="fpIcon">📄</span><span id="fpName">file</span><button class="close" onclick="clearFile()">✕</button></div>
      <div class="action-bar">
        <div class="bar-left">
          <button class="round-btn" id="voiceBtn" onclick="toggleVoice()" title="صوتي"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg></button>
          <button class="round-btn" id="plusBtn" onclick="toggleToolsMenu()" title="المزيد"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
        </div>
        <div class="bar-right">
          <button class="pill" id="searchToggle" onclick="toggleSearch()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg> بحث</button>
          <button class="pill" id="thinkToggle" onclick="toggleThink()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 0-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2z"/></svg> تفكير</button>
          <button class="send-btn" id="sendBtn" onclick="sendMessage()"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg></button>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast-wrap" id="toasts"></div>
<input type="file" id="fileInput" hidden onchange="handleFile(event,false)">
<input type="file" id="imageInput" hidden accept="image/*" onchange="handleFile(event,true)">

<script>
const MAX_CHARS = 8000;
let token = localStorage.getItem('fl_token') || '';
let user = null;
let chats = [];
let currentChatId = null;
let uploadedFile = null;
let abortCtrl = null;
let recognition = null;
let isSearchOn = false, isThinkOn = false, isStreaming = false;
let autoTTS = localStorage.getItem('fl_autoTTS') === 'on';
let arabicVoice = null;

/* ===== Theme ===== */
function applyTheme(t){document.documentElement.setAttribute('data-theme',t);document.getElementById('themeBtn').textContent=t==='dark'?'☀️':'🌙';document.getElementById('hljs-theme').href=t==='dark'?'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css':'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css';localStorage.setItem('fl_theme',t)}
function toggleTheme(){applyTheme(document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark')}
applyTheme(localStorage.getItem('fl_theme')||'light');

/* ===== TTS ===== */
function loadVoices(){if(!('speechSynthesis' in window))return;const v=speechSynthesis.getVoices();arabicVoice=v.find(x=>x.lang&&x.lang.toLowerCase().startsWith('ar'))||null}
if('speechSynthesis' in window){loadVoices();speechSynthesis.onvoiceschanged=loadVoices}
function updateTTSBtn(){const b=document.getElementById('ttsBtn');if(!('speechSynthesis' in window)){b.textContent='🚫';return}b.textContent=autoTTS?'🔊':'🔇';b.style.color=autoTTS?'#10b981':'var(--text-2)'}
function toggleAutoTTS(){if(!('speechSynthesis' in window)){showToast('لا يدعم النطق','error');return}autoTTS=!autoTTS;localStorage.setItem('fl_autoTTS',autoTTS?'on':'off');updateTTSBtn();if(autoTTS){speak('النطق التلقائي مفعّل')}else{stopSpeaking()}}
function cleanTextForSpeech(t){return t.replace(/```[\s\S]*?```/g,' ... كود ... ').replace(/`([^`]+)`/g,'$1').replace(/!\[.*?\]\(.*?\)/g,'').replace(/\[([^\]]+)\]\([^)]+\)/g,'$1').replace(/[*_~#>`]/g,'').replace(/^\s*[-*+]\s+/gm,'').replace(/\n{3,}/g,'\n\n').replace(/\s{2,}/g,' ').trim()}
function speak(text){if(!('speechSynthesis' in window))return;stopSpeaking();const c=cleanTextForSpeech(text);if(!c||c.length<2)return;const f=c.length>6000?c.slice(0,6000)+'...':c;const u=new SpeechSynthesisUtterance(f);u.lang=arabicVoice?arabicVoice.lang:'ar-SA';if(arabicVoice)u.voice=arabicVoice;speechSynthesis.speak(u)}
function stopSpeaking(){if('speechSynthesis' in window){speechSynthesis.cancel();document.querySelectorAll('.act-btn.speaking').forEach(b=>b.classList.remove('speaking'))}}
function speakMessage(btn){const b=btn.closest('.bubble');const t=b.querySelector('.body').innerText;if(btn.classList.contains('speaking')){stopSpeaking()}else{document.querySelectorAll('.act-btn.speaking').forEach(x=>x.classList.remove('speaking'));speak(t);btn.classList.add('speaking');const c=setInterval(()=>{if(!speechSynthesis.speaking){btn.classList.remove('speaking');clearInterval(c)}},300)}}
updateTTSBtn();

/* ===== Auth ===== */
function switchTab(t){document.getElementById('tabLogin').classList.toggle('active',t==='login');document.getElementById('tabRegister').classList.toggle('active',t==='register');document.getElementById('loginForm').classList.toggle('hidden',t!=='login');document.getElementById('registerForm').classList.toggle('hidden',t!=='register');hideAuthMsg()}
function showAuthMsg(m,type){const e=document.getElementById('authMsg');e.textContent=m;e.className='auth-msg '+type;e.classList.remove('hidden')}
function hideAuthMsg(){document.getElementById('authMsg').classList.add('hidden')}


async function handleGoogleLogin(response){
  try{
    const r = await fetch('/api/auth/google', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({credential: response.credential})
    });
    const d = await r.json();
    if(!r.ok){ alert(d.error||'فشل'); return; }
    localStorage.setItem('fl_token', d.token);
    localStorage.setItem('fl_tk', d.token);
    location.reload();
  }catch(e){ alert('فشل الاتصال: ' + e.message); }
}

async function doLogin(){
  const u=document.getElementById('loginUser').value.trim();
  const p=document.getElementById('loginPass').value;
  if(!u||!p){showAuthMsg('املأ الحقول','error');return}
  const btn=document.getElementById('btnLogin');btn.disabled=true;btn.textContent='جاري الدخول...';
  try{const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});const d=await r.json();
  if(!r.ok){showAuthMsg(d.error||'خطأ','error');btn.disabled=false;btn.textContent='دخول';return}
  token=d.token;localStorage.setItem('fl_token',token);showAuthMsg(d.message,'success');setTimeout(()=>startApp(d.user),500);
  }catch(e){showAuthMsg('فشل الاتصال','error');btn.disabled=false;btn.textContent='دخول'}
}

async function doRegister(){
  const u=document.getElementById('regUser').value.trim();
  const e=document.getElementById('regEmail').value.trim();
  const p=document.getElementById('regPass').value;
  const p2=document.getElementById('regPass2').value;
  if(!u||!p){showAuthMsg('املأ الحقول المطلوبة','error');return}
  if(p!==p2){showAuthMsg('كلمتا المرور غير متطابقتين','error');return}
  if(p.length<6){showAuthMsg('كلمة المرور قصيرة','error');return}
  const btn=document.getElementById('btnReg');btn.disabled=true;btn.textContent='جاري الإنشاء...';
  try{const r=await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p,email:e||null})});const d=await r.json();
  if(!r.ok){showAuthMsg(d.error||'خطأ','error');btn.disabled=false;btn.textContent='إنشاء الحساب';return}
  token=d.token;localStorage.setItem('fl_token',token);showAuthMsg(d.message,'success');setTimeout(()=>startApp(d.user),700);
  }catch(e){showAuthMsg('فشل الاتصال','error');btn.disabled=false;btn.textContent='إنشاء الحساب'}
}

async function doLogout(){
  try{await fetch('/api/auth/logout',{method:'POST',headers:{'Authorization':'Bearer '+token}})}catch(e){}
  token='';localStorage.removeItem('fl_token');location.reload();
}

async function checkAuth(){
  if(!token){return false}
  try{const r=await fetch('/api/auth/me',{headers:{'Authorization':'Bearer '+token}});if(!r.ok)return false;const d=await r.json();user=d.user;return true}catch(e){return false}
}

/* ===== App ===== */
async function startApp(u){
  user=u;
  document.getElementById('authScreen').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
  document.getElementById('userName').textContent=u.username;
  document.getElementById('userAvatar').textContent=u.username.charAt(0).toUpperCase();
  await loadChats();
  await loadModelInfo();
  document.getElementById('chatBox').innerHTML=welcomeHTML();
}

async function loadModelInfo(){
  try{const r=await fetch('/api/status');const d=await r.json();document.getElementById('modelBadge').textContent='⚡ '+(d.model||'جاهز')}catch(e){}
}

async function loadChats(){
  try{const r=await fetch('/api/chats',{headers:{'Authorization':'Bearer '+token}});const d=await r.json();chats=d.chats||[];renderHistory();updateStats()}catch(e){chats=[]}
}

function updateStats(){const s=document.getElementById('userStats');const total=chats.reduce((a,c)=>a+(c.msg_count||0),0);s.textContent=`${chats.length} محادثة • ${total} رسالة`}

function renderHistory(){
  const l=document.getElementById('historyList');l.innerHTML='';
  if(!chats.length){l.innerHTML='<div style="text-align:center;color:var(--text-2);font-size:13px;padding:20px">لا توجد محادثات</div>';return}
  chats.forEach(c=>{const e=document.createElement('div');e.className='h-item'+(c.id===currentChatId?' active':'')+(c.pinned?' pinned':'');e.innerHTML=`<span class="title">${c.pinned?'📌 ':''}${escapeHtml(c.title)}</span><div class="actions"><button class="pin" onclick="event.stopPropagation();togglePin(${c.id})" title="تثبيت">📌</button><button class="del" onclick="event.stopPropagation();deleteChat(${c.id})" title="حذف">🗑</button></div>`;e.querySelector('.title').onclick=()=>loadChat(c.id);l.appendChild(e)});
}

async function loadChat(id){
  stopSpeaking();currentChatId=id;toggleSidebar();
  try{const r=await fetch('/api/chats/'+id,{headers:{'Authorization':'Bearer '+token}});const d=await r.json();
  document.getElementById('chatBox').innerHTML='';d.messages.forEach(m=>addMessage(m.role,m.content,m.created_at,false));
  renderHistory();scrollBottom();
  }catch(e){showToast('فشل التحميل','error')}
}

async function deleteChat(id){
  if(!confirm('حذف المحادثة؟'))return;
  await fetch('/api/chats/'+id,{method:'DELETE',headers:{'Authorization':'Bearer '+token}});
  if(currentChatId===id){currentChatId=null;document.getElementById('chatBox').innerHTML=welcomeHTML()}
  await loadChats();
}

async function togglePin(id){await fetch(`/api/chats/${id}/pin`,{method:'POST',headers:{'Authorization':'Bearer '+token}});await loadChats()}

async function newChat(){
  stopSpeaking();currentChatId=null;document.getElementById('chatBox').innerHTML=welcomeHTML();renderHistory();toggleSidebar();scrollBottom();
}

async function ensureChat(title){
  if(currentChatId)return currentChatId;
  const r=await fetch('/api/chats',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify({title})});
  const d=await r.json();currentChatId=d.id;await loadChats();return d.id;
}

/* ===== Sidebar ===== */
function toggleSidebar(){document.getElementById('sidebar').classList.toggle('open');document.getElementById('overlay').classList.toggle('active')}

/* ===== Messages ===== */
function welcomeHTML(){return`<div class="welcome"><h1>👋 أهلاً ${user?user.username:''}</h1><p>أنا Flash-Lite، كيف أساعدك اليوم؟</p><div class="chips"><div class="chip" onclick="quickSend('اكتب كود Python لخوارزمية ترتيب سريعة')"><span class="ico">💻</span><div class="ttl">كود Python</div></div><div class="chip" onclick="quickSend('صمم صفحة تسجيل دخول عصرية HTML و CSS')"><span class="ico">🎨</span><div class="ttl">تصميم واجهة</div></div><div class="chip" onclick="quickSend('اشرح الذكاء الاصطناعي بشكل مبسط')"><span class="ico">🧠</span><div class="ttl">شرح مبسط</div></div><div class="chip" onclick="quickSend('ارسم مدينة مستقبلية عند الغروب')"><span class="ico">🖼️</span><div class="ttl">توليد صورة</div></div></div></div>`}
function escapeHtml(t){return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function formatTime(ts){try{const d=new Date(ts);return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0')}catch{return''}}
function renderMarkdown(t){try{return DOMPurify.sanitize(marked.parse(t,{breaks:true,gfm:true}))}catch{return escapeHtml(t)}}
function buildActions(role){return`<button class="act-btn" onclick="copyMsg(this)" title="نسخ">📋</button>${role==='ai'?'<button class="act-btn" onclick="speakMessage(this)" title="نطق">🔊</button>':''}`}
function addMessage(role,content,time,save=true){
  const b=document.getElementById('chatBox');if(b.querySelector('.welcome'))b.innerHTML='';
  const m=document.createElement('div');m.className='msg '+role;
  const a=role==='user'?'أ':'G';
  const h=role==='ai'?renderMarkdown(content):escapeHtml(content).replace(/\n/g,'<br>');
  m.innerHTML=`<div class="avatar">${a}</div><div class="bubble"><div class="body">${h}</div><div class="meta"><span>${formatTime(time)}</span><div class="actions">${buildActions(role)}</div></div></div>`;
  b.appendChild(m);m.querySelectorAll('pre code').forEach(x=>{try{hljs.highlightElement(x)}catch{}});
  scrollBottom();return m;
}
function copyMsg(b){navigator.clipboard.writeText(b.closest('.bubble').querySelector('.body').innerText).then(()=>showToast('تم النسخ ✓','success'))}

/* ===== Input ===== */
function onInput(){const t=document.getElementById('userInput');t.style.height='auto';t.style.height=Math.min(t.scrollHeight,220)+'px';document.getElementById('sendBtn').classList.toggle('show',t.value.trim().length>0||uploadedFile)}
function onKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage()}}
function quickPrompt(t){const i=document.getElementById('userInput');i.value=t;i.focus();onInput()}
function quickSend(t){quickPrompt(t);sendMessage()}
function toggleToolsMenu(){
  var m=document.getElementById('toolsMenu')||document.querySelector('.tools-menu');
  var b=document.getElementById('plusBtn');
  if(!m)return;
  var on=m.classList.toggle('show');
  if(b) b.classList.toggle('on',on);
}
function closeToolsMenu(){
  var m=document.getElementById('toolsMenu')||document.querySelector('.tools-menu');
  var b=document.getElementById('plusBtn');
  if(m) m.classList.remove('show');
  if(b) b.classList.remove('on');
}
document.addEventListener('click',e=>{const m=document.getElementById('toolsMenu');const b=document.getElementById('plusBtn');if(m.classList.contains('show')&&!m.contains(e.target)&&!b.contains(e.target))closeToolsMenu()});
function toggleSearch(){isSearchOn=!isSearchOn;document.getElementById('searchToggle').classList.toggle('active',isSearchOn);showToast(isSearchOn?'🌐 البحث مفعّل':'البحث معطّل',isSearchOn?'success':'info')}
function toggleThink(){isThinkOn=!isThinkOn;const b=document.getElementById('thinkToggle');b.classList.toggle('active',isThinkOn);b.classList.toggle('active-thinking',isThinkOn);showToast(isThinkOn?'🧠 التفكير مفعّل':'التفكير معطّل',isThinkOn?'success':'info')}
function handleFile(e,isImage){const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=ev=>{uploadedFile={name:f.name,content:ev.target.result,isImage};document.getElementById('filePreview').classList.add('show');document.getElementById('fpName').textContent=f.name;document.getElementById('fpIcon').innerHTML=isImage?`<img src="${ev.target.result}">`:'📄';showToast('تم إرفاق: '+f.name,'success');onInput()};if(isImage)r.readAsDataURL(f);else r.readAsText(f)}
function clearFile(){uploadedFile=null;document.getElementById('filePreview').classList.remove('show');document.getElementById('fileInput').value='';document.getElementById('imageInput').value='';onInput()}
function toggleVoice(){if(!('webkitSpeechRecognition' in window)&&!('SpeechRecognition' in window)){showToast('لا يدعم الإدخال الصوتي','error');return}const b=document.getElementById('voiceBtn');if(recognition){recognition.stop();return}const SR=window.SpeechRecognition||window.webkitSpeechRecognition;recognition=new SR();recognition.lang='ar-SA';recognition.continuous=false;recognition.interimResults=true;b.classList.add('recording');const t=document.getElementById('userInput');const base=t.value;recognition.onresult=e=>{let i='';for(let j=e.resultIndex;j<e.results.length;j++){if(e.results[j].isFinal)t.value=base+e.results[j][0].transcript;else i+=e.results[j][0].transcript}if(i)t.value=base+i;onInput()};recognition.onend=()=>{b.classList.remove('recording');recognition=null};recognition.onerror=()=>{b.classList.remove('recording');recognition=null};recognition.start()}

/* ===== Send ===== */
async function sendMessage(){
  if(isStreaming)return;
  const t=document.getElementById('userInput');const x=t.value.trim();
  if(!x&&!uploadedFile)return;
  if(x.length>MAX_CHARS){showToast('النص طويل','error');return}
  stopSpeaking();
  addMessage('user',x||(uploadedFile?'📎 '+uploadedFile.name:''));
  t.value='';t.style.height='auto';onInput();closeToolsMenu();
  await sendPrompt(x);
}

async function sendPrompt(text){
  const btn=document.getElementById('sendBtn');isStreaming=true;btn.classList.add('stop');
  btn.onclick=stopGeneration;btn.innerHTML='<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
  let p=text;
  if(uploadedFile){if(uploadedFile.isImage)p=`[صورة: ${uploadedFile.name}]\n${text}`;else p=`محتوى الملف (${uploadedFile.name}):\n${uploadedFile.content}\n\nالطلب:\n${text}`;clearFile()}
  if(isThinkOn)p=`فكّر بعمق وقدّم إجابة مفصلة بخطوات مرتبة.\n\n${p}`;
  if(isSearchOn)p=`قدّم معلومات دقيقة ومحدّثة.\n\n${p}`;

  const box=document.getElementById('chatBox');if(box.querySelector('.welcome'))box.innerHTML='';
  const ai=document.createElement('div');ai.className='msg ai';
  ai.innerHTML=`<div class="avatar">G</div><div class="bubble"><div class="body streaming-cursor"></div><div class="meta"><span>${formatTime()}</span><div class="actions"></div></div></div>`;
  box.appendChild(ai);scrollBottom();
  const bd=ai.querySelector('.body');let full='';let first=true;
  abortCtrl=new AbortController();

  try{
    await ensureChat(text.slice(0,60)||'محادثة');
    const r=await fetch('/api/chat_stream',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify({prompt:p,chat_id:currentChatId}),signal:abortCtrl.signal});
    if(!r.ok){const e=await r.json().catch(()=>({error:'خطأ '+r.status}));throw new Error(e.error||'خطأ '+r.status)}
    const reader=r.body.getReader();const dec=new TextDecoder('utf-8');let buf='';
    while(true){const{done,value}=await reader.read();if(done)break;buf+=dec.decode(value,{stream:true});const lines=buf.split('\n');buf=lines.pop()||'';
      for(const line of lines){if(!line.startsWith('data: '))continue;const d=line.slice(6).trim();if(d==='[DONE]')continue;
        try{const j=JSON.parse(d);if(j.error){bd.classList.remove('streaming-cursor');bd.innerHTML='<span style="color:#ef4444">❌ '+escapeHtml(j.error)+'</span>';isStreaming=false;return}
        if(j.text){if(first){first=false;bd.innerHTML=''}full+=j.text;bd.innerHTML=renderMarkdown(full);scrollBottom()}}catch(e){}
      }
    }
    bd.classList.remove('streaming-cursor');if(!full.trim())full='_(لا يوجد رد)_';
    bd.innerHTML=renderMarkdown(full);ai.querySelector('.actions').innerHTML=buildActions('ai');
    ai.querySelectorAll('pre code').forEach(x=>{try{hljs.highlightElement(x)}catch{}});
    addExtras(text,full,ai);
    if(autoTTS&&full.trim()){setTimeout(()=>{speak(full);const sb=ai.querySelector('.act-btn[onclick="speakMessage(this)"]');if(sb){sb.classList.add('speaking');const c=setInterval(()=>{if(!speechSynthesis.speaking){sb.classList.remove('speaking');clearInterval(c)}},300)}},200)}
    await loadChats();
  }catch(err){
    bd.classList.remove('streaming-cursor');
    if(err.name==='AbortError')bd.innerHTML=renderMarkdown(full+'\n\n⏹️ _تم الإيقاف_');
    else bd.innerHTML='<span style="color:#ef4444">❌ '+escapeHtml(err.message)+'</span>';
  }finally{
    abortCtrl=null;isStreaming=false;btn.classList.remove('stop');btn.onclick=sendMessage;
    btn.innerHTML='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>';onInput();
  }
}

function addExtras(text,reply,ai){
  const b=ai.querySelector('.bubble');
  if(/ارسم|صورة|draw|image/i.test(text)&&reply.length<1500){const q=encodeURIComponent(text.replace(/ارسم لي|ارسم|صورة|صمم/g,'').trim()||text);const img=document.createElement('img');img.src='https://image.pollinations.ai/prompt/'+q+'?width=1024&height=1024&nologo=true';b.appendChild(img);scrollBottom()}
  if(/رسوم متحركة|أنيميشن|animation/i.test(text)||/```html/i.test(reply)){let code=reply;const m=code.match(/```(?:html)?\n([\s\S]*?)```/);if(m)code=m[1];if(code.includes('<html')||code.includes('<!DOCTYPE')||code.includes('<div')){const ifr=document.createElement('iframe');ifr.sandbox='allow-scripts';ifr.srcdoc=code;b.appendChild(ifr);scrollBottom()}}
}

function stopGeneration(){if(abortCtrl)abortCtrl.abort();stopSpeaking()}
function scrollBottom(s){const w=document.getElementById('chatWrap');w.scrollTo({top:w.scrollHeight,behavior:s?'smooth':'auto'})}
function showToast(m,t='info'){const x=document.createElement('div');x.className='toast '+t;x.innerHTML=`<span>${t==='success'?'✅':t==='error'?'❌':'ℹ️'}</span><span>${escapeHtml(m)}</span>`;document.getElementById('toasts').appendChild(x);setTimeout(()=>{x.style.opacity='0';setTimeout(()=>x.remove(),300)},2400)}
function exportChat(){if(!currentChatId){showToast('لا يوجد محتوى','error');return}fetch('/api/chats/'+currentChatId,{headers:{'Authorization':'Bearer '+token}}).then(r=>r.json()).then(d=>{let t=`# ${d.chat.title}\n\n`;d.messages.forEach(m=>{t+=`## ${m.role==='user'?'👤':'🤖'}\n${m.content}\n\n`});const b=new Blob([t],{type:'text/markdown;charset=utf-8'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=(d.chat.title||'chat')+'.md';a.click();showToast('تم التصدير ✓','success')})}

/* ===== Init ===== */
(async function(){
  if(await checkAuth()){startApp(user)}
  else{document.getElementById('loginUser').focus()}
})();
</script>

<div class="mega-modal" id="statsModal"><div class="mega-box">
<button class="close" onclick="closeMega('statsModal')">✕</button>
<h2>📊 إحصائياتي</h2>
<div id="statsContent"><div class="stat-row"><span>جاري التحميل...</span></div></div>
</div></div>

<div class="mega-modal" id="themesModal"><div class="mega-box">
<button class="close" onclick="closeMega('themesModal')">✕</button>
<h2>🎨 لون التطبيق</h2>
<div style="display:flex;gap:12px;flex-wrap:wrap;justify-content:center;margin-top:16px">
<div class="theme-dot" onclick="setAccent('#1a73e8','#9b72cb',this)" style="background:linear-gradient(135deg,#1a73e8,#9b72cb)"></div>
<div class="theme-dot" onclick="setAccent('#10b981','#059669',this)" style="background:linear-gradient(135deg,#10b981,#059669)"></div>
<div class="theme-dot" onclick="setAccent('#ef4444','#f97316',this)" style="background:linear-gradient(135deg,#ef4444,#f97316)"></div>
<div class="theme-dot" onclick="setAccent('#ec4899','#a855f7',this)" style="background:linear-gradient(135deg,#ec4899,#a855f7)"></div>
<div class="theme-dot" onclick="setAccent('#f59e0b','#d97706',this)" style="background:linear-gradient(135deg,#f59e0b,#d97706)"></div>
<div class="theme-dot" onclick="setAccent('#06b6d4','#0891b2',this)" style="background:linear-gradient(135deg,#06b6d4,#0891b2)"></div>
<div class="theme-dot" onclick="setAccent('#6b7280','#1f2937',this)" style="background:linear-gradient(135deg,#6b7280,#1f2937)"></div>
</div></div></div>

<div class="mega-modal" id="personaModal"><div class="mega-box">
<button class="close" onclick="closeMega('personaModal')">✕</button>
<h2>🎭 شخصية AI</h2>
<select class="pers-select" id="personaSelect" onchange="savePersona()">
<option value="">🌟 افتراضي</option>
<option value="مدرس">👨‍🏫 مدرّس</option>
<option value="مبرمج">👨‍💻 مبرمج</option>
<option value="شاعر">✍️ شاعر</option>
<option value="مترجم">🌍 مترجم</option>
<option value="طبيب">⚕️ مستشار صحي</option>
<option value="محامي">⚖️ مستشار قانوني</option>
<option value="محاسب">📊 محاسب</option>
<option value="مستشار أعمال">💼 مستشار أعمال</option>
<option value="مدرب حياة">🎯 مدرب حياة</option>
</select>
<p style="color:var(--text-2);font-size:12px;text-align:center;margin:0">تُطبَّق على جميع الردود</p>
</div></div>

<div class="mega-modal" id="galleryModal"><div class="mega-box" style="max-width:700px">
<button class="close" onclick="closeMega('galleryModal')">✕</button>
<h2>🖼️ معرض الصور</h2>
<div class="gallery-grid" id="galleryContent"></div>
</div></div>

<div class="search-in-chat" id="chatSearch">
<input id="chatSearchInput" placeholder="ابحث..." oninput="doChatSearch(this.value)"/>
<span id="chatSearchCount" style="font-size:12px;color:var(--text-2)"></span>
<button onclick="closeChatSearch()">✕</button>
</div>


<div id="lcModal">
  <div class="lc-header">
    <div class="lc-status">
      <span class="lc-dot" id="lcDot"></span>
      <span id="lcStatusText">جاري البدء...</span>
    </div>
    <button class="lc-x" onclick="lcStop()">✕</button>
  </div>
  <video class="lc-video" id="lcVideo" autoplay playsinline muted></video>
  <div class="lc-info" id="lcInfo">
    <span class="lbl">👤 أنت قلت:</span>
    <div class="you" id="lcYou">...</div>
    <div class="dv"></div>
    <span class="lbl">🤖 رد AI:</span>
    <div class="ai" id="lcAI">...</div>
  </div>
  <div class="lc-bottom">
    <div class="lc-hint" id="lcHint">اضغط 🎙️ للبدء</div>
    <div class="lc-btns">
      <button class="lc-btn info" onclick="lcToggleInfo()">📄</button>
      <button class="lc-btn pause" id="lcPauseBtn" onclick="lcTogglePause()">⏸️</button>
      <button class="lc-btn end" onclick="lcStop()">📵</button>
    </div>
  </div>
</div>


<script id="live-js">
(function(){
  var stream=null,rec=null,active=false,paused=false,busy=false;
  var finalText='',silTimer=null,lastRes=0,hasSpoken=false,sessionId=0;
  var SIL=1500, RES=400;

  window.lcSetStatus=function(t,c){
    var d=document.getElementById('lcDot'),x=document.getElementById('lcStatusText');
    if(d)d.className='lc-dot '+(c||'');
    if(x)x.textContent=t;
  };
  window.lcSetHint=function(t){var h=document.getElementById('lcHint');if(h)h.textContent=t;};
  window.lcToggleInfo=function(){
    var i=document.getElementById('lcInfo');
    i.style.display=(i.style.display==='none')?'block':'none';
  };

  window.lcStart=async function(){
    if(active)return;
    document.getElementById('lcModal').classList.add('on');
    document.getElementById('lcInfo').style.display='block';
    document.getElementById('lcYou').textContent='...';
    document.getElementById('lcAI').textContent='...';
    paused=false;busy=false;hasSpoken=false;
    document.getElementById('lcPauseBtn').textContent='⏸️';
    sessionId++;
    var my=sessionId;

    lcSetStatus('طلب الأذونات...','thinking');
    lcSetHint('جاري تشغيل الكاميرا...');

    try{
      stream=await navigator.mediaDevices.getUserMedia({
        video:{facingMode:'environment',width:{ideal:720},height:{ideal:1280}},
        audio:false
      });
      if(my!==sessionId)return;
      document.getElementById('lcVideo').srcObject=stream;
      active=true;
    }catch(err){
      alert('فشل الكاميرا: '+err.message);
      window.lcStop();
      return;
    }
    lcSetStatus('يستمع...','listening');
    lcSetHint('تحدث بحرية — سأرد تلقائياً');
    lcStartRec(my);
  };

  function lcStartRec(my){
    if(my!==sessionId)return;
    if(!active||paused||busy)return;
    var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
    if(!SR){lcSetStatus('لا يدعم الصوت','error');return;}
    if(rec){try{rec.abort();}catch(e){}}
    finalText='';hasSpoken=false;lastRes=Date.now();
    rec=new SR();
    rec.lang='ar-SA';
    rec.continuous=true;
    rec.interimResults=true;
    rec.maxAlternatives=1;

    rec.onstart=function(){
      if(my!==sessionId)return;
      lcSetStatus('يستمع...','listening');
      lcSetHint('تحدث بحرية...');
      lcStartSil();
    };
    rec.onresult=function(e){
      if(my!==sessionId)return;
      var interim='';
      for(var i=e.resultIndex;i<e.results.length;i++){
        if(e.results[i].isFinal){finalText+=e.results[i][0].transcript+' ';hasSpoken=true;}
        else interim+=e.results[i][0].transcript;
      }
      lastRes=Date.now();
      var show=(finalText+interim).trim();
      document.getElementById('lcYou').textContent=show||'...';
    };
    rec.onend=function(){
      if(my!==sessionId)return;
      if(active&&!paused&&!busy){
        setTimeout(function(){
          if(my===sessionId&&active&&!paused&&!busy)lcStartRec(my);
        },RES);
      }
    };
    rec.onerror=function(e){
      if(my!==sessionId)return;
      if(e.error==='not-allowed'){
        lcSetStatus('الميكروفون محجوب','error');
        lcSetHint('اسمح باستخدام الميكروفون');
        window.lcStop();return;
      }
      if(active&&!paused&&!busy){
        setTimeout(function(){
          if(my===sessionId&&active&&!paused&&!busy)lcStartRec(my);
        },RES);
      }
    };
    try{rec.start();}catch(e){}
  }

  function lcStartSil(){
    lcStopSil();
    silTimer=setInterval(function(){
      if(!active||paused||busy)return;
      if(hasSpoken&&finalText.trim()&&(Date.now()-lastRes)>SIL){
        var txt=finalText.trim();
        finalText='';hasSpoken=false;
        lcStopSil();
        if(rec){try{rec.stop();}catch(e){}}
        lcProcess(txt);
      }
    },300);
  }
  function lcStopSil(){if(silTimer){clearInterval(silTimer);silTimer=null;}}

  function lcProcess(text){
    var my=sessionId;
    busy=true;lcStopSil();
    lcSetStatus('يفكر...','thinking');
    lcSetHint('جاري التحليل...');
    document.getElementById('lcYou').textContent=text;

    var img='';
    try{
      var v=document.getElementById('lcVideo');
      if(v&&v.videoWidth>0){
        var cv=document.createElement('canvas');
        var w=Math.min(800,v.videoWidth);
        var h=w*(v.videoHeight/v.videoWidth);
        cv.width=w;cv.height=h;
        cv.getContext('2d').drawImage(v,0,0,w,h);
        img=cv.toDataURL('image/jpeg',0.75);
      }
    }catch(e){}

    fetch('/api/vision',{
      method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+getTok()},
      body:JSON.stringify({
        image:img,
        prompt:'المستخدم قال: "'+text+'". لديك صورة من كاميرته. أجب بالعربية بشكل مختصر (2-3 جمل).'+(img?' اذكر ما تراه في الصورة إن أفاد.' : '')
      })
    })
    .then(function(r){return r.json();})
    .then(function(d){
      if(my!==sessionId)return;
      if(d.reply){
        document.getElementById('lcAI').textContent=d.reply;
        lcSpeak(d.reply);
      }else{
        document.getElementById('lcAI').textContent='خطأ: '+(d.error||'فشل');
        busy=false;
        lcResume();
      }
    })
    .catch(function(err){
      if(my!==sessionId)return;
      document.getElementById('lcAI').textContent='فشل: '+err.message;
      busy=false;
      lcResume();
    });
  }

  function lcSpeak(text){
    if(!('speechSynthesis' in window)){busy=false;lcResume();return;}
    window.speechSynthesis.cancel();
    var clean=String(text)
      .replace(/```[\s\S]*?```/g,' ... كود ... ')
      .replace(/`([^`]+)`/g,'$1')
      .replace(/[*_~#>]/g,'')
      .replace(/\s{2,}/g,' ')
      .trim().slice(0,3000);
    var u=new SpeechSynthesisUtterance(clean);
    u.lang='ar-SA';u.rate=1;u.pitch=1;
    u.onstart=function(){lcSetStatus('يتحدث...','speaking');lcSetHint('🔊 استمع للرد...');};
    u.onend=function(){busy=false;lcResume();};
    u.onerror=function(){busy=false;lcResume();};
    window.speechSynthesis.speak(u);
  }

  function lcResume(){
    if(!active||paused)return;
    setTimeout(function(){
      if(active&&!paused&&!busy){
        lcSetStatus('يستمع...','listening');
        lcSetHint('تحدث بحرية...');
        lcStartRec(sessionId);
      }
    },500);
  }

  window.lcTogglePause=function(){
    if(!active)return;
    paused=!paused;
    var b=document.getElementById('lcPauseBtn');
    if(paused){
      b.textContent='▶️';
      lcSetStatus('موقوف','');
      lcSetHint('اضغط ▶️ للاستئناف');
      lcStopSil();
      if(rec){try{rec.stop();}catch(e){}}
      if('speechSynthesis' in window)window.speechSynthesis.cancel();
      busy=false;
    }else{
      b.textContent='⏸️';
      lcSetStatus('يستمع...','listening');
      lcSetHint('تحدث بحرية...');
      lcResume();
    }
  };

  window.lcStop=function(){
    sessionId++;
    active=false;paused=false;busy=false;
    lcStopSil();
    if(rec){try{rec.abort();}catch(e){}rec=null;}
    if('speechSynthesis' in window)window.speechSynthesis.cancel();
    if(stream){stream.getTracks().forEach(function(t){t.stop();});stream=null;}
    var v=document.getElementById('lcVideo');if(v)v.srcObject=null;
    document.getElementById('lcModal').classList.remove('on');
    document.getElementById('lcYou').textContent='...';
    document.getElementById('lcAI').textContent='...';
    finalText='';hasSpoken=false;
  };
})();
</script>


<button id="pwaInstallBtn" onclick="pwaInstall()" style="display:none;position:fixed;bottom:230px;left:20px;padding:12px 20px;background:linear-gradient(135deg,#10b981,#059669);color:#fff;border:none;border-radius:30px;font-weight:700;font-size:14px;cursor:pointer;box-shadow:0 8px 24px rgba(16,185,129,.5);z-index:900;font-family:inherit;animation:pwaFloat 2s infinite">
📲 ثبّت التطبيق
</button>
<style>
@keyframes pwaFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
</style>

<div class="bg-modal" id="bgModal"><div class="bg-box">
<button class="bg-close" onclick="bgClose()">✕</button>
<h2>🎨 خلفية التطبيق</h2>

<div class="bg-section">
<div class="bg-section-title">🌈 خلفيات جاهزة</div>
<div class="bg-grid" id="bgGrid"></div>
</div>

<div class="bg-section">
<div class="bg-section-title">🖼️ رفع صورة</div>
<button class="bg-btn upload" onclick="bgUpload()">📁 اختيار صورة من الجهاز</button>
<input type="file" id="bgFileInput" accept="image/*" hidden onchange="bgFileChosen(event)">
</div>

<div class="bg-section">
<div class="bg-section-title">🔆 شفافية المحتوى</div>
<span class="bg-opacity-lbl" id="bgOpacityLbl">85%</span>
<input type="range" class="bg-opacity" id="bgOpacitySlider" min="30" max="100" value="85" oninput="bgSetOpacity(this.value)">
</div>

<div class="bg-section">
<button class="bg-btn clear" onclick="bgClear()">🗑️ إزالة الخلفية</button>
</div>

</div></div>


<script id="bg-js">
var BG_PRESETS = [
  {n:'بحر',   v:'linear-gradient(135deg,#1a73e8,#4a90f7,#9b72cb)'},
  {n:'غروب',  v:'linear-gradient(135deg,#f97316,#ef4444,#a855f7)'},
  {n:'غابة',  v:'linear-gradient(135deg,#10b981,#059669,#065f46)'},
  {n:'ليل',   v:'linear-gradient(135deg,#0f172a,#1e293b,#312e81)'},
  {n:'بنفسج', v:'linear-gradient(135deg,#8b5cf6,#a855f7,#ec4899)'},
  {n:'ذهبي',  v:'linear-gradient(135deg,#f59e0b,#d97706,#b45309)'},
  {n:'سماوي', v:'linear-gradient(135deg,#06b6d4,#0891b2,#0369a1)'},
  {n:'وردي',  v:'linear-gradient(135deg,#ec4899,#f472b6,#fb7185)'},
  {n:'فضي',   v:'linear-gradient(135deg,#6b7280,#9ca3af,#d1d5db)'},
  {n:'فحمي',  v:'linear-gradient(135deg,#111827,#1f2937,#374151)'},
  {n:'نعناعي',v:'linear-gradient(135deg,#a7f3d0,#34d399,#10b981)'},
  {n:'كريمي', v:'linear-gradient(135deg,#fef3c7,#fcd34d,#f59e0b)'}
];

function bgApply(bg, opacity){
  var b = document.body;
  if(bg){
    if(bg.indexOf('linear-gradient') === 0 || bg.indexOf('radial-gradient') === 0){
      b.style.backgroundImage = bg;
    } else {
      b.style.backgroundImage = 'url(' + bg + ')';
    }
    b.classList.add('has-bg');
  } else {
    b.style.backgroundImage = '';
    b.classList.remove('has-bg');
  }
  var ov = opacity !== undefined ? opacity : localStorage.getItem('fl_bgOpacity') || 85;
  var style = document.getElementById('bgOverlayStyle');
  if(!style){
    style = document.createElement('style');
    style.id = 'bgOverlayStyle';
    document.head.appendChild(style);
  }
  var alpha = (100 - ov) / 100;
  style.innerHTML = 'body.has-bg::before{background:rgba(255,255,255,' + alpha + ') !important}' +
                    '[data-theme="dark"] body.has-bg::before{background:rgba(15,17,23,' + (alpha + 0.05) + ') !important}';
}

function bgLoad(){
  var bg = localStorage.getItem('fl_bg');
  var op = localStorage.getItem('fl_bgOpacity') || 85;
  if(bg){ bgApply(bg, op); }
  var sl = document.getElementById('bgOpacitySlider');
  if(sl) sl.value = op;
  var lb = document.getElementById('bgOpacityLbl');
  if(lb) lb.textContent = op + '%';
  bgBuildGrid();
}

function bgBuildGrid(){
  var g = document.getElementById('bgGrid');
  if(!g) return;
  var current = localStorage.getItem('fl_bg') || '';
  g.innerHTML = '';
  BG_PRESETS.forEach(function(p){
    var d = document.createElement('div');
    d.className = 'bg-thumb' + (p.v === current ? ' sel' : '');
    d.style.background = p.v;
    d.title = p.n;
    d.onclick = function(){ bgChoose(p.v); };
    g.appendChild(d);
  });
}

function bgChoose(v){
  localStorage.setItem('fl_bg', v);
  bgApply(v);
  bgBuildGrid();
  alert('✅ تم تطبيق الخلفية');
}

function bgUpload(){
  document.getElementById('bgFileInput').click();
}

function bgFileChosen(e){
  var f = e.target.files[0];
  if(!f) return;
  if(f.size > 3 * 1024 * 1024){ alert('الصورة كبيرة جداً (الحد 3 MB)'); return; }
  var r = new FileReader();
  r.onload = function(ev){
    localStorage.setItem('fl_bg', ev.target.result);
    bgApply(ev.target.result);
    alert('✅ تم رفع الخلفية');
  };
  r.readAsDataURL(f);
}

function bgSetOpacity(v){
  localStorage.setItem('fl_bgOpacity', v);
  document.getElementById('bgOpacityLbl').textContent = v + '%';
  var bg = localStorage.getItem('fl_bg');
  if(bg) bgApply(bg, v);
}

function bgClear(){
  if(!confirm('إزالة الخلفية؟')) return;
  localStorage.removeItem('fl_bg');
  bgApply(null);
  bgBuildGrid();
}

function bgOpen(){
  bgBuildGrid();
  document.getElementById('bgModal').classList.add('on');
}

function bgClose(){
  document.getElementById('bgModal').classList.remove('on');
}

document.addEventListener('DOMContentLoaded', bgLoad);
setTimeout(bgLoad, 500);
</script>

</body>
</html>
"""




# ============================================================
# 📱 PWA Routes
# ============================================================
MANIFEST = {
    "name": "Flash-Lite",
    "short_name": "Flash-Lite",
    "description": "مساعدك الذكي - إعداد معتصم علي",
    "start_url": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#1a73e8",
    "theme_color": "#1a73e8",
    "lang": "ar",
    "dir": "rtl",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
    ]
}


@app.route('/manifest.json')
def manifest():
    return jsonify(MANIFEST)


@app.route('/sw.js')
def service_worker():
    sw = """
const CACHE = 'flashlite-v1';
self.addEventListener('install', e => {
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(clients.claim());
});
self.addEventListener('fetch', e => {
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
"""
    return Response(sw, mimetype='application/javascript')


@app.route('/icon-192.png')
def icon192():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
<stop offset="0%" stop-color="#1a73e8"/><stop offset="100%" stop-color="#9b72cb"/>
</linearGradient></defs>
<rect width="192" height="192" rx="42" fill="url(#g)"/>
<text x="96" y="130" font-size="110" text-anchor="middle" fill="white" font-family="Arial">⚡</text>
</svg>"""
    return Response(svg, mimetype='image/svg+xml')


@app.route('/icon-512.png')
def icon512():
    return icon192()



@app.route('/')
def home():
    return render_template_string(HOME_HTML)


@app.route('/api/status')
def api_status():
    return jsonify({"status": "ok", "model": AVAILABLE_MODEL or "غير محدد"})


# ============================================================
# 🔴 البث المباشر
# ============================================================
@app.route('/api/chat_stream', methods=['POST'])
@require_user
def chat_stream():
    data = request.get_json() or {}
    prompt = data.get('prompt', '').strip()
    chat_id = data.get('chat_id')
    if not prompt:
        return Response("data: [DONE]\n\n", mimetype='text/event-stream')

    # حفظ رسالة المستخدم — اتصال مستقل
    if chat_id:
        try:
            con = sqlite3.connect(DB_PATH)
            con.execute("INSERT INTO messages (chat_id, role, content) VALUES (?, 'user', ?)",
                        (chat_id, prompt))
            con.execute("UPDATE chats SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (chat_id,))
            con.commit()
            con.close()
        except Exception as e:
            print(f"DB error (user): {e}")

    def sse_text(text):
        for chunk in [text[i:i+8] for i in range(0, len(text), 8)]:
            yield f"data: {json.dumps({'text': chunk})}\n\n"
        yield "data: [DONE]\n\n"

    # ردود محلية
    if any(k in prompt for k in ["من صممك", "صممك"]):
        msg = "أنا من تصميم **معتصم علي** 🚀"
        if chat_id:
            try:
                con = sqlite3.connect(DB_PATH)
                con.execute("INSERT INTO messages (chat_id, role, content) VALUES (?, 'ai', ?)",
                            (chat_id, msg))
                con.commit()
                con.close()
            except Exception as e:
                print(f"DB error (local): {e}")
        return Response(stream_with_context(sse_text(msg)), mimetype='text/event-stream')

    def generate():
        primary = detect_best_model()
        models_list = [primary] if primary else []
        for m in MODEL_QUEUE:
            if m not in models_list:
                models_list.append(m)
        full_reply = ""
        for try_model in models_list:
            try:
                url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                       f"{try_model}:streamGenerateContent?key={API_KEY}&alt=sse")
                payload = {"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"temperature": 0.9, "maxOutputTokens": 2048, "topP": 0.95}}
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode('utf-8'),
                    headers={"Content-Type": "application/json"}, method="POST"
                )
                got = False
                with urllib.request.urlopen(req, timeout=60) as resp:
                    buffer = b''
                    for raw in resp:
                        buffer += raw
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            line = line.strip()
                            if not line or not line.startswith(b'data: '):
                                continue
                            try:
                                chunk = json.loads(line[6:].decode('utf-8'))
                                text = chunk['candidates'][0]['content']['parts'][0].get('text', '')
                                if text:
                                    got = True
                                    full_reply += text
                                    yield f"data: {json.dumps({'text': text})}\\n\\n"
                            except (KeyError, IndexError, json.JSONDecodeError):
                                continue
                if got:
                    if chat_id and full_reply:
                        try:
                            con = sqlite3.connect(DB_PATH)
                            con.execute("INSERT INTO messages (chat_id, role, content, model) VALUES (?, 'ai', ?, ?)",
                                        (chat_id, full_reply, try_model))
                            con.execute("UPDATE chats SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (chat_id,))
                            con.commit()
                            con.close()
                        except Exception as e:
                            print(f"DB error (ai): {e}")
                    yield "data: [DONE]\\n\\n"
                    return
            except Exception as e:
                print(f"FAIL {try_model}: {str(e)[:80]}")
                continue
        yield f"data: {json.dumps({'error': 'All models busy'})}\\n\\n"
        yield "data: [DONE]\\n\\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'}
    )




@app.route('/api/vision', methods=['POST'])
@require_user
def vision():
    data = request.get_json() or {}
    image_b64 = data.get('image', '')
    prompt = data.get('prompt', 'Describe the image in Arabic')
    if not image_b64:
        return jsonify({"error": "No image"}), 400
    if ',' in image_b64:
        image_b64 = image_b64.split(',', 1)[1]
    model = detect_best_model()
    if not model:
        return jsonify({"error": "No model"}), 500
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}"
        payload = {"contents": [{"parts": [
            {"text": prompt},
            {"inlineData": {"mimeType": "image/jpeg", "data": image_b64}}
        ]}], "generationConfig": {"temperature": 0.7, "maxOutputTokens": 512}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                      headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode())
            reply = res['candidates'][0]['content']['parts'][0]['text']
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    print("=" * 60)
    print("⚡ Flash-Lite — النسخة الكاملة")
    print("=" * 60)
    init_db()
    print("✅ قاعدة البيانات جاهزة")
    m = detect_best_model()
    if m:
        print(f"✅ الموديل النشط: {m}")
    print("=" * 60)
    print("🌐 http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True, use_reloader=False)

