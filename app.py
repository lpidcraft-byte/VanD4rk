# ============================================================
#  VanD4rk Hosting - Telegram Bot Hosting Platform
#  Tek dosya · Flask + SQLite'sız JSON store · Railway uyumlu
# ============================================================

import os, sys, json, time, uuid, hashlib, secrets, threading
import subprocess, shutil, re, signal, asyncio
from datetime import datetime
from functools import wraps
from pathlib import Path
from flask import (Flask, request, jsonify, session, render_template,
                   redirect, url_for, send_from_directory, abort)
import urllib.request, urllib.parse

# ---------- CONFIG ----------
ADMIN_BOT_TOKEN = "9227195723:AAEikeBupi_sTPH55R4O2dm3E5QJo1N1CN4"
ADMIN_CHAT_ID   = 8913966694
BASE_DIR        = Path(os.environ.get("DATA_DIR", "./vand4rk_data"))
BASE_DIR.mkdir(parents=True, exist_ok=True)
USERS_DB        = BASE_DIR / "users.json"
BOTS_DB         = BASE_DIR / "bots.json"
PENDING_DB      = BASE_DIR / "pending.json"
LOGS_DB         = BASE_DIR / "logs.json"
BOTS_DIR        = BASE_DIR / "bots"
BOTS_DIR.mkdir(exist_ok=True)

MAX_RAM_MB      = 7000          # 7 GB toplam tahsis
SECRET_KEY      = "vand4rk-super-secret-key-change-me"

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB upload

# ---------- JSON STORE ----------
_lock = threading.RLock()

def _load(path, default):
    if not path.exists(): return default
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return default

def _save(path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

def load_users():  return _load(USERS_DB, {})
def save_users(u):  _save(USERS_DB, u)
def load_bots():   return _load(BOTS_DB, {})
def save_bots(b):   _save(BOTS_DB, b)
def load_pending():return _load(PENDING_DB, {})
def save_pending(p):_save(PENDING_DB, p)
def load_logs():   return _load(LOGS_DB, [])
def save_logs(l):   _save(LOGS_DB, l[-2000:])

def add_log(user_id, action, detail=""):
    with _lock:
        logs = load_logs()
        logs.append({"ts": datetime.utcnow().isoformat(), "user": user_id,
                     "action": action, "detail": detail})
        save_logs(logs)

# ---------- TELEGRAM API ----------
def tg(method, payload, token=None, timeout=15):
    token = token or ADMIN_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}

def get_bot_info(token):
    r = tg("getMe", {}, token=token)
    if r.get("ok"): return r["result"]
    return None

def get_bot_avatar_url(bot_info):
    # Telegram botlarının avatarını çekmek için getChat + getFile
    try:
        r = tg("getChat", {"chat_id": bot_info["id"]}, token=ADMIN_BOT_TOKEN)
        if not r.get("ok"): return ""
        photo = r["result"].get("photo", {})
        if not photo: return ""
        biggest = max(photo.values(), key=lambda x: x.get("file_size", 0))
        fr = tg("getFile", {"file_id": biggest["file_id"]})
        if not fr.get("ok"): return ""
        fp = fr["result"]["file_path"]
        return f"https://api.telegram.org/file/bot{ADMIN_BOT_TOKEN}/{fp}"
    except: return ""

