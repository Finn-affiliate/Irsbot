"""
╔══════════════════════════════════════════════╗
║   CRYPTEX BANK BRIDGE BOT — SECURE EDITION  ║
║   Verknüpft Staatsbot mit Cryptex Webseite  ║
╚══════════════════════════════════════════════╝

INSTALLATION:
  pip install discord.py

STARTEN:
  screen -S bridge
  python3 bank_bridge_bot.py
  [Strg+A dann D]
"""

import discord
from discord.ext import commands
import sqlite3
import re
import os
import time
import logging
from datetime import datetime, timedelta
from collections import defaultdict

# ═══════════════════════════════════════════
#   LOGGING — Alle Aktionen werden geloggt
# ═══════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bridge_audit.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("BRIDGE")

# ═══════════════════════════════════════════
#   KONFIGURATION
# ═══════════════════════════════════════════

from config import BRIDGE_TOKEN  # Token aus config.py
BANK_CHANNEL_ID    = 1486833105884151919           # Channel wo Staatsbot schreibt
COMMAND_CHANNEL_ID = 1486841457485680654           # Channel wo Spieler Commands nutzen
STAATSBOT_ID    = 0                             # 0 = alle Bots/User akzeptieren (TEST MODUS)
DB_PFAD         = "/root/cryptex/cryptex.db"   # Cryptex Datenbank

# Sicherheitslimits
MAX_BETRAG_PRO_TRANSAKTION = 10_000_000   # Max $10 Mio pro Einzahlung
MIN_BETRAG                 = 1            # Mindestbetrag $1
MAX_TRANSAKTIONEN_PRO_MIN  = 10           # Max 10 Transaktionen pro Minute
MAX_TRANSAKTIONEN_PRO_TAG  = 500          # Max 500 Transaktionen pro Tag

# ═══════════════════════════════════════════
#   RATE LIMITER
# ═══════════════════════════════════════════

class RateLimiter:
    def __init__(self):
        self.minute_counts = defaultdict(list)  # {key: [timestamps]}
        self.day_counts = defaultdict(list)

    def check(self, key):
        now = time.time()
        # Alte Einträge löschen
        self.minute_counts[key] = [t for t in self.minute_counts[key] if now - t < 60]
        self.day_counts[key] = [t for t in self.day_counts[key] if now - t < 86400]

        if len(self.minute_counts[key]) >= MAX_TRANSAKTIONEN_PRO_MIN:
            return False, "Rate-Limit: Zu viele Transaktionen pro Minute"
        if len(self.day_counts[key]) >= MAX_TRANSAKTIONEN_PRO_TAG:
            return False, "Rate-Limit: Tages-Limit erreicht"

        self.minute_counts[key].append(now)
        self.day_counts[key].append(now)
        return True, None

rate_limiter = RateLimiter()

# ═══════════════════════════════════════════
#   DATENBANK
# ═══════════════════════════════════════════

def db():
    conn = sqlite3.connect(DB_PFAD)
    conn.row_factory = sqlite3.Row
    return conn

