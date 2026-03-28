"""
CRYPTEX — Vollständige Krypto-Börse für GTA RP
Starten: python3 app.py
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit
import sqlite3, hashlib, os, json, time, threading, requests, schedule
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*")

# ═══════════════════════════════════════════
#   KONFIGURATION
# ═══════════════════════════════════════════

from config import DISCORD_SECRET
DISCORD_CLIENT_ID     = "1485940671545086002"
DISCORD_CLIENT_SECRET = DISCORD_SECRET
DISCORD_REDIRECT      = "http://cryptex-gta.duckdns.org/callback"
DB_FILE               = "cryptex.db"

COINS = {
    "BTC": {"name": "Bitcoin",  "emoji": "₿", "aktiv": True,  "startpreis": 67000.0},
    "ETH": {"name": "Ethereum", "emoji": "Ξ", "aktiv": True,  "startpreis": 3500.0},
    "SOL": {"name": "Solana",   "emoji": "◎", "aktiv": True,  "startpreis": 170.0},
    "BNB": {"name": "BNB",      "emoji": "B", "aktiv": True,  "startpreis": 600.0},
    "ADA": {"name": "Cardano",  "emoji": "A", "aktiv": True,  "startpreis": 0.48},
    "XRP": {"name": "Ripple",   "emoji": "X", "aktiv": True,  "startpreis": 0.52},
}

STEUER_SATZ   = 0.0    # Keine Steuer
STANDARD_GAP  = 0.002  # 0.2% Broker-Gap (einstellbar im Admin)
OWNER_DISCORD_ID = "1181910886277976127"  # Eddie - bekommt wöchentlichen Spread-Gewinn

# ═══════════════════════════════════════════
#   DATENBANK
# ═══════════════════════════════════════════

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
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
        CREATE TABLE IF NOT EXISTS depot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            coin TEXT,
            menge REAL DEFAULT 0,
            avg_kaufpreis REAL DEFAULT 0,
            UNIQUE(user_id, coin)
        );
        CREATE TABLE IF NOT EXISTS konto (
            user_id INTEGER PRIMARY KEY,
            cash REAL DEFAULT 0,
            eingezahlt REAL DEFAULT 0,
            ausgezahlt REAL DEFAULT 0,
            steuern_gezahlt REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            coin TEXT,
            typ TEXT,
            richtung TEXT,
            menge REAL,
            menge_gefuellt REAL DEFAULT 0,
            limit_preis REAL,
            stop_preis REAL,
            stop_ausgeloest INTEGER DEFAULT 0,
            status TEXT DEFAULT 'offen',
            erstellt TEXT DEFAULT CURRENT_TIMESTAMP,
            ausgefuehrt TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kaeufer_id INTEGER,
            verkaeufer_id INTEGER,
            coin TEXT,
            menge REAL,
            preis REAL,
            steuer REAL DEFAULT 0,
            gewinn REAL DEFAULT 0,
            zeitpunkt TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS markt (
            coin TEXT PRIMARY KEY,
            bid REAL,
            ask REAL,
            letzter_preis REAL,
            gap REAL DEFAULT 0.002,
            aktiv INTEGER DEFAULT 1,
            history TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS einstellungen (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS transaktionen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            typ TEXT,
            betrag REAL,
            beschreibung TEXT,
            mitarbeiter_id INTEGER,
            zeitpunkt TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # Markt initialisieren
        for sym, info in COINS.items():
            c.execute("""
                INSERT OR IGNORE INTO markt (coin, bid, ask, letzter_preis, gap, aktiv, history)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sym,
                  info["startpreis"] * (1 - STANDARD_GAP),
                  info["startpreis"] * (1 + STANDARD_GAP),
                  info["startpreis"],
                  STANDARD_GAP,
                  1 if info["aktiv"] else 0,
                  json.dumps([info["startpreis"]] * 20)))
        # Super-Admin erstellen falls nicht vorhanden
        pw = hashlib.sha256("admin123".encode()).hexdigest()
        c.execute("""
            INSERT OR IGNORE INTO users (username, password_hash, rolle)
            VALUES ('admin', ?, 'superadmin')
        """, (pw,))
        c.execute("INSERT OR IGNORE INTO konto (user_id, cash) SELECT id, 0 FROM users WHERE username='admin'")