def notify_admin(text, reply_markup=None):
    payload = {"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    return tg("sendMessage", payload)

# ---------- TOKEN PARSER ----------
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

def extract_token(code: str):
    for line in code.splitlines():
        s = line.strip().strip("'\"")
        if TOKEN_RE.match(s):
            return s
    m = TOKEN_RE.search(code)
    return m.group(0) if m else None

# ---------- PROCESS MANAGER ----------
PROCS = {}   # bot_id -> subprocess

def bot_path(bot_id): return BOTS_DIR / bot_id

def stop_proc(bot_id):
    p = PROCS.get(bot_id)
    if not p: return
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        try: p.wait(5)
        except: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except: pass
    PROCS.pop(bot_id, None)

def start_proc(bot_id):
    stop_proc(bot_id)
    bdir = bot_path(bot_id)
    main_py = bdir / "bot.py"
    if not main_py.exists():
        # .py dosyası ne olursa olsun ilk .py'yi bot.py yap
        pys = list(bdir.glob("*.py"))
        if pys: pys[0].rename(main_py)
    if not main_py.exists(): return False, "bot.py bulunamadı"
    log = open(bdir / "console.log", "a", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        p = subprocess.Popen(
            [sys.executable, "-u", str(main_py)],
            cwd=str(bdir), stdout=log, stderr=subprocess.STDOUT,
            env=env, preexec_fn=os.setsid,
        )
        PROCS[bot_id] = p
        return True, f"PID {p.pid}"
    except Exception as e:
        return False, str(e)

def proc_status(bot_id):
    p = PROCS.get(bot_id)
    if p and p.poll() is None: return "running"
    return "stopped"

# ---------- AUTH ----------
def hash_pw(pw): return hashlib.sha256((pw + SECRET_KEY).encode()).hexdigest()

def current_user():
    uid = session.get("uid")
    if not uid: return None
    return load_users().get(uid)

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not current_user(): return redirect(url_for("login_page"))
        return f(*a, **k)
    return w

# ============================================================
#  ROUTES
# ============================================================
@app.route("/")
def index():
    if current_user(): return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        d = request.get_json() or request.form
        username = (d.get("username") or "").strip().lower()
        pw = d.get("password") or ""
        users = load_users()
        if username not in users:
            return jsonify({"ok": False, "error": "Kullanıcı bulunamadı"}), 400
        if users[username]["password"] != hash_pw(pw):
            return jsonify({"ok": False, "error": "Şifre hatalı"}), 400
        session["uid"] = username
        add_log(username, "login")
        return jsonify({"ok": True, "redirect": "/dashboard"})
    return render_template("auth.html", mode="login")

@app.route("/register", methods=["POST"])
def register_api():
    d = request.get_json() or request.form
    username = (d.get("username") or "").strip().lower()
    pw = d.get("password") or ""
    if not re.match(r"^[a-z0-9_]{3,20}$", username):
        return jsonify({"ok": False, "error": "Kullanıcı adı 3-20 karakter, harf/rakam/_"}), 400
    if len(pw) < 6:
        return jsonify({"ok": False, "error": "Şifre en az 6 karakter"}), 400
    with _lock:
        users = load_users()
        if username in users:
            return jsonify({"ok": False, "error": "Bu kullanıcı adı alınmış"}), 400
        users[username] = {"password": hash_pw(pw), "created": datetime.utcnow().isoformat(),
                           "bots": []}
        save_users(users)
    add_log(username, "register")
    session["uid"] = username
    return jsonify({"ok": True, "redirect": "/dashboard"})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=current_user())

# ---------- BOTLAR ----------
@app.route("/api/bots")
@login_required
def api_bots():
    u = current_user()
    bots = load_bots()
    out = []
    for bid in u["bots"]:
        b = bots.get(bid)
        if not b: continue
        out.append({**b, "status": proc_status(bid)})
    return jsonify({"ok": True, "bots": out})

@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    u = current_user()
    username = session["uid"]
    file = request.files.get("file")
    if not file or not file.filename.endswith(".py"):
        return jsonify({"ok": False, "error": "Sadece .py dosyası yüklenebilir"}), 400
    code = file.read().decode("utf-8", errors="ignore")
    token = extract_token(code)
    if not token:
        return jsonify({"ok": False, "error": "Geçerli bir bot token bulunamadı"}), 400
    info = get_bot_info(token)
    if not info:
        return jsonify({"ok": False, "error": "Token geçersiz (BotFather'dan alınan token gerekli)"}), 400

    bot_id = "bot_" + uuid.uuid4().hex[:10]
    bdir = bot_path(bot_id); bdir.mkdir(parents=True)
    (bdir / "bot.py").write_text(code, encoding="utf-8")

    avatar = get_bot_avatar_url(info)
    record = {
        "id": bot_id, "owner": username, "token": token,
        "name": info.get("first_name", "Bot"),
        "username": "@" + info.get("username", ""),
        "tg_id": info["id"],
        "avatar": avatar,
        "created": datetime.utcnow().isoformat(),
        "updated": datetime.utcnow().isoformat(),
        "status": "pending_approval",
        "approved": False,
    }
    with _lock:
        bots = load_bots(); bots[bot_id] = record; save_bots(bots)
        users = load_users(); users[username]["bots"].append(bot_id); save_users(users)
        pend = load_pending()
        pend[bot_id] = {"type": "upload", "user": username,
                        "bot_id": bot_id, "name": record["name"], "ts": time.time()}
        save_pending(pend)
    add_log(username, "upload", bot_id)

    # Admin onay mesajı
    notify_admin(
        f"🛰️ <b>VanD4rk Hosting — Yeni Bot Onayı</b>\n\n"
        f"👤 Kullanıcı: <code>{username}</code>\n"
        f"🤖 Bot: <b>{record['name']}</b> ({record['username']})\n"
        f"🆔 Bot ID: <code>{record['tg_id']}</code>\n"
        f"📄 Dosya: <code>{file.filename}</code>\n\n"
        f"Bu dosya onaylansın mı?",
        {"inline_keyboard": [[
            {"text": "✅ Onayla", "callback_data": f"approve:{bot_id}"},
            {"text": "❌ Reddet", "callback_data": f"reject:{bot_id}"}
        ]]}
    )
    return jsonify({"ok": True, "bot": record, "msg": "Admin onayı bekleniyor"})

@app.route("/api/bot/<bid>")
@login_required
def api_bot_detail(bid):
    u = current_user(); bots = load_bots()
    b = bots.get(bid)
    if not b or b["owner"] != session["uid"]:
        return jsonify({"ok": False}), 404
    b = {**b, "status": proc_status(bid)}
    log_file = bot_path(bid) / "console.log"
    tail = ""
    if log_file.exists():
        with open(log_file, "r", errors="ignore") as f:
            tail = "".join(f.readlines()[-80:])
    return jsonify({"ok": True, "bot": b, "console": tail})

@app.route("/api/bot/<bid>/restart", methods=["POST"])
@login_required
def api_restart(bid):
    bots = load_bots(); b = bots.get(bid)
    if not b or b["owner"] != session["uid"]: return jsonify({"ok": False}), 404
    if not b.get("approved"): return jsonify({"ok": False, "error": "Bot onaylanmadı"}), 403
    ok, msg = start_proc(bid)
    add_log(session["uid"], "restart", f"{bid}:{msg}")
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/bot/<bid>/stop", methods=["POST"])
@login_required
def api_stop(bid):
    bots = load_bots(); b = bots.get(bid)
    if not b or b["owner"] != session["uid"]: return jsonify({"ok": False}), 404
    stop_proc(bid)
    add_log(session["uid"], "stop", bid)
    return jsonify({"ok": True})

@app.route("/api/bot/<bid>/delete", methods=["POST"])
@login_required
def api_delete(bid):
    bots = load_bots(); b = bots.get(bid)
    if not b or b["owner"] != session["uid"]: return jsonify({"ok": False}), 404
    stop_proc(bid)
    shutil.rmtree(bot_path(bid), ignore_errors=True)
    with _lock:
        bots = load_bots(); bots.pop(bid, None); save_bots(bots)
        users = load_users()
        if bid in users[session["uid"]]["bots"]:
            users[session["uid"]]["bots"].remove(bid)
        save_users(users)
    add_log(session["uid"], "delete", bid)
    return jsonify({"ok": True})

@app.route("/api/bot/<bid>/update", methods=["POST"])
@login_required
def api_update(bid):
    bots = load_bots(); b = bots.get(bid)
    if not b or b["owner"] != session["uid"]: return jsonify({"ok": False}), 404
    file = request.files.get("file")
    if not file or not file.filename.endswith(".py"):
        return jsonify({"ok": False, "error": "Sadece .py"}), 400
    code = file.read().decode("utf-8", errors="ignore")
    token = extract_token(code)
    if not token:
        return jsonify({"ok": False, "error": "Token bulunamadı"}), 400
    (bot_path(bid) / "bot.py").write_text(code, encoding="utf-8")
    with _lock:
        bots = load_bots(); bots[bid]["updated"] = datetime.utcnow().isoformat()
        bots[bid]["approved"] = False  # güncelleme de onay ister
        save_bots(bots)
        pend = load_pending()
        pend[bid + "_upd_" + uuid.uuid4().hex[:6]] = {
            "type": "update", "user": session["uid"], "bot_id": bid,
            "name": bots[bid]["name"], "ts": time.time()}
        save_pending(pend)
    add_log(session["uid"], "update", bid)
    notify_admin(
        f"♻️ <b>Bot Güncelleme Onayı</b>\n\n"
        f"👤 Kullanıcı: <code>{session['uid']}</code>\n"
        f"🤖 Bot: <b>{bots[bid]['name']}</b>\n\n"
        f"Bu dosya onaylansın mı?",
        {"inline_keyboard": [[
            {"text": "✅ Onayla", "callback_data": f"approve:{bid}"},
            {"text": "❌ Reddet", "callback_data": f"reject:{bid}"}
        ]]}
    )
    return jsonify({"ok": True, "msg": "Güncelleme admin onayına gönderildi"})

# ---------- ADMIN WEBHOOK (Telegram callback) ----------
@app.route("/tg/webhook", methods=["POST"])
def tg_webhook():
    update = request.get_json(force=True, silent=True) or {}
    cb = update.get("callback_query")
    if not cb: return jsonify({"ok": True})
    data = cb.get("data", "")
    cb_id = cb["id"]
    if ":" not in data: 
        tg("answerCallbackQuery", {"callback_query_id": cb_id}); return jsonify({"ok": True})
    action, bot_id = data.split(":", 1)
    with _lock:
        bots = load_bots(); pend = load_pending()
        b = bots.get(bot_id)
    if not b:
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Bot bulunamadı"})
        return jsonify({"ok": True})
    if action == "approve":
        b["approved"] = True
        b["status"] = "running"
        with _lock:
            bots[bot_id] = b; save_bots(bots)
        ok, msg = start_proc(bot_id)
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Onaylandı ✅"})
        tg("editMessageReplyMarkup", {"chat_id": cb["message"]["chat"]["id"],
            "message_id": cb["message"]["message_id"], "reply_markup": json.dumps({"inline_keyboard": []})})
        tg("sendMessage", {"chat_id": cb["message"]["chat"]["id"],
            "text": f"✅ Onaylandı ve başlatıldı: {b['name']}\nSonuç: {msg}"})
        add_log("admin", "approve", bot_id)
    elif action == "reject":
        stop_proc(bot_id)
        b["approved"] = False; b["status"] = "rejected"
        with _lock:
            bots[bot_id] = b; save_bots(bots)
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Reddedildi ❌"})
        tg("editMessageReplyMarkup", {"chat_id": cb["message"]["chat"]["id"],
            "message_id": cb["message"]["message_id"], "reply_markup": json.dumps({"inline_keyboard": []})})
        add_log("admin", "reject", bot_id)
    return jsonify({"ok": True})

@app.route("/tg/setup", methods=["GET"])
def tg_setup():
    """Admin webhook'unu ayarlamak için bir defa çağır."""
    host = request.host_url.rstrip("/")
    r = tg("setWebhook", {"url": f"{host}/tg/webhook",
                          "allowed_updates": json.dumps(["callback_query"])})
    return jsonify(r)

# ---------- SYSTEM INFO ----------
@app.route("/api/system")
@login_required
def api_system():
    import psutil
    try:
        vm = psutil.virtual_memory()
        du = shutil.disk_usage(str(BASE_DIR))
        cpu = psutil.cpu_percent(interval=0.3)
        return jsonify({"ok": True, "ram_used": vm.used, "ram_total": vm.total,
                        "disk_used": du.used, "disk_total": du.total, "cpu": cpu,
                        "procs": len(PROCS), "max_ram_mb": MAX_RAM_MB})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/healthz")
def healthz(): return "ok"

# ---------- START ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