def init_security_tables():
    """Sicherheits-Tabellen erstellen"""
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            verarbeitet_am TEXT DEFAULT CURRENT_TIMESTAMP,
            aktion TEXT,
            discord_id TEXT,
            betrag REAL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zeitpunkt TEXT DEFAULT CURRENT_TIMESTAMP,
            aktion TEXT,
            discord_id TEXT,
            betrag REAL,
            status TEXT,
            fehler TEXT,
            message_id TEXT,
            bot_id TEXT
        );
        CREATE TABLE IF NOT EXISTS gesperrte_ids (
            discord_id TEXT PRIMARY KEY,
            grund TEXT,
            gesperrt_am TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

def ist_gesperrt(discord_id):
    with db() as c:
        r = c.execute("SELECT * FROM gesperrte_ids WHERE discord_id=?", (str(discord_id),)).fetchone()
    return r is not None

def bereits_verarbeitet(message_id):
    """Prüft ob diese Nachricht schon verarbeitet wurde"""
    with db() as c:
        r = c.execute("SELECT * FROM processed_messages WHERE message_id=?", (str(message_id),)).fetchone()
    return r is not None

def markiere_verarbeitet(message_id, aktion, discord_id, betrag):
    with db() as c:
        c.execute("""
            INSERT OR IGNORE INTO processed_messages (message_id, aktion, discord_id, betrag)
            VALUES (?, ?, ?, ?)
        """, (str(message_id), aktion, str(discord_id), betrag))

def audit(aktion, discord_id, betrag, status, fehler=None, message_id=None, bot_id=None):
    """Alle Aktionen ins Audit-Log schreiben"""
    with db() as c:
        c.execute("""
            INSERT INTO audit_log (aktion, discord_id, betrag, status, fehler, message_id, bot_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (aktion, str(discord_id), betrag, status, fehler, str(message_id) if message_id else None, str(bot_id) if bot_id else None))
    log.info(f"[{status}] {aktion} | ID:{discord_id} | ${betrag:,.0f} | Fehler:{fehler}")

def get_user_by_discord_id(discord_id):
    with db() as c:
        return c.execute("""
            SELECT u.*, k.cash, k.eingezahlt, k.ausgezahlt
            FROM users u LEFT JOIN konto k ON u.id=k.user_id
            WHERE u.discord_id=?
        """, (str(discord_id),)).fetchone()

def validiere_betrag(betrag):
    """Betrag auf Plausibilität prüfen"""
    if betrag < MIN_BETRAG:
        return False, f"Betrag zu klein. Minimum: ${MIN_BETRAG}"
    if betrag > MAX_BETRAG_PRO_TRANSAKTION:
        return False, f"Betrag zu gross. Maximum: ${MAX_BETRAG_PRO_TRANSAKTION:,.0f}"
    if betrag != int(betrag):
        betrag = round(betrag, 2)
    return True, None

def einzahlen(discord_id, betrag, message_id, bot_id):
    """Sicheres Einzahlen mit allen Checks"""
    discord_id = str(discord_id)

    # Bereits verarbeitet?
    if bereits_verarbeitet(message_id):
        log.warning(f"Doppelte Nachricht ignoriert: {message_id}")
        return False, "Nachricht bereits verarbeitet (Duplikat)"

    # Gesperrt?
    if ist_gesperrt(discord_id):
        audit("EINZAHLUNG", discord_id, betrag, "GESPERRT", "ID gesperrt", message_id, bot_id)
        return False, "Discord ID ist gesperrt"

    # Betrag validieren
    ok, fehler = validiere_betrag(betrag)
    if not ok:
        audit("EINZAHLUNG", discord_id, betrag, "FEHLER", fehler, message_id, bot_id)
        return False, fehler

    # Rate-Limit
    ok, fehler = rate_limiter.check(f"einzahlung_{discord_id}")
    if not ok:
        audit("EINZAHLUNG", discord_id, betrag, "RATE_LIMIT", fehler, message_id, bot_id)
        return False, fehler

    # User finden
    with db() as c:
        user = c.execute("SELECT id FROM users WHERE discord_id=?", (discord_id,)).fetchone()
        if not user:
            audit("EINZAHLUNG", discord_id, betrag, "FEHLER", "User nicht gefunden", message_id, bot_id)
            return False, "Discord ID nicht auf Cryptex registriert. Spieler muss sich auf cryptex-gta.duckdns.org registrieren!"

        uid = user["id"]
        c.execute("UPDATE konto SET cash=cash+?, eingezahlt=eingezahlt+? WHERE user_id=?",
                  (betrag, betrag, uid))
        c.execute("""
            INSERT INTO transaktionen (user_id, typ, betrag, beschreibung, zeitpunkt)
            VALUES (?, 'einzahlung', ?, 'Staatsbot Einzahlung', CURRENT_TIMESTAMP)
        """, (uid, betrag))

    markiere_verarbeitet(message_id, "EINZAHLUNG", discord_id, betrag)
    audit("EINZAHLUNG", discord_id, betrag, "OK", None, message_id, bot_id)
    return True, None

def auszahlen(discord_id, betrag, message_id=None, bot_id=None):
    """Sicheres Auszahlen mit allen Checks"""
    discord_id = str(discord_id)

    if message_id and bereits_verarbeitet(message_id):
        return False, "Bereits verarbeitet"

    if ist_gesperrt(discord_id):
        audit("AUSZAHLUNG", discord_id, betrag, "GESPERRT", "ID gesperrt", message_id, bot_id)
        return False, "Discord ID ist gesperrt"

    ok, fehler = validiere_betrag(betrag)
    if not ok:
        audit("AUSZAHLUNG", discord_id, betrag, "FEHLER", fehler, message_id, bot_id)
        return False, fehler

    ok, fehler = rate_limiter.check(f"auszahlung_{discord_id}")
    if not ok:
        audit("AUSZAHLUNG", discord_id, betrag, "RATE_LIMIT", fehler, message_id, bot_id)
        return False, fehler

    with db() as c:
        user = c.execute("""
            SELECT u.id, k.cash FROM users u
            LEFT JOIN konto k ON u.id=k.user_id
            WHERE u.discord_id=?
        """, (discord_id,)).fetchone()

        if not user:
            audit("AUSZAHLUNG", discord_id, betrag, "FEHLER", "User nicht gefunden", message_id, bot_id)
            return False, "Discord ID nicht auf Cryptex registriert"

        cash = user["cash"] or 0
        if cash < betrag:
            audit("AUSZAHLUNG", discord_id, betrag, "FEHLER", f"Zu wenig Cash: ${cash:,.0f}", message_id, bot_id)
            return False, f"Nicht genug Geld auf Cryptex. Verfügbar: ${cash:,.0f}"

        uid = user["id"]
        c.execute("UPDATE konto SET cash=cash-?, ausgezahlt=ausgezahlt+? WHERE user_id=?",
                  (betrag, betrag, uid))
        c.execute("""
            INSERT INTO transaktionen (user_id, typ, betrag, beschreibung)
            VALUES (?, 'auszahlung', ?, 'Staatsbot Auszahlung')
        """, (uid, betrag))

    if message_id:
        markiere_verarbeitet(message_id, "AUSZAHLUNG", discord_id, betrag)
    audit("AUSZAHLUNG", discord_id, betrag, "OK", None, message_id, bot_id)
    return True, None


async def check_channel(interaction: discord.Interaction) -> bool:
    """Prüft ob Command im richtigen Channel ausgeführt wird"""
    if interaction.channel_id != COMMAND_CHANNEL_ID:
        channel = interaction.guild.get_channel(COMMAND_CHANNEL_ID)
        mention = channel.mention if channel else f"<#{COMMAND_CHANNEL_ID}>"
        await interaction.response.send_message(
            f"❌ Dieser Command funktioniert nur in {mention}!",
            ephemeral=True
        )
        return False
    return True

# ═══════════════════════════════════════════
#   BOT
# ═══════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

@bot.event
async def on_ready():
    await bot.tree.sync()
    init_security_tables()
    print(f"✅  Bridge Bot online als {bot.user}")
    print(f"    Channel  : {BANK_CHANNEL_ID}")
    print(f"    Staatsbot: {STAATSBOT_ID}")
    print(f"    Datenbank: {DB_PFAD}")
    print(f"    Audit-Log: bridge_audit.log")

# ═══════════════════════════════════════════
#   NACHRICHTEN SCANNER
# ═══════════════════════════════════════════

@bot.event
async def on_message(message):
    if message.channel.id != BANK_CHANNEL_ID:
        await bot.process_commands(message)
        return

    if message.author == bot.user:
        await bot.process_commands(message)
        return

    # ⚠️ SICHERHEIT: NUR Nachrichten vom echten Staatsbot
    if message.author.id != STAATSBOT_ID:
        if message.author.bot:
            log.warning(f"Unbekannter Bot ignoriert: {message.author} (ID: {message.author.id}) | Nachricht: {message.content[:100]}")
        await bot.process_commands(message)
        return

    text = message.content.strip()
    channel = message.channel
    msg_id = message.id
    bot_id = message.author.id

    log.info(f"Staatsbot Nachricht empfangen: {text[:100]}")

    # ── FORMAT: "Spieler 123456789 hat 5000$ eingezahlt" ──
    match_ein = re.search(
        r'[Ss]pieler\s+(\d+)\s+hat\s+([\d.,]+)\$?\s+eingezahlt',
        text
    )
    if match_ein:
        discord_id = match_ein.group(1)
        betrag_str = match_ein.group(2).replace(".", "").replace(",", "")
        try:
            betrag = float(betrag_str)
        except ValueError:
            log.error(f"Ungültiger Betrag: {betrag_str}")
            return

        ok, fehler = einzahlen(discord_id, betrag, msg_id, bot_id)

        if ok:
            user = get_user_by_discord_id(discord_id)
            name = user["username"] if user else discord_id
            await channel.send(
                f"✅ **CRYPTEX EINZAHLUNG ERFOLGREICH**\n"
                f"👤 Spieler: `{name}`\n"
                f"💵 Betrag: **${betrag:,.0f}**\n"
                f"💰 Neues Cryptex-Guthaben: **${user['cash']:,.0f}**\n"
                f"🌐 cryptex-gta.duckdns.org"
            )
        else:
            await channel.send(
                f"❌ **CRYPTEX EINZAHLUNG FEHLGESCHLAGEN**\n"
                f"👤 Discord ID: `{discord_id}`\n"
                f"💵 Betrag: **${betrag:,.0f}**\n"
                f"⚠️ Grund: {fehler}\n"
                f"{'💸 Bitte Geld manuell zurückbuchen!' if 'registriert' in fehler else ''}"
            )
        return

    # ── FORMAT: "EINZAHLUNG 123456789 5000" ──
    match_ein2 = re.match(r'EINZAHLUNG\s+(\d+)\s+([\d.,]+)', text)
    if match_ein2:
        discord_id = match_ein2.group(1)
        try:
            betrag = float(match_ein2.group(2).replace(",", "."))
        except ValueError:
            return

        ok, fehler = einzahlen(discord_id, betrag, msg_id, bot_id)

        if ok:
            user = get_user_by_discord_id(discord_id)
            await channel.send(
                f"✅ **CRYPTEX** | Einzahlung OK\n"
                f"`{discord_id}` → **${betrag:,.0f}** | Guthaben: **${user['cash']:,.0f}**"
            )
        else:
            await channel.send(
                f"❌ **CRYPTEX** | Einzahlung fehlgeschlagen\n"
                f"`{discord_id}` | Grund: {fehler}"
            )
        return

    # ── FORMAT: "AUSZAHLUNG 123456789 5000" ──
    match_aus = re.match(r'AUSZAHLUNG\s+(\d+)\s+([\d.,]+)', text)
    if match_aus:
        discord_id = match_aus.group(1)
        try:
            betrag = float(match_aus.group(2).replace(",", "."))
        except ValueError:
            return

        ok, fehler = auszahlen(discord_id, betrag, msg_id, bot_id)

        if ok:
            user = get_user_by_discord_id(discord_id)
            await channel.send(
                f"✅ **CRYPTEX AUSZAHLUNG ERFOLGREICH**\n"
                f"👤 Discord ID: `{discord_id}`\n"
                f"💵 Betrag: **${betrag:,.0f}**\n"
                f"💰 Verbleibendes Guthaben: **${user['cash']:,.0f}**\n"
                f"💸 STAATSBOT_AUSZAHLUNG {discord_id} {betrag:.0f}"
            )
        else:
            await channel.send(
                f"❌ **CRYPTEX AUSZAHLUNG FEHLGESCHLAGEN**\n"
                f"👤 Discord ID: `{discord_id}`\n"
                f"⚠️ Grund: {fehler}\n"
                f"💸 STAATSBOT_RUECKBUCHUNG {discord_id} {betrag:.0f}"
            )
        return

    await bot.process_commands(message)

# ═══════════════════════════════════════════
#   SLASH COMMANDS
# ═══════════════════════════════════════════

@bot.tree.command(name="auszahlen", description="Geld von Cryptex ins Spiel auszahlen")
async def slash_auszahlen(interaction: discord.Interaction, betrag: int):
    if not await check_channel(interaction): return
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    user = get_user_by_discord_id(discord_id)
    if not user:
        await interaction.followup.send(
            "❌ Du hast kein Cryptex-Konto.\n"
            "📝 Registriere dich auf: http://cryptex-gta.duckdns.org",
            ephemeral=True
        )
        return

    ok, fehler = auszahlen(discord_id, betrag, bot_id=bot.user.id)

    if ok:
        user_neu = get_user_by_discord_id(discord_id)
        channel = bot.get_channel(BANK_CHANNEL_ID)
        await channel.send(f"💸 STAATSBOT_AUSZAHLUNG {discord_id} {betrag}")
        await interaction.followup.send(
            f"✅ **${betrag:,.0f}** werden dir gutgeschrieben!\n"
            f"Verbleibendes Cryptex-Guthaben: **${user_neu['cash']:,.0f}**",
            ephemeral=True
        )
    else:
        await interaction.followup.send(f"❌ {fehler}", ephemeral=True)

@bot.tree.command(name="konto", description="Dein Cryptex-Kontostand")
async def slash_konto(interaction: discord.Interaction):
    if not await check_channel(interaction): return
    discord_id = str(interaction.user.id)
    user = get_user_by_discord_id(discord_id)

    if not user:
        await interaction.response.send_message(
            "❌ Kein Cryptex-Konto.\n🌐 http://cryptex-gta.duckdns.org",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"💼 **Cryptex Konto** — `{user['username']}`\n"
        f"💵 Cash: **${user['cash']:,.0f}**\n"
        f"📥 Eingezahlt: **${user['eingezahlt']:,.0f}**\n"
        f"📤 Ausgezahlt: **${user['ausgezahlt']:,.0f}**\n"
        f"🌐 http://cryptex-gta.duckdns.org",
        ephemeral=True
    )

@bot.tree.command(name="cryptex", description="Info zur Cryptex Börse")
async def slash_cryptex(interaction: discord.Interaction):
    if not await check_channel(interaction): return
    await interaction.response.send_message(
        f"📈 **CRYPTEX — GTA RP Krypto Börse**\n"
        f"🌐 http://cryptex-gta.duckdns.org\n\n"
        f"`/konto` — Kontostand\n"
        f"`/auszahlen <betrag>` — Geld auszahlen\n"
        f"`/cryptex` — Diese Info",
        ephemeral=True
    )

# Admin Commands
@bot.tree.command(name="sperren", description="Discord ID sperren (Admin)")
@discord.app_commands.default_permissions(administrator=True)
async def slash_sperren(interaction: discord.Interaction, discord_id: str, grund: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO gesperrte_ids (discord_id, grund) VALUES (?, ?)",
                  (discord_id, grund))
    log.warning(f"ID gesperrt: {discord_id} | Grund: {grund} | Admin: {interaction.user}")
    await interaction.response.send_message(
        f"🔒 `{discord_id}` wurde gesperrt.\nGrund: {grund}",
        ephemeral=True
    )

@bot.tree.command(name="entsperren", description="Discord ID entsperren (Admin)")
@discord.app_commands.default_permissions(administrator=True)
async def slash_entsperren(interaction: discord.Interaction, discord_id: str):
    with db() as c:
        c.execute("DELETE FROM gesperrte_ids WHERE discord_id=?", (discord_id,))
    await interaction.response.send_message(f"🔓 `{discord_id}` entsperrt.", ephemeral=True)

@bot.tree.command(name="auditlog", description="Letzte Transaktionen anzeigen (Admin)")
@discord.app_commands.default_permissions(administrator=True)
async def slash_auditlog(interaction: discord.Interaction):
    with db() as c:
        logs = c.execute("""
            SELECT * FROM audit_log ORDER BY zeitpunkt DESC LIMIT 10
        """).fetchall()

    if not logs:
        await interaction.response.send_message("Keine Einträge.", ephemeral=True)
        return

    text = "📋 **Letzte 10 Transaktionen**\n```\n"
    for l in logs:
        status_icon = "✅" if l["status"] == "OK" else "❌"
        text += f"{status_icon} {l['zeitpunkt'][:16]} | {l['aktion']} | ID:{l['discord_id']} | ${l['betrag']:,.0f}"
        if l["fehler"]:
            text += f" | {l['fehler']}"
        text += "\n"
    text += "```"

    await interaction.response.send_message(text, ephemeral=True)

# ═══════════════════════════════════════════
#   START
# ═══════════════════════════════════════════

bot.run(BRIDGE_TOKEN)
