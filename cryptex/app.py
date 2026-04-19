"""
CRYPTEX - GTA RP Krypto Boerse
Starten: python3 app.py
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit
import sqlite3, hashlib, os, json, time, threading, requests
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*")

# ═══════════════════════════════════════════
#   KONFIGURATION
# ═══════════════════════════════════════════

try:
    from config import DISCORD_SECRET
except:
    DISCORD_SECRET = ""

DISCORD_CLIENT_ID  = "1485940671545086002"
DISCORD_REDIRECT   = "http://cryptex-gta.duckdns.org/callback"
DB_FILE            = "cryptex.db"

COINS = {
    "BTC": {"name": "Bitcoin",  "start": 67000.0, "vol": 0.003, "spread": 0.002},
    "ETH": {"name": "Ethereum", "start": 3500.0,  "vol": 0.004, "spread": 0.002},
    "SOL": {"name": "Solana",   "start": 170.0,   "vol": 0.005, "spread": 0.003},
    "BNB": {"name": "BNB",      "start": 600.0,   "vol": 0.003, "spread": 0.003},
    "ADA": {"name": "Cardano",  "start": 0.48,    "vol": 0.006, "spread": 0.004},
    "XRP": {"name": "Ripple",   "start": 0.52,    "vol": 0.005, "spread": 0.004},
}

# ═══════════════════════════════════════════
#   DATENBANK
# ═══════════════════════════════════════════

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT UNIQUE,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            rolle TEXT DEFAULT 'user',
            aktiv INTEGER DEFAULT 1,
            erstellt TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS konto (
            user_id INTEGER PRIMARY KEY,
            cash REAL DEFAULT 0,
            eingezahlt REAL DEFAULT 0,
            ausgezahlt REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS depot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            coin TEXT,
            menge REAL DEFAULT 0,
            avg_kaufpreis REAL DEFAULT 0,
            UNIQUE(user_id, coin)
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            coin TEXT,
            richtung TEXT,
            menge REAL,
            preis REAL,
            zeitpunkt TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS markt (
            coin TEXT PRIMARY KEY,
            preis REAL,
            history TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            zeitpunkt TEXT DEFAULT CURRENT_TIMESTAMP,
            discord_id TEXT,
            betrag REAL
        );
        """)

        # Markt initialisieren
        for sym, info in COINS.items():
            c.execute("INSERT OR IGNORE INTO markt (coin, preis, history) VALUES (?, ?, ?)",
                      (sym, info["start"], json.dumps([info["start"]] * 20)))

        # Admin erstellen
        pw = hashlib.sha256("admin124".encode()).hexdigest()
        c.execute("INSERT OR IGNORE INTO users (username, password_hash, rolle) VALUES ('admin', ?, 'superadmin')", (pw,))
        c.execute("INSERT OR IGNORE INTO konto (user_id) SELECT id FROM users WHERE username='admin'")

# ═══════════════════════════════════════════
#   MARKT ENGINE
# ═══════════════════════════════════════════

def markt_tick():
    while True:
        time.sleep(30)
        try:
            with db() as c:
                for sym, info in COINS.items():
                    row = c.execute("SELECT preis, history FROM markt WHERE coin=?", (sym,)).fetchone()
                    if not row:
                        continue
                    p = row["preis"]
                    abw = (p - info["start"]) / info["start"]
                    reversion = -abw * 0.01
                    import random
                    noise = (random.random() - 0.5) * info["vol"] * 0.3
                    neu = max(info["start"] * 0.5, min(info["start"] * 2.0, p * (1 + reversion + noise)))
                    neu = round(neu, 6)
                    hist = json.loads(row["history"])
                    hist.append(neu)
                    if len(hist) > 60:
                        hist = hist[-60:]
                    c.execute("UPDATE markt SET preis=?, history=? WHERE coin=?", (neu, json.dumps(hist), sym))
            broadcast_markt()
        except:
            pass

