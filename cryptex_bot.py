"""
╔══════════════════════════════════════════╗
║   CRYPTEX MARKET BOT — GTA RP Edition   ║
╚══════════════════════════════════════════╝

INSTALLATION auf dem VPS:
  pip install discord.py

STARTEN (empfohlen mit screen damit er 24/7 läuft):
  screen -S cryptex
  python3 cryptex_bot.py
  [Strg+A dann D zum Minimieren]

WIEDER ÖFFNEN:
  screen -r cryptex
"""

import discord
from discord.ext import commands, tasks
import json
import os
import random
from datetime import datetime

# ═══════════════════════════════════════════
#   ⚙️  KONFIGURATION — hier anpassen!
# ═══════════════════════════════════════════

from config import BOT_TOKEN

# Rollenname der Admins (exakt so wie in Discord geschrieben)
ADMIN_ROLE_NAME = "Rechte"

# Channel-ID wo der Bot alle X Minuten Markt-Updates postet
# 0 = deaktiviert | Rechtsklick auf Channel in Discord → ID kopieren
MARKT_CHANNEL_ID = 0

# Sekunden zwischen automatischen Markt-Updates (300 = alle 5 Minuten)
MARKT_UPDATE_INTERVAL = 300

# Datei wo alle Kontodaten gespeichert werden
DATEN_DATEI = "bank.json"

# ═══════════════════════════════════════════
#   💰 COINS — Startpreise & Volatilität
#   vol = max. Schwankung pro Tick (0.004 = sehr sanft)
#   spread = Differenz Kauf/Verkauf (0.002 = 0.2%)
# ═══════════════════════════════════════════

COINS = {
    "BTC": {"name": "Bitcoin",  "preis": 67000, "vol": 0.004, "spread": 0.002, "emoji": "🟡"},
    "ETH": {"name": "Ethereum", "preis": 3500,  "vol": 0.005, "spread": 0.002, "emoji": "🔵"},
    "SOL": {"name": "Solana",   "preis": 170,   "vol": 0.006, "spread": 0.003, "emoji": "🟣"},
    "BNB": {"name": "BNB",      "preis": 600,   "vol": 0.004, "spread": 0.003, "emoji": "🟠"},
    "ADA": {"name": "Cardano",  "preis": 0.48,  "vol": 0.007, "spread": 0.004, "emoji": "🔷"},
    "XRP": {"name": "Ripple",   "preis": 0.52,  "vol": 0.006, "spread": 0.004, "emoji": "🩵"},
}

# ═══════════════════════════════════════════
#   📁 DATEN
# ═══════════════════════════════════════════

def lade():
    if not os.path.exists(DATEN_DATEI):
        return {"spieler": {}, "markt": {}}
    with open(DATEN_DATEI, "r", encoding="utf-8") as f:
        return json.load(f)

def speichere(daten):
    with open(DATEN_DATEI, "w", encoding="utf-8") as f:
        json.dump(daten, f, indent=2, ensure_ascii=False)

def init_markt(daten):
    if not daten["markt"]:
        for sym, info in COINS.items():
            daten["markt"][sym] = {
                "preis": info["preis"],
                "history": [info["preis"]] * 24,
            }
        speichere(daten)

def konto(daten, uid):
    uid = str(uid)
    if uid not in daten["spieler"]:
        daten["spieler"][uid] = {"cash": 0, "portfolio": {}, "eingezahlt": 0, "ausgezahlt": 0}
    return daten["spieler"][uid]

# ═══════════════════════════════════════════
#   📈 MARKT ENGINE
# ═══════════════════════════════════════════

def tick(daten):
    for sym, info in COINS.items():
        c = daten["markt"][sym]
        p = c["preis"]
        # Sanfter Drift zurück zum Startpreis verhindert Ausbrennen
        abw = (p - info["preis"]) / info["preis"]
        reversion = -abw * 0.02
        noise = (random.random() - 0.5) * info["vol"] * 0.5
        shock = (random.random() - 0.5) * info["vol"] * 2 if random.random() < 0.03 else 0
        neu = p * (1 + reversion + noise + shock)
        # Preis bleibt immer zwischen 70% und 130% des Startpreises
        neu = max(info["preis"] * 0.70, min(info["preis"] * 1.30, neu))
        c["preis"] = round(neu, 6)
        c["history"].append(round(neu, 6))
        if len(c["history"]) > 48:
            c["history"].pop(0)
    speichere(daten)

