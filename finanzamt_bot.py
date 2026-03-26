import discord
import re
from config import BOT_TOKEN
EINGANGS_KANAL_ID = 1485772331388375152
PING_ROLLE = "Sachbearbeiter"
KATEGORIE_PRIVAT = "Akten - Privat"
KATEGORIE_GEWERBE = "Akten - Gewerbe"
ueberweisung_ausstehend = {}

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
client = discord.Client(intents=intents)


def antrag_typ(embed):
    titel = (embed.title or "").lower()
    if "privatperson" in titel:
        return "privat"
    if "unternehmen" in titel:
        return "gewerbe"
    return "unbekannt"


def name_aus_embed(embed, typ):
    ziel_felder = ["unternehmen"] if typ == "gewerbe" else ["name"]
    for field in embed.fields:
        bereinigt = re.sub(r"[^\w\s]", "", field.name).strip().lower()
        if bereinigt in ziel_felder:
            return field.value.strip()
    return None


def discord_username_aus_embed(embed):
    for field in embed.fields:
        bereinigt = re.sub(r"[^\w\s]", "", field.name).strip().lower()
        if bereinigt == "discord":
            return field.value.strip().lower().lstrip("@")
    return None


def gesamtsteuer_aus_embed(embed):
    for field in embed.fields:
        bereinigt = re.sub(r"[^\w\s]", "", field.name).strip().lower()
        if "steuer" in bereinigt and "gesamt" in bereinigt:
            return field.value.strip()
    if embed.footer and embed.footer.text:
        match = re.search(r"Gesamtsteuer[:\s]+([^|]+)", embed.footer.text)
        if match:
            return match.group(1).strip()
    return None


def kanal_name(name):
    name = name.lower()
    name = name.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return ("akte-" + name)[:100]


async def kategorie_holen_oder_erstellen(guild, name):
    for cat in guild.categories:
        if cat.name.lower() == name.lower():
            return cat
    return await guild.create_category(name)


async def akten_kanal_holen_oder_erstellen(guild, kname, kategorie, person):
    for kanal in kategorie.text_channels:
        if kanal.name == kname:
            return kanal, False
    neuer = await kategorie.create_text_channel(name=kname, topic="Aktenmappe fuer " + person)
    return neuer, True


async def rolle_mention(guild):
    for rolle in guild.roles:
        if rolle.name.lower() == PING_ROLLE.lower():
            return rolle.mention
    return "@" + PING_ROLLE


async def user_finden(guild, username):
    username = username.lower().lstrip("@")
    async for member in guild.fetch_members(limit=None):
        if member.name.lower() == username:
            return member
        if member.display_name.lower() == username:
            return member
    return None


async def antrag_verarbeiten(message):
    guild = message.guild
    if not guild or not message.embeds:
        return False
    embed = message.embeds[0]
    typ = antrag_typ(embed)
    if typ == "unbekannt":
        await message.add_reaction("?")
        return False
    person = name_aus_embed(embed, typ)
    if not person:
        await message.add_reaction("?")
        return False
    kat_name = KATEGORIE_PRIVAT if typ == "privat" else KATEGORIE_GEWERBE
    kategorie = await kategorie_holen_oder_erstellen(guild, kat_name)
    kname = kanal_name(person)
    kanal, neu = await akten_kanal_holen_oder_erstellen(guild, kname, kategorie, person)
    ping = await rolle_mention(guild)
    status = "Neue Akte angelegt" if neu else "Neuer Antrag in bestehender Akte"
    header = ping + "\n" + status + "\nEingegangen: " + message.created_at.strftime("%d.%m.%Y %H:%M") + " Uhr"
    await kanal.send(header)
    await kanal.send(embeds=[embed])
    await message.add_reaction("\u2705")
    discord_username = discord_username_aus_embed(embed)
    if discord_username:
        user = await user_finden(guild, discord_username)
        if user:
            ueberweisung_ausstehend[user.id] = kanal.id
            steuer = gesamtsteuer_aus_embed(embed)
            steuer_text = "\nFaelliger Betrag: " + steuer if steuer else ""
            try:
                await user.send(
                    "Hallo " + person + ",\n\n"
                    "dein Steuerantrag ist eingegangen." + steuer_text + "\n\n"
                    "Bitte ueberweis den faelligen Betrag an die Staatsbank und "
                    "schicke mir ein Foto der Ueberweisung als Antwort auf diese Nachricht.\n\n"
                    "Vielen Dank\nDas Finanzamt"
                )
            except discord.Forbidden:
                await kanal.send("Hinweis: Konnte keine DM an " + discord_username + " senden.")
        else:
            await kanal.send("Warnung: Discord-User " + discord_username + " wurde nicht gefunden.")
    else:
        await kanal.send("Hinweis: Kein Discord-Username im Antrag gefunden.")
    return True


@client.event
async def on_ready():
    print("Bot eingeloggt als " + str(client.user))
    kanal = client.get_channel(EINGANGS_KANAL_ID)
    if not kanal:
        return
    nachgeholt = 0
    async for message in kanal.history(limit=200):
        hat_haekchen = any(str(r.emoji) == "\u2705" for r in message.reactions)
        if hat_haekchen:
            continue
        if message.author == client.user or not message.embeds:
            continue
        erfolg = await antrag_verarbeiten(message)
        if erfolg:
            nachgeholt += 1
    print(str(nachgeholt) + " verpasste Antraege nachgeholt")


@client.event
async def on_message(message):
    if isinstance(message.channel, discord.DMChannel):
        if message.author == client.user:
            return
        if message.author.id in ueberweisung_ausstehend:
            if message.attachments:
                kanal_id = ueberweisung_ausstehend[message.author.id]
                kanal = client.get_channel(kanal_id)
                if kanal:
                    foto_msg = await kanal.send(
                        "Ueberweisungsbeleg von " + message.author.display_name + ":",
                        files=[await a.to_file() for a in message.attachments]
                    )
                    await foto_msg.add_reaction("\u2705")
                    await message.author.send("Danke! Dein Beleg wurde eingereicht und wird geprueft.")
            else:
                await message.author.send("Bitte schicke ein Foto der Ueberweisung.")
        return
    if message.channel.id != EINGANGS_KANAL_ID:
        return
    if message.author == client.user:
        return
    await antrag_verarbeiten(message)


@client.event
async def on_raw_reaction_add(payload):
    if str(payload.emoji) != "\u2705":
        return
    if payload.user_id == client.user.id:
        return
    kanal = client.get_channel(payload.channel_id)
    if not kanal:
        return
    try:
        message = await kanal.fetch_message(payload.message_id)
    except Exception:
        return
    if message.author != client.user or not message.attachments:
        return
    for user_id, kanal_id in list(ueberweisung_ausstehend.items()):
        if kanal_id == payload.channel_id:
            try:
                user = await client.fetch_user(user_id)
                await user.send(
                    "Deine Steuerzahlung wurde bestaetigt!\n"
                    "Dein Antrag ist damit abgeschlossen.\n\n"
                    "Vielen Dank\nDas Finanzamt"
                )
            except Exception:
                pass
            del ueberweisung_ausstehend[user_id]
            break


client.run(BOT_TOKEN)