def broadcast_markt():
    try:
        with db() as c:
            rows = c.execute("SELECT * FROM markt").fetchall()
        data = {}
        for r in rows:
            info = COINS.get(r["coin"], {})
            spread = info.get("spread", 0.002)
            p = r["preis"]
            hist = json.loads(r["history"])
            chg = ((hist[-1] - hist[0]) / hist[0] * 100) if len(hist) > 1 else 0
            data[r["coin"]] = {
                "preis": round(p, 6),
                "bid": round(p * (1 - spread), 6),
                "ask": round(p * (1 + spread), 6),
                "chg": round(chg, 2),
                "history": hist[-30:],
                "name": info.get("name", r["coin"]),
            }
        socketio.emit("markt", data)
    except:
        pass

# ═══════════════════════════════════════════
#   HELPER
# ═══════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        with db() as c:
            u = c.execute("SELECT rolle FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not u or u["rolle"] not in ("admin", "superadmin", "mitarbeiter"):
            return jsonify({"error": "Kein Zugriff"}), 403
        return f(*args, **kwargs)
    return decorated

# ═══════════════════════════════════════════
#   SEITEN
# ═══════════════════════════════════════════

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def login_post():
    data = request.json
    username = data.get("username", "").strip()
    pw_hash = hashlib.sha256(data.get("password", "").encode()).hexdigest()
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=? AND password_hash=? AND aktiv=1", (username, pw_hash)).fetchone()
    if not u:
        return jsonify({"error": "Falscher Benutzername oder Passwort"}), 401
    session["user_id"] = u["id"]
    session["username"] = u["username"]
    session["rolle"] = u["rolle"]
    return jsonify({"ok": True, "rolle": u["rolle"]})

@app.route("/discord/login")
def discord_login():
    return redirect(
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT}"
        f"&response_type=code&scope=identify"
    )