# ═══════════════════════════════════════════
#   🔧 HILFSFUNKTIONEN
# ═══════════════════════════════════════════

def fp(preis):
    """Preis formatieren"""
    if preis >= 1000:
        return f"${preis:,.0f}"
    elif preis >= 1:
        return f"${preis:.2f}"
    else:
        return f"${preis:.4f}"

def fg(betrag):
    """Geld formatieren"""
    return f"${betrag:,.0f}"

def aenderung(history):
    if len(history) < 2:
        return 0.0
    return ((history[-1] - history[0]) / history[0]) * 100

def minichart(history, w=18):
    if len(history) < 2:
        return "─" * w
    h = history[-w:]
    mi, ma = min(h), max(h)
    if ma == mi:
        return "─" * w
    out = []
    for v in h:
        r = (v - mi) / (ma - mi)
        out.append("▲" if r > 0.6 else ("─" if r > 0.35 else "▼"))
    return "".join(out)

def ist_admin(ctx):
    return any(r.name == ADMIN_ROLE_NAME for r in ctx.author.roles)

def markt_embed(daten):
    embed = discord.Embed(
        title="📈  CRYPTEX MARKT",
        description=f"🕐 Stand: {datetime.now().strftime('%d.%m.%Y  %H:%M:%S')}",
        color=0x00e676
    )
    for sym, info in COINS.items():
        c = daten["markt"][sym]
        p = c["preis"]
        kauf = p * (1 + info["spread"])
        verk = p * (1 - info["spread"])
        chg = aenderung(c["history"])
        pfeil = "🟢" if chg >= 0 else "🔴"
        chart = minichart(c["history"])
        embed.add_field(
            name=f"{info['emoji']}  {sym}  —  {info['name']}",
            value=(
                f"`{chart}`\n"
                f"**Kurs:** {fp(p)}   {pfeil} `{chg:+.2f}%`\n"
                f"🟢 Kaufen: `{fp(kauf)}`   🔴 Verkaufen: `{fp(verk)}`"
            ),
            inline=False
        )
    embed.set_footer(text="!kaufen <COIN> <MENGE>  •  !verkaufen <COIN> <MENGE>  •  !konto  •  !hilfe")
    return embed

# ═══════════════════════════════════════════
#   🤖 BOT
# ═══════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

@tasks.loop(seconds=30)
async def markt_tick():
    d = lade()
    tick(d)

@tasks.loop(seconds=MARKT_UPDATE_INTERVAL)
async def markt_post():
    if MARKT_CHANNEL_ID == 0:
        return
    ch = bot.get_channel(MARKT_CHANNEL_ID)
    if ch:
        d = lade()
        await ch.send(embed=markt_embed(d))

@bot.event
async def on_ready():
    d = lade()
    init_markt(d)
    markt_tick.start()
    if MARKT_CHANNEL_ID != 0:
        markt_post.start()
    print(f"✅  CRYPTEX Bot online  |  Eingeloggt als: {bot.user}")
    print(f"    Admin-Rolle : {ADMIN_ROLE_NAME}")
    print(f"    Daten-Datei : {DATEN_DATEI}")

# ─── !hilfe ───────────────────────────────

@bot.command(name="hilfe", aliases=["help"])
async def hilfe(ctx):
    embed = discord.Embed(title="📖  CRYPTEX — Alle Befehle", color=0x00e676)
    embed.add_field(name="📊  Markt & Kurse", value=(
        "`!markt` — Alle Kurse mit Chart\n"
        "`!kurse` — Kompakte Preisübersicht"
    ), inline=False)
    embed.add_field(name="💼  Dein Konto", value=(
        "`!konto` — Guthaben & Portfolio anzeigen\n"
        "`!konto @spieler` — Fremdes Konto *(Admin)*"
    ), inline=False)
    embed.add_field(name="💹  Trading", value=(
        "`!kaufen BTC 0.5` — Krypto kaufen\n"
        "`!verkaufen ETH 1` — Krypto verkaufen"
    ), inline=False)
    embed.add_field(name="🏆  Rangliste", value="`!rangliste` — Top 10 reichste Spieler", inline=False)
    embed.add_field(name="🔒  Admin-Befehle", value=(
        "`!einzahlen @spieler 5000` — Cash einzahlen\n"
        "`!auszahlen @spieler 5000` — Cash auszahlen\n"
        "`!reset @spieler` — Konto löschen"
    ), inline=False)
    embed.add_field(name="💡  Verfügbare Coins", value="BTC  •  ETH  •  SOL  •  BNB  •  ADA  •  XRP", inline=False)
    await ctx.send(embed=embed)

