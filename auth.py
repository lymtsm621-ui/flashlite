import sqlite3, hashlib, secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify, g

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')
DB_PATH = None

def set_db_path(p):
   global DB_PATH
   DB_PATH = p

def get_db():
   c = sqlite3.connect(DB_PATH)
   c.row_factory = sqlite3.Row
   return c

def hash_pw(pw):
   salt = secrets.token_hex(16)
   h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100000).hex()
   return h, salt

def verify_pw(pw, salt, h):
   c = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100000).hex()
   return secrets.compare_digest(c, h)

def create_token(uid):
   t = secrets.token_urlsafe(32)
   e = datetime.now() + timedelta(days=30)
   conn = get_db()
   conn.execute("INSERT INTO sessions (token,user_id,expires_at) VALUES (?,?,?)", (t,uid,e.isoformat()))
   conn.commit(); conn.close()
   return t

def get_user_from_token(t):
   if not t: return None
   conn = get_db()
   r = conn.execute("SELECT user_id,expires_at FROM sessions WHERE token=?", (t,)).fetchone()
   if not r:
       conn.close(); return None
   if datetime.fromisoformat(r['expires_at']) < datetime.now():
       conn.execute("DELETE FROM sessions WHERE token=?", (t,))
       conn.commit(); conn.close(); return None
   u = conn.execute("SELECT id,username,email FROM users WHERE id=?", (r['user_id'],)).fetchone()
   conn.close()
   return dict(u) if u else None

def get_current_user():
   a = request.headers.get('Authorization','')
   if not a.startswith('Bearer '): return None
   return get_user_from_token(a[7:])

def require_auth(f):
   @wraps(f)
   def w(*a,**k):
       u = get_current_user()
       if not u:
           return jsonify({"error":"يجب تسجيل الدخول"}),401
       g.user = u
       return f(*a,**k)
   return w

@auth_bp.route('/register', methods=['POST'])
def register():
   d = request.get_json() or {}
   u = (d.get('username') or '').strip()
   p = d.get('password') or ''
   e = (d.get('email') or '').strip() or None
   if not u or not p:
       return jsonify({"error":"الحقول مطلوبة"}),400
   if len(u) < 3 or len(u) > 30:
       return jsonify({"error":"اسم المستخدم 3-30 حرف"}),400
   if len(p) < 6:
       return jsonify({"error":"كلمة المرور 6 أحرف على الأقل"}),400
   conn = get_db()
   if conn.execute("SELECT id FROM users WHERE username=?",(u,)).fetchone():
       conn.close()
       return jsonify({"error":"الاسم محجوز"}),400
   h,s = hash_pw(p)
   try:
       c = conn.execute("INSERT INTO users (username,password_hash,salt,email) VALUES (?,?,?,?)",(u,h,s,e))
       uid = c.lastrowid
       conn.execute("INSERT INTO user_settings (user_id) VALUES (?)",(uid,))
       conn.commit()
       t = create_token(uid)
       conn.close()
       return jsonify({"success":True,"token":t,"user":{"id":uid,"username":u,"email":e},"message":f"مرحباً {u}!"})
   except Exception as ex:
       conn.close()
       return jsonify({"error":str(ex)}),500

@auth_bp.route('/login', methods=['POST'])
def login():
   d = request.get_json() or {}
   u = (d.get('username') or '').strip()
   p = d.get('password') or ''
   if not u or not p:
       return jsonify({"error":"الحقول مطلوبة"}),400
   conn = get_db()
   r = conn.execute("SELECT id,username,email,password_hash,salt FROM users WHERE username=?",(u,)).fetchone()
   if not r or not verify_pw(p, r['salt'], r['password_hash']):
       conn.close()
       return jsonify({"error":"بيانات خاطئة"}),401
   conn.execute("UPDATE users SET last_login=? WHERE id=?",(datetime.now().isoformat(),r['id']))
   conn.commit()
   t = create_token(r['id'])
   conn.close()
   return jsonify({"success":True,"token":t,"user":{"id":r['id'],"username":r['username'],"email":r['email']},"message":f"مرحباً {r['username']}!"})

@auth_bp.route('/logout', methods=['POST'])
def logout():
   a = request.headers.get('Authorization','')
   if a.startswith('Bearer '):
       conn = get_db()
       conn.execute("DELETE FROM sessions WHERE token=?",(a[7:],))
       conn.commit()
       conn.close()
   return jsonify({"success":True})

@auth_bp.route('/me', methods=['GET'])
@require_auth
def me():
   return jsonify({"user":g.user})



# ============================================================
# 🔐 تسجيل الدخول بـ Google
# ============================================================
GOOGLE_CLIENT_ID = None


def set_google_client_id(cid):
    global GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_ID = cid


@auth_bp.route('/google', methods=['POST'])
def google_login():
    import json as js
    import urllib.request as ur
    d = request.get_json() or {}
    id_token = (d.get('credential') or '').strip()
    if not id_token:
        return jsonify({"error": "التوكن مطلوب"}), 400
    try:
        url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + id_token
        with ur.urlopen(url, timeout=10) as r:
            info = js.loads(r.read().decode())
    except Exception as e:
        return jsonify({"error": "توكن غير صالح"}), 401

    if info.get('aud') != GOOGLE_CLIENT_ID:
        return jsonify({"error": "Client ID غير مطابق"}), 401

    email = info.get('email')
    name = info.get('name') or (email.split('@')[0] if email else 'user')
    if not email:
        return jsonify({"error": "لا يوجد بريد"}), 400

    c = get_db()
    r = c.execute("SELECT id, username FROM users WHERE username=? OR email=?", (email, email)).fetchone()
    if not r:
        import secrets as sc
        import hashlib as hl
        random_pw = sc.token_hex(32)
        salt = sc.token_hex(16)
        h = hl.pbkdf2_hmac('sha256', random_pw.encode(), salt.encode(), 100000).hex()
        cur = c.execute("INSERT INTO users (username, email, password_hash, salt) VALUES (?, ?, ?, ?)", (email, email, h, salt))
        uid = cur.lastrowid
        c.execute("INSERT INTO user_settings (user_id) VALUES (?)", (uid,))
        c.commit()
        username = email
    else:
        uid = r['id']
        username = r['username']

    import secrets as sc2
    from datetime import datetime as dt, timedelta as td
    t = sc2.token_urlsafe(32)
    e = (dt.now() + td(days=30)).isoformat()
    c.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)", (t, uid, e))
    c.commit()
    c.close()

    return jsonify({
        "success": True,
        "token": t,
        "user": {"id": uid, "username": username, "email": email, "name": name},
        "message": "مرحبا " + name
    })