# ═══════════════════════════════════════════
#   HILFSFUNKTIONEN
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
            user = c.execute("SELECT rolle FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not user or user["rolle"] not in ("admin", "superadmin", "mitarbeiter"):
            return jsonify({"error": "Kein Zugriff"}), 403
        return f(*args, **kwargs)
    return decorated

def get_markt():
    with db() as c:
        rows = c.execute("SELECT * FROM markt").fetchall()
    return {r["coin"]: dict(r) for r in rows}

def broadcast_markt():
    markt = get_markt()
    data = {}
    for sym, m in markt.items():
        data[sym] = {
            "bid": round(m["bid"], 6),
            "ask": round(m["ask"], 6),
            "letzter": round(m["letzter_preis"], 6),
            "aktiv": m["aktiv"],
            "history": json.loads(m["history"] or "[]")[-30:],
        }
    socketio.emit("markt_update", data)

def update_preis_nach_trade(coin, preis, menge, richtung):
    """Preis bewegt sich durch echte Trades — Angebot & Nachfrage"""
    with db() as c:
        m = c.execute("SELECT * FROM markt WHERE coin=?", (coin,)).fetchone()
        if not m:
            return
        gap = m["gap"]
        startpreis = COINS[coin]["startpreis"]
        aktuell = m["letzter_preis"]

        # Impact basierend auf Volumen
        volumen = menge * preis
        impact = min((volumen / 50000) * 0.01, 0.03)

        if richtung == "kauf":
            neu = aktuell * (1 + impact)
        else:
            neu = aktuell * (1 - impact)

        # Mean Reversion — max ±40% vom Startpreis
        neu = max(startpreis * 0.60, min(startpreis * 1.60, neu))

        bid = round(neu * (1 - gap), 6)
        ask = round(neu * (1 + gap), 6)

        history = json.loads(m["history"] or "[]")
        history.append(round(neu, 6))
        if len(history) > 60:
            history = history[-60:]

        c.execute("""
            UPDATE markt SET bid=?, ask=?, letzter_preis=?, history=? WHERE coin=?
        """, (bid, ask, round(neu, 6), json.dumps(history), coin))

    broadcast_markt()


def check_stop_orders():
    """Prüft alle 10 Sekunden ob Stop-Orders ausgelöst werden sollen"""
    while True:
        time.sleep(10)
        try:
            with db() as c:
                stop_orders = c.execute("""
                    SELECT * FROM orders
                    WHERE typ IN ('stop','stoplimit') AND status='offen' AND stop_ausgeloest=0
                """).fetchall()

                for o in stop_orders:
                    m = c.execute("SELECT * FROM markt WHERE coin=?", (o["coin"],)).fetchone()
                    if not m:
                        continue
                    preis = m["letzter_preis"]
                    ausloesen = False

                    if o["richtung"] == "kauf" and preis >= o["stop_preis"]:
                        ausloesen = True
                    elif o["richtung"] == "verkauf" and preis <= o["stop_preis"]:
                        ausloesen = True

                    if ausloesen:
                        if o["typ"] == "stop":
                            # Wird zur Market Order
                            c.execute("UPDATE orders SET typ='market', stop_ausgeloest=1 WHERE id=?", (o["id"],))
                        elif o["typ"] == "stoplimit":
                            # Wird zur Limit Order
                            c.execute("UPDATE orders SET typ='limit', stop_ausgeloest=1 WHERE id=?", (o["id"],))

                        threading.Thread(target=match_orders, args=(o["coin"],), daemon=True).start()
        except Exception as e:
            pass


def send_dm(discord_id, nachricht):
    """Sendet eine DM via Discord Bot"""
    try:
        from config import BOT_TOKEN
        dm = requests.post(
            "https://discord.com/api/v10/users/@me/channels",
            headers={"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"},
            json={"recipient_id": str(discord_id)}
        )
        if dm.status_code == 200:
            channel_id = dm.json()["id"]
            requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"},
                json={"content": nachricht}
            )
            return True
    except Exception as e:
        print(f"DM Fehler: {e}")
    return False

def berechne_spread_gewinn_woche():
    """Berechnet Spread-Gewinn der letzten 7 Tage"""
    with db() as c:
        trades = c.execute("""
            SELECT t.coin, t.menge, t.preis, m.gap
            FROM trades t
            JOIN markt m ON t.coin = m.coin
            WHERE t.zeitpunkt > datetime('now', '-7 days')
        """).fetchall()
        total = sum(t["menge"] * t["preis"] * t["gap"] * 2 for t in trades)
    return round(total, 2)