# ─── !markt ───────────────────────────────

@bot.command(name="markt", aliases=["m", "market"])
async def markt_cmd(ctx):
    d = lade()
    await ctx.send(embed=markt_embed(d))

# ─── !kurse ───────────────────────────────

@bot.command(name="kurse", aliases=["preise"])
async def kurse_cmd(ctx):
    d = lade()
    lines = []
    for sym, info in COINS.items():
        c = d["markt"][sym]
        chg = aenderung(c["history"])
        pfeil = "🟢" if chg >= 0 else "🔴"
        lines.append(f"{info['emoji']} **{sym}** `{fp(c['preis'])}` {pfeil} `{chg:+.2f}%`")
    embed = discord.Embed(title="💹  Aktuelle Kurse", description="\n".join(lines), color=0x00e676)
    embed.set_footer(text=datetime.now().strftime("%d.%m.%Y  %H:%M:%S"))
    await ctx.send(embed=embed)

# ─── !konto ───────────────────────────────

@bot.command(name="konto", aliases=["wallet", "depot", "balance"])
async def konto_cmd(ctx, member: discord.Member = None):
    if member and not ist_admin(ctx):
        await ctx.send("❌  Nur Admins können fremde Konten einsehen.")
        return
    ziel = member or ctx.author
    d = lade()
    sp = konto(d, ziel.id)

    port_wert = 0
    port_lines = []
    for sym, menge in sp["portfolio"].items():
        if menge > 0.000001 and sym in d["markt"]:
            p = d["markt"][sym]["preis"]
            wert = menge * p
            port_wert += wert
            info = COINS.get(sym, {})
            port_lines.append(f"{info.get('emoji','•')} **{sym}**: {menge:.6f} = {fp(wert)}")

    gesamt = sp["cash"] + port_wert
    embed = discord.Embed(title=f"💼  Konto von {ziel.display_name}", color=0x00e676)
    embed.add_field(name="💵  Cash",           value=fg(sp["cash"]),   inline=True)
    embed.add_field(name="📊  Portfolio-Wert", value=fp(port_wert),    inline=True)
    embed.add_field(name="💰  Gesamtwert",     value=fp(gesamt),       inline=True)
    embed.add_field(
        name="📦  Positionen",
        value="\n".join(port_lines) if port_lines else "*Keine Krypto im Depot*",
        inline=False
    )
    embed.add_field(
        name="📋  Statistik",
        value=f"Eingezahlt: {fg(sp['eingezahlt'])}   |   Ausgezahlt: {fg(sp['ausgezahlt'])}",
        inline=False
    )
    await ctx.send(embed=embed)

# ─── !kaufen ──────────────────────────────

@bot.command(name="kaufen", aliases=["buy"])
async def kaufen_cmd(ctx, coin: str = None, menge_str: str = None):
    if not coin or not menge_str:
        await ctx.send("❌  Nutzung: `!kaufen BTC 0.5`")
        return
    coin = coin.upper()
    if coin not in COINS:
        await ctx.send(f"❌  Unbekannter Coin. Verfügbar: {', '.join(COINS.keys())}")
        return
    try:
        menge = float(menge_str.replace(",", "."))
    except ValueError:
        await ctx.send("❌  Ungültige Menge.")
        return
    if menge <= 0:
        await ctx.send("❌  Menge muss größer als 0 sein.")
        return

    d = lade()
    sp = konto(d, ctx.author.id)
    info = COINS[coin]
    p = d["markt"][coin]["preis"]
    kaufpreis = p * (1 + info["spread"])
    kosten = round(menge * kaufpreis, 2)

    if sp["cash"] < kosten:
        await ctx.send(
            f"❌  Nicht genug Cash!\n"
            f"Benötigt: **{fg(kosten)}**   |   Verfügbar: **{fg(sp['cash'])}**"
        )
        return

    sp["cash"] = round(sp["cash"] - kosten, 2)
    sp["portfolio"][coin] = round(sp["portfolio"].get(coin, 0) + menge, 8)
    speichere(d)

    embed = discord.Embed(title="✅  Kauf erfolgreich!", color=0x00e676)
    embed.add_field(name="Coin",           value=f"{info['emoji']} {coin}",  inline=True)
    embed.add_field(name="Menge",          value=f"{menge:.6f}",             inline=True)
    embed.add_field(name="Kaufpreis",      value=fp(kaufpreis),              inline=True)
    embed.add_field(name="Gesamt bezahlt", value=fg(kosten),                 inline=True)
    embed.add_field(name="Cash verbleibend", value=fg(sp["cash"]),           inline=True)
    embed.set_footer(text=f"Bestand: {sp['portfolio'].get(coin,0):.6f} {coin}")
    await ctx.send(embed=embed)