@app.route("/callback")
def discord_callback():
    code = request.args.get("code")
    if not code or not DISCORD_SECRET:
        return redirect(url_for("login_page"))
    try:
        r = requests.post("https://discord.com/api/oauth2/token", data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = r.json()["access_token"]
        ur = requests.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token}"})
        duser = ur.json()
        discord_id = duser["id"]
        username = duser["username"]
        with db() as c:
            u = c.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,)).fetchone()
            if not u:
                c.execute("INSERT OR IGNORE INTO users (discord_id, username, rolle) VALUES (?, ?, 'user')", (discord_id, username))
                uid = c.execute("SELECT id FROM users WHERE discord_id=?", (discord_id,)).fetchone()["id"]
                c.execute("INSERT OR IGNORE INTO konto (user_id) VALUES (?)", (uid,))
            else:
                uid = u["id"]
            u2 = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        session["user_id"] = uid
        session["username"] = u2["username"]
        session["rolle"] = u2["rolle"]
        return redirect(url_for("dashboard"))
    except:
        return redirect(url_for("login_page"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", username=session["username"], rolle=session["rolle"])

@app.route("/admin")
@login_required
def admin_page():
    if session.get("rolle") not in ("admin", "superadmin", "mitarbeiter"):
        return redirect(url_for("dashboard"))
    return render_template("admin.html", username=session["username"], rolle=session["rolle"])

# ═══════════════════════════════════════════
#   API - MARKT
# ═══════════════════════════════════════════

@app.route("/api/markt")
@login_required
def api_markt():
    with db() as c:
        rows = c.execute("SELECT * FROM markt").fetchall()
    result = {}
    for r in rows:
        info = COINS.get(r["coin"], {})
        spread = info.get("spread", 0.002)
        p = r["preis"]
        hist = json.loads(r["history"])
        chg = ((hist[-1] - hist[0]) / hist[0] * 100) if len(hist) > 1 else 0
        result[r["coin"]] = {
            "preis": round(p, 6),
            "bid": round(p * (1 - spread), 6),
            "ask": round(p * (1 + spread), 6),
            "chg": round(chg, 2),
            "history": hist[-30:],
            "name": info.get("name", r["coin"]),
        }
    return jsonify(result)

# ═══════════════════════════════════════════
#   API - KONTO & DEPOT
# ═══════════════════════════════════════════

@app.route("/api/konto")
@login_required
def api_konto():
    uid = session["user_id"]
    with db() as c:
        k = c.execute("SELECT * FROM konto WHERE user_id=?", (uid,)).fetchone()
        depot = c.execute("SELECT * FROM depot WHERE user_id=? AND menge > 0", (uid,)).fetchall()
        markt = c.execute("SELECT * FROM markt").fetchall()
    markt_map = {m["coin"]: m["preis"] for m in markt}
    depot_data = []
    depot_wert = 0
    for pos in depot:
        p = markt_map.get(pos["coin"], 0)
        wert = pos["menge"] * p
        depot_wert += wert
        gewinn = (p - pos["avg_kaufpreis"]) * pos["menge"]
        depot_data.append({
            "coin": pos["coin"],
            "menge": pos["menge"],
            "avg_kp": pos["avg_kaufpreis"],
            "aktuell": p,
            "wert": wert,
            "gewinn": gewinn,
            "gewinn_pct": ((p - pos["avg_kaufpreis"]) / pos["avg_kaufpreis"] * 100) if pos["avg_kaufpreis"] > 0 else 0,
        })
    cash = k["cash"] if k else 0
    return jsonify({
        "cash": round(cash, 2),
        "depot_wert": round(depot_wert, 2),
        "gesamt": round(cash + depot_wert, 2),
        "eingezahlt": round(k["eingezahlt"] if k else 0, 2),
        "depot": depot_data,
    })

@app.route("/api/trades")
@login_required
def api_trades():
    uid = session["user_id"]
    with db() as c:
        trades = c.execute("SELECT * FROM trades WHERE user_id=? ORDER BY zeitpunkt DESC LIMIT 20", (uid,)).fetchall()
    return jsonify([dict(t) for t in trades])

# ═══════════════════════════════════════════
#   API - KAUFEN / VERKAUFEN
# ═══════════════════════════════════════════

@app.route("/api/kaufen", methods=["POST"])
@login_required
def api_kaufen():
    data = request.json
    uid = session["user_id"]
    coin = data.get("coin", "").upper()
    menge = float(data.get("menge", 0))

    if coin not in COINS:
        return jsonify({"error": "Unbekannter Coin"}), 400
    if menge <= 0:
        return jsonify({"error": "Ungueltige Menge"}), 400

    with db() as c:
        markt = c.execute("SELECT preis FROM markt WHERE coin=?", (coin,)).fetchone()
        if not markt:
            return jsonify({"error": "Coin nicht gefunden"}), 400

        spread = COINS[coin]["spread"]
        kaufpreis = markt["preis"] * (1 + spread)
        kosten = round(menge * kaufpreis, 2)

        k = c.execute("SELECT cash FROM konto WHERE user_id=?", (uid,)).fetchone()
        if not k or k["cash"] < kosten:
            return jsonify({"error": f"Nicht genug Cash. Benoetigt: ${kosten:,.2f}"}), 400

        # Cash abziehen
        c.execute("UPDATE konto SET cash=cash-? WHERE user_id=?", (kosten, uid))

        # Depot aktualisieren
        existing = c.execute("SELECT menge, avg_kaufpreis FROM depot WHERE user_id=? AND coin=?", (uid, coin)).fetchone()
        if existing:
            neue_menge = existing["menge"] + menge
            neuer_avg = (existing["avg_kaufpreis"] * existing["menge"] + kaufpreis * menge) / neue_menge
            c.execute("UPDATE depot SET menge=?, avg_kaufpreis=? WHERE user_id=? AND coin=?",
                      (neue_menge, neuer_avg, uid, coin))
        else:
            c.execute("INSERT INTO depot (user_id, coin, menge, avg_kaufpreis) VALUES (?,?,?,?)",
                      (uid, coin, menge, kaufpreis))

        # Trade speichern
        c.execute("INSERT INTO trades (user_id, coin, richtung, menge, preis) VALUES (?,?,?,?,?)",
                  (uid, coin, "kauf", menge, kaufpreis))

        # Preis bewegen
        p = markt["preis"]
        volumen = menge * kaufpreis
        impact = min((volumen / 50000) * 0.005, 0.03)
        neu = round(p * (1 + impact), 6)
        hist = json.loads(c.execute("SELECT history FROM markt WHERE coin=?", (coin,)).fetchone()["history"])
        hist.append(neu)
        if len(hist) > 60:
            hist = hist[-60:]
        c.execute("UPDATE markt SET preis=?, history=? WHERE coin=?", (neu, json.dumps(hist), coin))

    broadcast_markt()
    return jsonify({"ok": True, "kaufpreis": kaufpreis, "kosten": kosten})

@app.route("/api/verkaufen", methods=["POST"])
@login_required
def api_verkaufen():
    data = request.json
    uid = session["user_id"]
    coin = data.get("coin", "").upper()
    menge = float(data.get("menge", 0))

    if coin not in COINS:
        return jsonify({"error": "Unbekannter Coin"}), 400
    if menge <= 0:
        return jsonify({"error": "Ungueltige Menge"}), 400

    with db() as c:
        markt = c.execute("SELECT preis FROM markt WHERE coin=?", (coin,)).fetchone()
        depot = c.execute("SELECT menge FROM depot WHERE user_id=? AND coin=?", (uid, coin)).fetchone()

        if not depot or depot["menge"] < menge:
            return jsonify({"error": f"Nicht genug {coin} im Depot"}), 400

        spread = COINS[coin]["spread"]
        verkaufpreis = markt["preis"] * (1 - spread)
        erloes = round(menge * verkaufpreis, 2)

        # Cash gutschreiben
        c.execute("UPDATE konto SET cash=cash+? WHERE user_id=?", (erloes, uid))

        # Depot aktualisieren
        neue_menge = round(depot["menge"] - menge, 8)
        if neue_menge < 0.000001:
            c.execute("DELETE FROM depot WHERE user_id=? AND coin=?", (uid, coin))
        else:
            c.execute("UPDATE depot SET menge=? WHERE user_id=? AND coin=?", (neue_menge, uid, coin))

        # Trade speichern
        c.execute("INSERT INTO trades (user_id, coin, richtung, menge, preis) VALUES (?,?,?,?,?)",
                  (uid, coin, "verkauf", menge, verkaufpreis))

        # Preis bewegen
        p = markt["preis"]
        volumen = menge * verkaufpreis
        impact = min((volumen / 50000) * 0.005, 0.03)
        neu = round(p * (1 - impact), 6)
        hist = json.loads(c.execute("SELECT history FROM markt WHERE coin=?", (coin,)).fetchone()["history"])
        hist.append(neu)
        if len(hist) > 60:
            hist = hist[-60:]
        c.execute("UPDATE markt SET preis=?, history=? WHERE coin=?", (neu, json.dumps(hist), coin))

    broadcast_markt()
    return jsonify({"ok": True, "verkaufpreis": verkaufpreis, "erloes": erloes})

# ═══════════════════════════════════════════
#   API - ADMIN
# ═══════════════════════════════════════════

@app.route("/api/admin/users")
@admin_required
def admin_users():
    with db() as c:
        users = c.execute("""
            SELECT u.*, k.cash, k.eingezahlt
            FROM users u LEFT JOIN konto k ON u.id=k.user_id
            ORDER BY u.erstellt DESC
        """).fetchall()
    return jsonify([dict(u) for u in users])

@app.route("/api/admin/einzahlen", methods=["POST"])
@admin_required
def admin_einzahlen():
    data = request.json
    target_id = data.get("user_id")
    betrag = float(data.get("betrag", 0))
    if betrag <= 0:
        return jsonify({"error": "Ungültiger Betrag"}), 400
    with db() as c:
        c.execute("UPDATE konto SET cash=cash+?, eingezahlt=eingezahlt+? WHERE user_id=?", (betrag, betrag, target_id))
    socketio.emit("konto_update", {"user_id": target_id})
    return jsonify({"ok": True})

@app.route("/api/admin/auszahlen", methods=["POST"])
@admin_required
def admin_auszahlen():
    data = request.json
    target_id = data.get("user_id")
    betrag = float(data.get("betrag", 0))
    with db() as c:
        k = c.execute("SELECT cash FROM konto WHERE user_id=?", (target_id,)).fetchone()
        if not k or k["cash"] < betrag:
            return jsonify({"error": "Nicht genug Cash"}), 400
        c.execute("UPDATE konto SET cash=cash-?, ausgezahlt=ausgezahlt+? WHERE user_id=?", (betrag, betrag, target_id))
    return jsonify({"ok": True})

@app.route("/api/admin/mitarbeiter", methods=["POST"])
@admin_required
def admin_create_mitarbeiter():
    if session.get("rolle") not in ("admin", "superadmin"):
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "")
    rolle = data.get("rolle", "mitarbeiter")
    if not username or not password:
        return jsonify({"error": "Felder fehlen"}), 400
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    with db() as c:
        try:
            c.execute("INSERT INTO users (username, password_hash, rolle) VALUES (?,?,?)", (username, pw_hash, rolle))
            uid = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            c.execute("INSERT INTO konto (user_id) VALUES (?)", (uid,))
        except:
            return jsonify({"error": "Benutzername vergeben"}), 400
    return jsonify({"ok": True})

@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    with db() as c:
        users = c.execute("SELECT COUNT(*) as n FROM users WHERE rolle='user'").fetchone()["n"]
        cash = c.execute("SELECT SUM(cash) as s FROM konto").fetchone()["s"] or 0
        trades = c.execute("SELECT COUNT(*) as n FROM trades").fetchone()["n"]
    return jsonify({"users": users, "cash": round(cash, 2), "trades": trades})

# ═══════════════════════════════════════════
#   BRIDGE BOT API (fuer Discord Bot)
# ═══════════════════════════════════════════

@app.route("/api/bridge/einzahlen", methods=["POST"])
def bridge_einzahlen():
    data = request.json
    api_key = data.get("api_key", "")
    try:
        from config import BRIDGE_API_KEY
        if api_key != BRIDGE_API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
    except:
        pass

    discord_id = str(data.get("discord_id", ""))
    betrag = float(data.get("betrag", 0))
    message_id = str(data.get("message_id", ""))

    if betrag <= 0:
        return jsonify({"error": "Ungültiger Betrag"}), 400

    with db() as c:
        # Doppelte Verarbeitung verhindern
        if message_id and c.execute("SELECT 1 FROM processed_messages WHERE message_id=?", (message_id,)).fetchone():
            return jsonify({"error": "Bereits verarbeitet"}), 400

        user = c.execute("SELECT id, username FROM users WHERE discord_id=?", (discord_id,)).fetchone()
        if not user:
            return jsonify({"error": "Spieler nicht registriert auf cryptex-gta.duckdns.org"}), 404

        c.execute("UPDATE konto SET cash=cash+?, eingezahlt=eingezahlt+? WHERE user_id=?", (betrag, betrag, user["id"]))
        if message_id:
            c.execute("INSERT OR IGNORE INTO processed_messages (message_id, discord_id, betrag) VALUES (?,?,?)",
                      (message_id, discord_id, betrag))

        k = c.execute("SELECT cash FROM konto WHERE user_id=?", (user["id"],)).fetchone()

    return jsonify({"ok": True, "username": user["username"], "cash": k["cash"]})

# ═══════════════════════════════════════════
#   SOCKETIO
# ═══════════════════════════════════════════

@socketio.on("connect")
def on_connect():
    if "user_id" not in session:
        return False
    broadcast_markt()

# ═══════════════════════════════════════════
#   START
# ═══════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=markt_tick, daemon=True)
    t.start()
    print("CRYPTEX startet...")
    print("URL: http://cryptex-gta.duckdns.org")
    print("Admin: admin / admin124")
    socketio.run(app, host="0.0.0.0", port=80, debug=False)