def weekly_spread_payout():
    """Jeden Montag 09:00 Spread-Gewinn auszahlen"""
    def payout():
        gewinn = berechne_spread_gewinn_woche()
        datum = datetime.now().strftime("%d.%m.%Y")
        print(f"[SPREAD] Wöchentliche Auszahlung: ${gewinn:,.2f}")

        with db() as c:
            # Owner-Konto finden oder erstellen
            user = c.execute("SELECT id FROM users WHERE discord_id=?", (OWNER_DISCORD_ID,)).fetchone()
            if not user:
                print("[SPREAD] Owner nicht gefunden!")
                return
            uid = user["id"]

            if gewinn > 0:
                c.execute("UPDATE konto SET cash=cash+? WHERE user_id=?", (gewinn, uid))
                c.execute("""
                    INSERT INTO transaktionen (user_id, typ, betrag, beschreibung)
                    VALUES (?, 'spread_gewinn', ?, 'Wöchentlicher Spread-Gewinn')
                """, (uid, gewinn))

        # DM Beleg senden
        beleg = (
            f"🧾 **CRYPTEX — Wöchentlicher Spread-Gewinn**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Auszahlungsdatum: **{datum}**\n"
            f"💰 Betrag: **${gewinn:,.2f}**\n"
            f"📋 Grund: Broker Spread-Gewinn (7 Tage)\n"
            f"🏦 Gutgeschrieben auf: Dein Cryptex-Konto\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Automatisch erstellt von CRYPTEX_"
        )
        send_dm(OWNER_DISCORD_ID, beleg)

    schedule.every().monday.at("09:00").do(payout)
    while True:
        schedule.run_pending()
        time.sleep(60)

def markt_recovery():
    """Alle 60 Sekunden langsame Erholung zum Startpreis"""
    while True:
        time.sleep(60)
        with db() as c:
            rows = c.execute("SELECT * FROM markt").fetchall()
            for m in rows:
                startpreis = COINS.get(m["coin"], {}).get("startpreis", m["letzter_preis"])
                aktuell = m["letzter_preis"]
                abw = (aktuell - startpreis) / startpreis
                neu = aktuell * (1 - abw * 0.01)
                gap = m["gap"]
                bid = round(neu * (1 - gap), 6)
                ask = round(neu * (1 + gap), 6)
                history = json.loads(m["history"] or "[]")
                history.append(round(neu, 6))
                if len(history) > 60:
                    history = history[-60:]
                c.execute("UPDATE markt SET bid=?, ask=?, letzter_preis=?, history=? WHERE coin=?",
                          (bid, ask, round(neu, 6), json.dumps(history), m["coin"]))
        broadcast_markt()