# ─── !verkaufen ───────────────────────────

@bot.command(name="verkaufen", aliases=["sell"])
async def verkaufen_cmd(ctx, coin: str = None, menge_str: str = None):
    if not coin or not menge_str:
        await ctx.send("❌  Nutzung: `!verkaufen ETH 1`")
        return
    coin = coin.upper()
    if coin not in COINS:
        await ctx.send(f"❌  Unbekannter Coin. Verfügbar: {', '.join(COINS.keys())}")
        return
    try:
        menge = float(menge_str.replace(",", "."))
    except ValueError:
        await ctx.send("❌  Ungültige Menge.")
        return
    if menge <= 0:
        await ctx.send("❌  Menge muss größer als 0 sein.")
        return

    d = lade()
    sp = konto(d, ctx.author.id)
    bestand = sp["portfolio"].get(coin, 0)

    if bestand < menge:
        await ctx.send(
            f"❌  Nicht genug {coin}!\n"
            f"Benötigt: **{menge:.6f}**   |   Verfügbar: **{bestand:.6f}**"
        )
        return

    info = COINS[coin]
    p = d["markt"][coin]["preis"]
    vkpreis = p * (1 - info["spread"])
    erloes = round(menge * vkpreis, 2)

    sp["cash"] = round(sp["cash"] + erloes, 2)
    neuer_bestand = round(bestand - menge, 8)
    if neuer_bestand < 0.000001:
        sp["portfolio"].pop(coin, None)
    else:
        sp["portfolio"][coin] = neuer_bestand
    speichere(d)

    embed = discord.Embed(title="✅  Verkauf erfolgreich!", color=0xff4d4d)
    embed.add_field(name="Coin",             value=f"{info['emoji']} {coin}", inline=True)
    embed.add_field(name="Menge",            value=f"{menge:.6f}",            inline=True)
    embed.add_field(name="Verkaufspreis",    value=fp(vkpreis),               inline=True)
    embed.add_field(name="Erlös",            value=fg(erloes),                inline=True)
    embed.add_field(name="Neuer Cash",       value=fg(sp["cash"]),            inline=True)
    embed.set_footer(text=f"Verbleibend: {sp['portfolio'].get(coin,0):.6f} {coin}")
    await ctx.send(embed=embed)

# ─── !rangliste ───────────────────────────