def match_orders(coin):
    """Versucht offene Orders zu matchen"""
    with db() as c:
        m = c.execute("SELECT * FROM markt WHERE coin=?", (coin,)).fetchone()
        if not m:
            return
        bid = m["bid"]
        ask = m["ask"]
        gap = m["gap"]

        # Kauforders (höchster Preis zuerst)
        kauf_orders = c.execute("""
            SELECT o.*, u.id as uid FROM orders o
            JOIN users u ON o.user_id = u.id
            WHERE o.coin=? AND o.richtung='kauf' AND o.status='offen'
            AND (o.typ='market' OR o.limit_preis >= ?)
            ORDER BY o.limit_preis DESC, o.erstellt ASC
        """, (coin, ask)).fetchall()

        # Verkaufsorders (niedrigster Preis zuerst)
        verk_orders = c.execute("""
            SELECT o.*, u.id as uid FROM orders o
            JOIN users u ON o.user_id = u.id
            WHERE o.coin=? AND o.richtung='verkauf' AND o.status='offen'
            AND (o.typ='market' OR o.limit_preis <= ?)
            ORDER BY o.limit_preis ASC, o.erstellt ASC
        """, (coin, bid)).fetchall()

        for ko in kauf_orders:
            for vo in verk_orders:
                if ko["user_id"] == vo["user_id"]:
                    continue

                # Preis bestimmen
                if ko["typ"] == "market" and vo["typ"] == "market":
                    handelspreis = m["letzter_preis"]
                elif vo["typ"] == "limit":
                    handelspreis = vo["limit_preis"]
                else:
                    handelspreis = ko["limit_preis"]

                # GAP prüfen
                if ko["typ"] == "limit" and vo["typ"] == "limit":
                    if ko["limit_preis"] < vo["limit_preis"] * (1 - gap):
                        continue

                # Handelsmenge
                ko_rest = ko["menge"] - ko["menge_gefuellt"]
                vo_rest = vo["menge"] - vo["menge_gefuellt"]
                handelsmenge = min(ko_rest, vo_rest)

                if handelsmenge <= 0:
                    continue

                kosten = handelsmenge * handelspreis

                # Käufer-Konto prüfen
                kaeufer_konto = c.execute("SELECT cash FROM konto WHERE user_id=?", (ko["user_id"],)).fetchone()
                if not kaeufer_konto or kaeufer_konto["cash"] < kosten:
                    continue

                # Verkäufer-Depot prüfen
                verk_depot = c.execute("SELECT menge, avg_kaufpreis FROM depot WHERE user_id=? AND coin=?",
                                       (vo["user_id"], coin)).fetchone()
                if not verk_depot or verk_depot["menge"] < handelsmenge:
                    continue

                # Trade ausführen
                # Käufer: Cash abziehen, Depot gutschreiben
                c.execute("UPDATE konto SET cash=cash-? WHERE user_id=?", (kosten, ko["user_id"]))
                c.execute("""
                    INSERT INTO depot (user_id, coin, menge, avg_kaufpreis)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, coin) DO UPDATE SET
                    avg_kaufpreis = (avg_kaufpreis * menge + ? * ?) / (menge + ?),
                    menge = menge + ?
                """, (ko["user_id"], coin, handelsmenge, handelspreis,
                      handelspreis, handelsmenge, handelsmenge, handelsmenge))

                # Verkäufer: Depot abziehen, Cash gutschreiben
                avg_kp = verk_depot["avg_kaufpreis"]
                gewinn = (handelspreis - avg_kp) * handelsmenge
                steuer = 0

                c.execute("UPDATE depot SET menge=menge-? WHERE user_id=? AND coin=?",
                          (handelsmenge, vo["user_id"], coin))
                c.execute("UPDATE konto SET cash=cash+? WHERE user_id=?",
                          (kosten, vo["user_id"]))

                # Orders aktualisieren
                neue_ko_gefuellt = ko["menge_gefuellt"] + handelsmenge
                neue_vo_gefuellt = vo["menge_gefuellt"] + handelsmenge
                ko_status = "ausgefuehrt" if neue_ko_gefuellt >= ko["menge"] else "teilgefuellt"
                vo_status = "ausgefuehrt" if neue_vo_gefuellt >= vo["menge"] else "teilgefuellt"

                c.execute("UPDATE orders SET menge_gefuellt=?, status=?, ausgefuehrt=CURRENT_TIMESTAMP WHERE id=?",
                          (neue_ko_gefuellt, ko_status, ko["id"]))
                c.execute("UPDATE orders SET menge_gefuellt=?, status=?, ausgefuehrt=CURRENT_TIMESTAMP WHERE id=?",
                          (neue_vo_gefuellt, vo_status, vo["id"]))

                # Trade speichern
                c.execute("""
                    INSERT INTO trades (kaeufer_id, verkaeufer_id, coin, menge, preis, steuer, gewinn)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (ko["user_id"], vo["user_id"], coin, handelsmenge, handelspreis, steuer, gewinn))

                update_preis_nach_trade(coin, handelspreis, handelsmenge, "kauf")

                # Benachrichtigung senden
                socketio.emit("order_update", {"user_id": ko["user_id"], "coin": coin, "status": ko_status})
                socketio.emit("order_update", {"user_id": vo["user_id"], "coin": coin, "status": vo_status})

# ═══════════════════════════════════════════
#   ROUTEN — SEITEN
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
    password = data.get("password", "")
    pw_hash = hashlib.sha256(password.encode()).hexdigest()

    with db() as c:
        user = c.execute("SELECT * FROM users WHERE username=? AND password_hash=? AND aktiv=1",
                         (username, pw_hash)).fetchone()
    if not user:
        return jsonify({"error": "Falscher Benutzername oder Passwort"}), 401

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["rolle"] = user["rolle"]
    return jsonify({"ok": True, "rolle": user["rolle"]})


@app.route("/register", methods=["POST"])
def register():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or len(username) < 3:
        return jsonify({"error": "Benutzername muss mindestens 3 Zeichen haben"}), 400
    if not password or len(password) < 4:
        return jsonify({"error": "Passwort muss mindestens 4 Zeichen haben"}), 400

    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    with db() as c:
        try:
            c.execute("INSERT INTO users (username, password_hash, rolle) VALUES (?, ?, 'user')",
                      (username, pw_hash))
            uid = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            c.execute("INSERT INTO konto (user_id, cash) VALUES (?, 0)", (uid,))
        except sqlite3.IntegrityError:
            return jsonify({"error": "Benutzername bereits vergeben"}), 400
    return jsonify({"ok": True})

@app.route("/discord/login")
def discord_login():
    return redirect(
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT}"
        f"&response_type=code"
        f"&scope=identify"
    )

@app.route("/callback")
def discord_callback():
    code = request.args.get("code")
    if not code:
        return redirect(url_for("login_page"))

    # Token holen
    r = requests.post("https://discord.com/api/oauth2/token", data={
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})

    if r.status_code != 200:
        return redirect(url_for("login_page"))

    token = r.json()["access_token"]
    user_r = requests.get("https://discord.com/api/users/@me",
                          headers={"Authorization": f"Bearer {token}"})
    if user_r.status_code != 200:
        return redirect(url_for("login_page"))

    duser = user_r.json()
    discord_id = duser["id"]
    username = duser["username"]

    with db() as c:
        user = c.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,)).fetchone()
        if not user:
            c.execute("INSERT OR IGNORE INTO users (discord_id, username, rolle) VALUES (?, ?, 'user')",
                      (discord_id, username))
            user_id = c.execute("SELECT id FROM users WHERE discord_id=?", (discord_id,)).fetchone()["id"]
            c.execute("INSERT OR IGNORE INTO konto (user_id, cash) VALUES (?, 0)", (user_id,))
        else:
            user_id = user["id"]

        u = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    session["user_id"] = user_id
    session["username"] = u["username"]
    session["rolle"] = u["rolle"]
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html",
                           username=session["username"],
                           rolle=session["rolle"])

@app.route("/admin")
@login_required
def admin_page():
    if session.get("rolle") not in ("admin", "superadmin", "mitarbeiter"):
        return redirect(url_for("dashboard"))
    return render_template("admin.html",
                           username=session["username"],
                           rolle=session["rolle"])

# ═══════════════════════════════════════════
#   API — MARKT
# ═══════════════════════════════════════════

@app.route("/api/markt")
@login_required
def api_markt():
    markt = get_markt()
    result = {}
    for sym, m in markt.items():
        result[sym] = {
            "bid": round(m["bid"], 6),
            "ask": round(m["ask"], 6),
            "letzter": round(m["letzter_preis"], 6),
            "gap": m["gap"],
            "aktiv": bool(m["aktiv"]),
            "name": COINS[sym]["name"],
            "emoji": COINS[sym]["emoji"],
            "history": json.loads(m["history"] or "[]")[-30:],
        }
    return jsonify(result)

# ═══════════════════════════════════════════
#   API — KONTO & DEPOT
# ═══════════════════════════════════════════

@app.route("/api/konto")
@login_required
def api_konto():
    uid = session["user_id"]
    with db() as c:
        konto = c.execute("SELECT * FROM konto WHERE user_id=?", (uid,)).fetchone()
        depot = c.execute("SELECT * FROM depot WHERE user_id=? AND menge > 0", (uid,)).fetchall()
        markt = c.execute("SELECT * FROM markt").fetchall()

    markt_map = {m["coin"]: m for m in markt}
    depot_data = []
    depot_wert = 0
    for pos in depot:
        sym = pos["coin"]
        mp = markt_map.get(sym)
        if mp:
            aktuell = mp["letzter_preis"]
            wert = pos["menge"] * aktuell
            gewinn = (aktuell - pos["avg_kaufpreis"]) * pos["menge"]
            depot_wert += wert
            depot_data.append({
                "coin": sym,
                "menge": pos["menge"],
                "avg_kp": pos["avg_kaufpreis"],
                "aktuell": aktuell,
                "wert": wert,
                "gewinn": gewinn,
                "gewinn_pct": ((aktuell - pos["avg_kaufpreis"]) / pos["avg_kaufpreis"] * 100) if pos["avg_kaufpreis"] > 0 else 0,
            })

    return jsonify({
        "cash": round(konto["cash"], 2) if konto else 0,
        "eingezahlt": round(konto["eingezahlt"], 2) if konto else 0,
        "steuern": round(konto["steuern_gezahlt"], 2) if konto else 0,
        "depot": depot_data,
        "depot_wert": round(depot_wert, 2),
        "gesamt": round((konto["cash"] if konto else 0) + depot_wert, 2),
    })

@app.route("/api/orders")
@login_required
def api_orders():
    uid = session["user_id"]
    with db() as c:
        orders = c.execute("""
            SELECT * FROM orders WHERE user_id=? AND status IN ('offen','teilgefuellt')
            ORDER BY erstellt DESC
        """, (uid,)).fetchall()
        history = c.execute("""
            SELECT * FROM orders WHERE user_id=? AND status='ausgefuehrt'
            ORDER BY ausgefuehrt DESC LIMIT 20
        """, (uid,)).fetchall()
    return jsonify({
        "offen": [dict(o) for o in orders],
        "history": [dict(o) for o in history],
    })

@app.route("/api/trades")
@login_required
def api_trades():
    uid = session["user_id"]
    with db() as c:
        trades = c.execute("""
            SELECT * FROM trades WHERE kaeufer_id=? OR verkaeufer_id=?
            ORDER BY zeitpunkt DESC LIMIT 30
        """, (uid, uid)).fetchall()
    return jsonify([dict(t) for t in trades])

# ═══════════════════════════════════════════
#   API — ORDERS PLATZIEREN
# ═══════════════════════════════════════════

@app.route("/api/order", methods=["POST"])
@login_required
def place_order():
    data = request.json
    uid = session["user_id"]
    coin = data.get("coin", "").upper()
    typ = data.get("typ", "market")         # market / limit
    richtung = data.get("richtung", "")     # kauf / verkauf
    menge = float(data.get("menge", 0))
    limit_preis = float(data.get("limit_preis", 0)) if typ == "limit" else None

    stop_preis = float(data.get("stop_preis", 0)) if data.get("stop_preis") else None

    if coin not in COINS:
        return jsonify({"error": "Unbekannter Coin"}), 400
    if richtung not in ("kauf", "verkauf"):
        return jsonify({"error": "Ungültige Richtung"}), 400
    if typ not in ("market", "limit", "stop", "stoplimit"):
        return jsonify({"error": "Ungültiger Order-Typ"}), 400
    if menge <= 0:
        return jsonify({"error": "Ungültige Menge"}), 400

    with db() as c:
        m = c.execute("SELECT * FROM markt WHERE coin=?", (coin,)).fetchone()
        if not m or not m["aktiv"]:
            return jsonify({"error": "Coin nicht handelbar"}), 400

        konto_row = c.execute("SELECT cash FROM konto WHERE user_id=?", (uid,)).fetchone()
        if not konto_row:
            return jsonify({"error": "Kein Konto gefunden"}), 400

        if richtung == "kauf":
            preis_check = limit_preis if typ == "limit" else m["ask"]
            kosten = menge * preis_check
            if konto_row["cash"] < kosten:
                return jsonify({"error": f"Nicht genug Cash. Benötigt: ${kosten:,.2f}"}), 400
            # Cash reservieren
            c.execute("UPDATE konto SET cash=cash-? WHERE user_id=?", (kosten, uid))

        elif richtung == "verkauf":
            depot_row = c.execute("SELECT menge FROM depot WHERE user_id=? AND coin=?", (uid, coin)).fetchone()
            if not depot_row or depot_row["menge"] < menge:
                return jsonify({"error": f"Nicht genug {coin} im Depot"}), 400

        order_id = c.execute("""
            INSERT INTO orders (user_id, coin, typ, richtung, menge, limit_preis, stop_preis, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'offen')
        """, (uid, coin, typ, richtung, menge, limit_preis, stop_preis)).lastrowid

    # Market Orders sofort ausführen
    if typ == "market":
        with db() as c:
            m = c.execute("SELECT * FROM markt WHERE coin=?", (coin,)).fetchone()
            if m:
                if richtung == "kauf":
                    preis = m["ask"]
                    kosten = menge * preis
                    # Prüfen ob genug Cash
                    konto_row = c.execute("SELECT cash FROM konto WHERE user_id=?", (uid,)).fetchone()
                    if konto_row and konto_row["cash"] >= kosten:
                        c.execute("UPDATE konto SET cash=cash-? WHERE user_id=?", (kosten, uid))
                        c.execute("""
                            INSERT INTO depot (user_id, coin, menge, avg_kaufpreis)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(user_id, coin) DO UPDATE SET
                            avg_kaufpreis = (avg_kaufpreis * menge + ? * ?) / (menge + ?),
                            menge = menge + ?
                        """, (uid, coin, menge, preis, preis, menge, menge, menge))
                        c.execute("UPDATE orders SET status='ausgefuehrt', ausgefuehrt=CURRENT_TIMESTAMP WHERE id=?", (order_id,))
                        c.execute("INSERT INTO trades (kaeufer_id, verkaeufer_id, coin, menge, preis) VALUES (?,?,?,?,?)",
                                  (uid, 0, coin, menge, preis))
                        update_preis_nach_trade(coin, preis, menge, "kauf")
                elif richtung == "verkauf":
                    preis = m["bid"]
                    erloes = menge * preis
                    depot_row = c.execute("SELECT menge FROM depot WHERE user_id=? AND coin=?", (uid, coin)).fetchone()
                    if depot_row and depot_row["menge"] >= menge:
                        c.execute("UPDATE depot SET menge=menge-? WHERE user_id=? AND coin=?", (menge, uid, coin))
                        c.execute("UPDATE konto SET cash=cash+? WHERE user_id=?", (erloes, uid))
                        c.execute("UPDATE orders SET status='ausgefuehrt', ausgefuehrt=CURRENT_TIMESTAMP WHERE id=?", (order_id,))
                        c.execute("INSERT INTO trades (kaeufer_id, verkaeufer_id, coin, menge, preis) VALUES (?,?,?,?,?)",
                                  (0, uid, coin, menge, preis))
                        update_preis_nach_trade(coin, preis, menge, "verkauf")
    else:
        # Limit/Stop Orders matchen
        threading.Thread(target=match_orders, args=(coin,), daemon=True).start()

    return jsonify({"ok": True, "order_id": order_id})

@app.route("/api/order/<int:order_id>/cancel", methods=["POST"])
@login_required
def cancel_order(order_id):
    uid = session["user_id"]
    with db() as c:
        order = c.execute("SELECT * FROM orders WHERE id=? AND user_id=? AND status='offen'",
                          (order_id, uid)).fetchone()
        if not order:
            return jsonify({"error": "Order nicht gefunden"}), 404

        # Bei Kauforder: Cash zurückgeben
        if order["richtung"] == "kauf":
            rest_menge = order["menge"] - order["menge_gefuellt"]
            preis = order["limit_preis"] or 0
            rueckgabe = rest_menge * preis
            c.execute("UPDATE konto SET cash=cash+? WHERE user_id=?", (rueckgabe, uid))

        c.execute("UPDATE orders SET status='storniert' WHERE id=?", (order_id,))
    return jsonify({"ok": True})

# ═══════════════════════════════════════════
#   API — ADMIN
# ═══════════════════════════════════════════

@app.route("/api/admin/users")
@admin_required
def admin_users():
    with db() as c:
        users = c.execute("""
            SELECT u.*, k.cash, k.eingezahlt, k.steuern_gezahlt
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
        user = c.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
        if not user:
            return jsonify({"error": "Benutzer nicht gefunden"}), 404
        c.execute("UPDATE konto SET cash=cash+?, eingezahlt=eingezahlt+? WHERE user_id=?",
                  (betrag, betrag, target_id))
        c.execute("""
            INSERT INTO transaktionen (user_id, typ, betrag, beschreibung, mitarbeiter_id)
            VALUES (?, 'einzahlung', ?, ?, ?)
        """, (target_id, betrag, f"Einzahlung durch Mitarbeiter", session["user_id"]))

    socketio.emit("konto_update", {"user_id": target_id})
    return jsonify({"ok": True})

@app.route("/api/admin/auszahlen", methods=["POST"])
@admin_required
def admin_auszahlen():
    data = request.json
    target_id = data.get("user_id")
    betrag = float(data.get("betrag", 0))

    with db() as c:
        konto_row = c.execute("SELECT cash FROM konto WHERE user_id=?", (target_id,)).fetchone()
        if not konto_row or konto_row["cash"] < betrag:
            return jsonify({"error": "Nicht genug Cash"}), 400
        c.execute("UPDATE konto SET cash=cash-?, ausgezahlt=ausgezahlt+? WHERE user_id=?",
                  (betrag, betrag, target_id))
        c.execute("""
            INSERT INTO transaktionen (user_id, typ, betrag, beschreibung, mitarbeiter_id)
            VALUES (?, 'auszahlung', ?, ?, ?)
        """, (target_id, betrag, "Auszahlung durch Mitarbeiter", session["user_id"]))

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
        return jsonify({"error": "Benutzername und Passwort erforderlich"}), 400
    if rolle not in ("mitarbeiter", "admin"):
        rolle = "mitarbeiter"

    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    with db() as c:
        try:
            c.execute("INSERT INTO users (username, password_hash, rolle) VALUES (?, ?, ?)",
                      (username, pw_hash, rolle))
            uid = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            c.execute("INSERT INTO konto (user_id, cash) VALUES (?, 0)", (uid,))
        except sqlite3.IntegrityError:
            return jsonify({"error": "Benutzername bereits vergeben"}), 400
    return jsonify({"ok": True})

@app.route("/api/admin/mitarbeiter/<int:uid>/deaktivieren", methods=["POST"])
@admin_required
def admin_deaktivieren(uid):
    if session.get("rolle") not in ("admin", "superadmin"):
        return jsonify({"error": "Kein Zugriff"}), 403
    with db() as c:
        c.execute("UPDATE users SET aktiv=0 WHERE id=?", (uid,))
    return jsonify({"ok": True})

@app.route("/api/admin/coin/<coin>/toggle", methods=["POST"])
@admin_required
def admin_coin_toggle(coin):
    with db() as c:
        m = c.execute("SELECT aktiv FROM markt WHERE coin=?", (coin,)).fetchone()
        if not m:
            return jsonify({"error": "Coin nicht gefunden"}), 404
        neu = 0 if m["aktiv"] else 1
        c.execute("UPDATE markt SET aktiv=? WHERE coin=?", (neu, coin))
    broadcast_markt()
    return jsonify({"ok": True, "aktiv": bool(neu)})

@app.route("/api/admin/coin/<coin>/gap", methods=["POST"])
@admin_required
def admin_coin_gap(coin):
    data = request.json
    gap = float(data.get("gap", STANDARD_GAP))
    gap = max(0.0001, min(0.05, gap))  # 0.01% bis 5%
    with db() as c:
        m = c.execute("SELECT letzter_preis FROM markt WHERE coin=?", (coin,)).fetchone()
        if not m:
            return jsonify({"error": "Coin nicht gefunden"}), 404
        p = m["letzter_preis"]
        c.execute("UPDATE markt SET gap=?, bid=?, ask=? WHERE coin=?",
                  (gap, round(p * (1 - gap), 6), round(p * (1 + gap), 6), coin))
    broadcast_markt()
    # DM Beleg an Spieler
    try:
        with db() as c:
            ziel = c.execute("SELECT discord_id, username FROM users WHERE id=?", (target_id,)).fetchone()
            admin = c.execute("SELECT username FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if ziel and ziel["discord_id"]:
            datum = datetime.now().strftime("%d.%m.%Y %H:%M")
            beleg = (
                f"🧾 **CRYPTEX — Auszahlungsbeleg**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📅 Datum: **{datum}**\n"
                f"👤 Spieler: **{ziel['username']}**\n"
                f"💸 Ausgezahlt: **${betrag:,.0f}**\n"
                f"👮 Bearbeitet von: {admin['username'] if admin else 'Admin'}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"_Dieser Beleg wurde automatisch erstellt._"
            )
            send_dm(ziel["discord_id"], beleg)
    except Exception as e:
        print(f"Beleg DM Fehler: {e}")

    return jsonify({"ok": True})

@app.route("/api/admin/passwort", methods=["POST"])
@admin_required
def admin_passwort():
    if session.get("rolle") not in ("admin", "superadmin"):
        return jsonify({"error": "Kein Zugriff"}), 403
    data = request.json
    uid = data.get("user_id")
    pw = data.get("password", "")
    if not pw:
        return jsonify({"error": "Passwort erforderlich"}), 400
    pw_hash = hashlib.sha256(pw.encode()).hexdigest()
    with db() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, uid))
    return jsonify({"ok": True})

@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    with db() as c:
        user_count = c.execute("SELECT COUNT(*) as n FROM users WHERE rolle='user'").fetchone()["n"]
        total_cash = c.execute("SELECT SUM(cash) as s FROM konto").fetchone()["s"] or 0
        total_eingezahlt = c.execute("SELECT SUM(eingezahlt) as s FROM konto").fetchone()["s"] or 0
        total_steuern = c.execute("SELECT SUM(steuern_gezahlt) as s FROM konto").fetchone()["s"] or 0
        trade_count = c.execute("SELECT COUNT(*) as n FROM trades").fetchone()["n"]
    return jsonify({
        "user_count": user_count,
        "total_cash": round(total_cash, 2),
        "total_eingezahlt": round(total_eingezahlt, 2),
        "total_steuern": round(total_steuern, 2),
        "trade_count": trade_count,
    })

# ═══════════════════════════════════════════
#   DISCORD BOT API (für den Discord Bot)
# ═══════════════════════════════════════════

@app.route("/api/bot/konto/<discord_id>")
def bot_konto(discord_id):
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,)).fetchone()
        if not user:
            return jsonify({"error": "Nicht gefunden"}), 404
        konto_row = c.execute("SELECT * FROM konto WHERE user_id=?", (user["id"],)).fetchone()
    return jsonify({"cash": konto_row["cash"] if konto_row else 0, "username": user["username"]})

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
    # Markt-Recovery Thread
    t = threading.Thread(target=markt_recovery, daemon=True)
    t.start()
    # Stop-Order Checker
    t2 = threading.Thread(target=check_stop_orders, daemon=True)
    t2.start()
    # Wöchentlicher Spread-Gewinn
    t3 = threading.Thread(target=weekly_spread_payout, daemon=True)
    t3.start()
    print("✅  CRYPTEX Webserver startet...")
    print("    URL: http://cryptex-gta.duckdns.org")
    print("    Admin Login: admin / admin123")
    print("    ⚠️  Bitte Admin-Passwort sofort ändern!")
    socketio.run(app, host="0.0.0.0", port=80, debug=False)