@bot.command(name="rangliste", aliases=["top", "rl", "leaderboard"])
async def rangliste_cmd(ctx):
    d = lade()
    eintraege = []
    for uid, sp in d["spieler"].items():
        pw = sum(
            sp["portfolio"].get(sym, 0) * d["markt"][sym]["preis"]
            for sym in COINS if sym in d["markt"]
        )
        gesamt = sp.get("cash", 0) + pw
        try:
            m = ctx.guild.get_member(int(uid))
            name = m.display_name if m else f"Unbekannt"
        except:
            name = "Unbekannt"
        eintraege.append((name, gesamt))

    eintraege.sort(key=lambda x: x[1], reverse=True)
    medaillen = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = [f"{medaillen[i]}  **{n}** — {fp(g)}" for i, (n, g) in enumerate(eintraege[:10])]

    embed = discord.Embed(
        title="🏆  CRYPTEX — Rangliste Top 10",
        description="\n".join(lines) if lines else "*Noch keine Spieler vorhanden*",
        color=0xf7c948
    )
    embed.set_footer(text=f"Gesamtvermögen = Cash + Portfolio  |  {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    await ctx.send(embed=embed)

# ─── !einzahlen (Admin) ───────────────────

@bot.command(name="einzahlen", aliases=["ez", "deposit"])
async def einzahlen_cmd(ctx, member: discord.Member = None, betrag_str: str = None):
    if not ist_admin(ctx):
        await ctx.send("❌  Nur Admins können Geld einzahlen.")
        return
    if not member or not betrag_str:
        await ctx.send("❌  Nutzung: `!einzahlen @spieler 5000`")
        return
    try:
        betrag = float(betrag_str.replace(",", "").replace(".", ""))
        if betrag < 1:
            betrag = float(betrag_str)
    except:
        try:
            betrag = float(betrag_str)
        except:
            await ctx.send("❌  Ungültiger Betrag.")
            return
    if betrag <= 0:
        await ctx.send("❌  Betrag muss größer als 0 sein.")
        return

    d = lade()
    sp = konto(d, member.id)
    sp["cash"] = round(sp["cash"] + betrag, 2)
    sp["eingezahlt"] = round(sp["eingezahlt"] + betrag, 2)
    speichere(d)

    embed = discord.Embed(title="💵  Einzahlung erfolgreich", color=0x00e676)
    embed.add_field(name="Spieler",         value=member.mention,    inline=True)
    embed.add_field(name="Eingezahlt",      value=fg(betrag),         inline=True)
    embed.add_field(name="Neues Guthaben",  value=fg(sp["cash"]),     inline=True)
    embed.set_footer(text=f"Admin: {ctx.author.display_name}")
    await ctx.send(embed=embed)
    try:
        await member.send(
            f"💵  **Einzahlung erhalten!**\n"
            f"**{fg(betrag)}** wurden auf dein CRYPTEX-Konto eingezahlt.\n"
            f"Neues Guthaben: **{fg(sp['cash'])}**\n"
            f"Verwende `!kaufen <COIN> <MENGE>` um zu investieren!"
        )
    except:
        pass

# ─── !auszahlen (Admin) ───────────────────

@bot.command(name="auszahlen", aliases=["az", "withdraw"])
async def auszahlen_cmd(ctx, member: discord.Member = None, betrag_str: str = None):
    if not ist_admin(ctx):
        await ctx.send("❌  Nur Admins können Geld auszahlen.")
        return
    if not member or not betrag_str:
        await ctx.send("❌  Nutzung: `!auszahlen @spieler 5000`")
        return
    try:
        betrag = float(betrag_str)
    except:
        await ctx.send("❌  Ungültiger Betrag.")
        return
    if betrag <= 0:
        await ctx.send("❌  Betrag muss größer als 0 sein.")
        return

    d = lade()
    sp = konto(d, member.id)
    if sp["cash"] < betrag:
        await ctx.send(
            f"❌  {member.display_name} hat nicht genug Cash!\n"
            f"Verfügbar: **{fg(sp['cash'])}**   |   Gewünscht: **{fg(betrag)}**"
        )
        return

    sp["cash"] = round(sp["cash"] - betrag, 2)
    sp["ausgezahlt"] = round(sp["ausgezahlt"] + betrag, 2)
    speichere(d)

    embed = discord.Embed(title="💸  Auszahlung erfolgreich", color=0xff4d4d)
    embed.add_field(name="Spieler",               value=member.mention,  inline=True)
    embed.add_field(name="Ausgezahlt",            value=fg(betrag),       inline=True)
    embed.add_field(name="Verbleibendes Guthaben", value=fg(sp["cash"]), inline=True)
    embed.set_footer(text=f"Admin: {ctx.author.display_name}")
    await ctx.send(embed=embed)
    try:
        await member.send(
            f"💸  **Auszahlung verarbeitet!**\n"
            f"**{fg(betrag)}** wurden von deinem CRYPTEX-Konto abgezogen.\n"
            f"Verbleibendes Guthaben: **{fg(sp['cash'])}**"
        )
    except:
        pass

# ─── !reset (Admin) ───────────────────────

@bot.command(name="reset")
async def reset_cmd(ctx, member: discord.Member = None):
    if not ist_admin(ctx):
        await ctx.send("❌  Nur Admins können Konten zurücksetzen.")
        return
    if not member:
        await ctx.send("❌  Nutzung: `!reset @spieler`")
        return
    d = lade()
    uid = str(member.id)
    if uid in d["spieler"]:
        del d["spieler"][uid]
        speichere(d)
    await ctx.send(f"✅  Konto von **{member.display_name}** wurde vollständig zurückgesetzt.")

# ─── Fehlerbehandlung ─────────────────────

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("❌  Spieler nicht gefunden — benutze @mention.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.send(f"❌  Fehler: {error}")

# ─── START ────────────────────────────────

bot.run(BOT_TOKEN)
