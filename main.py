import asyncio
import json
import logging
import os
import re
import tempfile
import uuid

import aiohttp
import discord
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Set the DISCORD_TOKEN environment variable before starting the bot.")

modmail_channel_id = os.getenv("modmail_channel_id", "").strip()
if not modmail_channel_id.isdigit() or int(modmail_channel_id) <= 0:
    raise RuntimeError("Set modmail_channel_id to the staff modmail channel ID in .env.")

role_ids_value = os.getenv("role_ids", "").strip()
try:
    ROLE_IDS = tuple(dict.fromkeys(
        int(role_id.strip()) for role_id in role_ids_value.split(",") if role_id.strip()
    ))
except ValueError as error:
    raise RuntimeError("Set role_ids to a comma-separated list of Discord role IDs in .env.") from error
if not ROLE_IDS or any(role_id <= 0 for role_id in ROLE_IDS):
    raise RuntimeError("Set role_ids to a comma-separated list of Discord role IDs in .env.")

PREFIX = "s!"
STICKY_FILE = "stickies.json"
LOG_FILE = "logchannels.json"
INVITE_SOURCE_FILE = "invitesources.json"
INFRACTION_FILE = "infractions.json"
APPEALS_FILE = "appeals.json"
MUTE_ROLE_ID = 1558947443444555958
MODMAIL_CHANNEL_ID = int(modmail_channel_id)
LOG_COMMANDS = {"memberlog": "member", "automodlog": "automod", "messagelog": "message", "serverlog": "server"}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # also enable "Server Members Intent" in the Developer Portal
intents.auto_moderation_execution = True  # also enable "Message Content Intent" in the Developer Portal
intents.guilds = True  # for channel and role events
client = discord.Client(intents=intents)

# channel_id -> {"text": str, "last_id": int | None}
stickies = {}
locks = {}


def load_stickies():
    if os.path.exists(STICKY_FILE):
        with open(STICKY_FILE) as f:
            for cid, data in json.load(f).items():
                stickies[int(cid)] = data


def save_stickies():
    with open(STICKY_FILE, "w") as f:
        json.dump({str(cid): data for cid, data in stickies.items()}, f)


# guild_id -> {"member": channel_id, "automod": channel_id, "message": channel_id}
log_channels = {}


def load_logs():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            for gid, data in json.load(f).items():
                log_channels[int(gid)] = data


def save_logs():
    with open(LOG_FILE, "w") as f:
        json.dump({str(gid): data for gid, data in log_channels.items()}, f)


invite_sources = {}


def load_invite_sources():
    if os.path.exists(INVITE_SOURCE_FILE):
        with open(INVITE_SOURCE_FILE) as f:
            for gid, sources in json.load(f).items():
                invite_sources[int(gid)] = sources


def save_invite_sources():
    with open(INVITE_SOURCE_FILE, "w") as f:
        json.dump({str(gid): sources for gid, sources in invite_sources.items()}, f)


def clip(text, limit=1000):
    text = text or "*(empty)*"
    return text if len(text) <= limit else text[:limit] + "…"


async def send_log(guild_id, kind, embed, extra_embeds=()):
    cid = log_channels.get(guild_id, {}).get(kind)
    guild = client.get_guild(guild_id)
    channel = guild.get_channel(cid) if guild and cid else None
    if channel is None:
        return
    try:
        await channel.send(embeds=[embed, *list(extra_embeds)[:9]])
    except discord.HTTPException:
        pass


async def repost_sticky(channel):
    lock = locks.setdefault(channel.id, asyncio.Lock())
    async with lock:
        data = stickies.get(channel.id)
        if not data:
            return
        if data.get("last_id"):
            try:
                old = await channel.fetch_message(data["last_id"])
                await old.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                pass
        sent = await channel.send(data["text"])
        data["last_id"] = sent.id
        save_stickies()


invite_cache = {}  # guild_id -> {code: (uses, inviter, invite)}
vanity_uses = {}  # guild_id -> uses


async def snapshot_invites(guild):
    try:
        invites = await guild.invites()
    except discord.HTTPException:
        return  # missing Manage Server permission
    invite_cache[guild.id] = {i.code: (i.uses or 0, i.inviter, i) for i in invites}
    if "VANITY_URL" in guild.features:
        try:
            vanity_uses[guild.id] = (await guild.vanity_invite()).uses or 0
        except discord.HTTPException:
            pass


def format_invite_attribution(guild_id, code, invite):
    source = invite_sources.get(guild_id, {}).get(code)
    if source:
        lines = [f"Website/source: {source}"]
    else:
        lines = [
            "Website/source: unknown (Discord does not share the referring webpage; "
            "register a unique invite for each site with `s!invitesource`)"
        ]
    lines.append(f"Invite: https://discord.gg/{code}")
    inviter = getattr(invite, "inviter", None)
    if inviter:
        lines.append(f"Created by: {inviter.mention} ({inviter})")
    channel = getattr(invite, "channel", None)
    if channel:
        lines.append(f"Invite channel: {channel.mention}")
    return "\n".join(lines)


async def detect_join_method(member):
    guild = member.guild
    if member.bot:
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
                if entry.target and entry.target.id == member.id:
                    return f"Bot added by {entry.user.mention} ({entry.user})"
        except discord.HTTPException:
            pass
        return "Bot added via OAuth2 link"
    old = invite_cache.get(guild.id)
    if old is None:
        return "Unknown (invite tracking needs the Manage Server permission; Discord does not provide website referrers)"
    try:
        invites = await guild.invites()
    except discord.HTTPException:
        return "Unknown (I couldn't read server invites; check the bot's Manage Server permission)"
    new = {i.code: (i.uses or 0, i.inviter, i) for i in invites}
    invite_cache[guild.id] = new
    used = [c for c, (uses, _, _) in new.items() if uses > old.get(c, (0,))[0]]
    gone = [c for c in old if c not in new]  # single-use/expired invites vanish once used
    if len(used) == 1 or (not used and len(gone) == 1):
        code = used[0] if used else gone[0]
        inv = new[code][2] if used else old[code][2]
        return format_invite_attribution(guild.id, code, inv)
    if "VANITY_URL" in guild.features:
        try:
            uses = (await guild.vanity_invite()).uses or 0
        except discord.HTTPException:
            uses = None
        if uses is not None and uses > vanity_uses.get(guild.id, uses):
            vanity_uses[guild.id] = uses
            code = guild.vanity_url_code or "?"
            return "Vanity URL\n" + format_invite_attribution(guild.id, code, None)
        if uses is not None:
            vanity_uses[guild.id] = uses
    if len(used) > 1:
        details = [format_invite_attribution(guild.id, code, new[code][2]) for code in used]
        return "Unclear - multiple invite uses changed during this join:\n" + "\n\n".join(details)
    return (
        "Unknown (no invite-use change was detected. Discord does not provide the "
        "referring website; use a separate registered invite link for each site.)"
    )


@client.event
async def on_invite_create(invite):
    if invite.guild:
        await snapshot_invites(invite.guild)


@client.event
async def on_invite_delete(invite):
    pass  # keep cached copy so single-use invites can be matched on join


@client.event
async def on_ready():
    load_stickies()
    load_logs()
    load_invite_sources()
    load_infractions()
    load_appeals()
    for g in client.guilds:
        await snapshot_invites(g)
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user or getattr(message.author, "bot", False):
        return

    if message.guild is None:
        try:
            await handle_appeal_dm(message)
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.warning("Could not process or respond to DM from user %s: %s", message.author.id, error)
        return

    if message.guild and message.content.startswith(PREFIX):
        parts = message.content[len(PREFIX):].split(maxsplit=1)
        if parts and parts[0].lower() in MOD_COMMANDS:
            await run_mod_command(message, parts[0].lower(), parts[1] if len(parts) > 1 else "")
            return

    if message.channel.id in appeals:
        await handle_appeal_staff_message(message, appeals[message.channel.id])
        return

    if message.guild and message.content.startswith(PREFIX):
        parts = message.content[len(PREFIX):].split(maxsplit=2)
        command = parts[0].lower() if parts else ""
        if command in {"invitesource", "invitesources"}:
            if not message.author.guild_permissions.manage_guild:
                await message.channel.send("You need the Manage Server permission to manage invite sources.")
                return
            if command == "invitesources":
                sources = invite_sources.get(message.guild.id, {})
                lines = [f"`{code}` → {source}" for code, source in sources.items()]
                description = "\n".join(lines) if lines else "No invite sources are configured."
                await message.channel.send(embed=discord.Embed(
                    title="Invite website sources", description=clip(description, 4000)))
                return
            if len(parts) < 3:
                await message.channel.send(
                    f"Usage: `{PREFIX}invitesource <invite-code-or-link> <website>` "
                    f"or `{PREFIX}invitesource <invite-code-or-link> remove`")
                return
            code = parts[1].split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            source = parts[2].strip()
            if not code or any(char.isspace() for char in code) or len(code) > 100:
                await message.channel.send("❌ That invite code doesn't look valid.")
                return
            if source.lower() == "remove":
                sources = invite_sources.get(message.guild.id, {})
                if sources.pop(code, None) is None:
                    await message.channel.send(f"No website source was configured for `{code}`.")
                else:
                    if not sources:
                        invite_sources.pop(message.guild.id, None)
                    save_invite_sources()
                    await message.channel.send(f"✅ Removed the website source for `{code}`.")
                return
            if not source or len(source) > 200:
                await message.channel.send("❌ The website label must be between 1 and 200 characters.")
                return
            sources = invite_sources.setdefault(message.guild.id, {})
            sources[code] = source
            save_invite_sources()
            await message.channel.send(f"✅ Website source saved for invite `{code}`.")
            return

    if message.guild and message.content.lower().startswith(("!sticky", "!mute", "!ban", "!unban", "!warn", "!memberlog", "!automodlog", "!messagelog")):
        await message.channel.send(f"The prefix is `{PREFIX}`, e.g. `{PREFIX}sticky hello #channel`")
        return

    if message.guild and message.content.startswith(PREFIX):
        name = message.content[len(PREFIX):].split(maxsplit=1)[0].lower() if message.content[len(PREFIX):].strip() else ""
        if name in LOG_COMMANDS:
            if not message.author.guild_permissions.manage_guild:
                await message.channel.send("You need the Manage Server permission to do that.")
                return
            if not message.channel_mentions:
                await message.channel.send(f"Usage: `{PREFIX}{name} #channel`")
                return
            target = message.channel_mentions[0]
            log_channels.setdefault(message.guild.id, {})[LOG_COMMANDS[name]] = target.id
            save_logs()
            await message.channel.send(f"✅ {LOG_COMMANDS[name].capitalize()} log set to {target.mention}!")
            return

    if message.content.lower().strip() == PREFIX + "ping":
        start = time.perf_counter()
        sent = await message.channel.send("🏓 Pinging...")
        round_trip = (time.perf_counter() - start) * 1000
        ws = client.latency * 1000
        status = "🟢 Online" if ws < 250 else "🟡 Slow" if ws < 500 else "🔴 Lagging"
        embed = discord.Embed(title="🏓 Pong!", color=discord.Color.green() if ws < 250 else discord.Color.orange())
        embed.add_field(name="Status", value=status)
        embed.add_field(name="Websocket", value=f"{ws:.0f} ms")
        embed.add_field(name="Response time", value=f"{round_trip:.0f} ms")
        await sent.edit(content=None, embed=embed)
        return

    if message.content.lower().strip() == PREFIX + "commands":
        p = PREFIX
        embed = discord.Embed(title="📖 Commands", description=f"Prefix: `{p}`", color=discord.Color.blurple())
        embed.add_field(name="🛡️ Moderation", inline=False, value=(
            f"`{p}warn @user <reason>` - warn a user\n"
            f"`{p}mute @user <time> <reason>` - mute (e.g. `10m`, `2h`, `1d`, `1w`)\n"
            f"`{p}ban @user <reason>` - ban a user (they get an appeal button)\n"
            f"`{p}unban <user id>` - unban a user"))
        embed.add_field(name="📋 Infractions", inline=False, value=(
            f"`{p}warns @user` - view warnings\n"
            f"`{p}mutes @user` - view mutes\n"
            f"`{p}bans @user` - view bans"))
        embed.add_field(name="📝 Logging (Manage Server)", inline=False, value=(
            "\n".join(f"`{p}{c} #channel` - set the {k} log channel" for c, k in LOG_COMMANDS.items())))
        embed.add_field(name="🔗 Invite attribution (Manage Server)", inline=False, value=(
            f"`{p}invitesource <invite-code-or-link> <website>` - label a unique invite for a website\n"
            f"`{p}invitesource <invite-code-or-link> remove` - remove a source label\n"
            f"`{p}invitesources` - list source labels"))
        embed.add_field(name="📌 Utility", inline=False, value=(
            f"`{p}sticky <message> #channel` - sticky message (Manage Messages)\n"
            f"`{p}ping` - latency and bot status\n"
            f"`{p}commands` - this list"))
        await message.channel.send(embed=embed)
        return

    if message.guild and message.content.startswith(PREFIX + "sticky"):
        if not message.author.guild_permissions.manage_messages:
            await message.channel.send("You need the Manage Messages permission to do that.")
            return
        if not message.channel_mentions:
            await message.channel.send("Usage: `s!sticky <message> #channel`")
            return
        target = message.channel_mentions[-1]
        text = message.content[len(PREFIX + "sticky"):]
        for ch in message.channel_mentions:
            text = text.replace(ch.mention, "")
        text = text.strip()
        if not text:
            await message.channel.send("Usage: `s!sticky <message> #channel`")
            return
        stickies[target.id] = {"text": text, "last_id": None}
        try:
            await repost_sticky(target)
        except discord.Forbidden:
            stickies.pop(target.id, None)
            await message.channel.send(f"❌ I can't send messages in {target.mention}. Give me View Channel + Send Messages there.")
            return
        except discord.HTTPException as e:
            stickies.pop(target.id, None)
            await message.channel.send(f"❌ Failed: {e}")
            return
        await message.channel.send(f"✅ Sticky message set in {target.mention}!")
        return

    if message.channel.id in stickies:
        await repost_sticky(message.channel)



# ---------------- Moderation ----------------
import re
import time

MOD_COMMANDS = {"mute", "ban", "unban", "warn", "bans", "mutes", "warns"}
infractions = {"bans": [], "mutes": [], "warns": []}
USER_RE = re.compile(r"^<@!?(\d+)>$|^(\d{15,22})$")
DURATION_RE = re.compile(r"^(\d+)([smhdw])$", re.I)
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def load_infractions():
    if os.path.exists(INFRACTION_FILE):
        with open(INFRACTION_FILE) as f:
            infractions.update(json.load(f))


def save_infractions():
    save_json_atomic(INFRACTION_FILE, infractions)


appeals = {}
appeal_locks: dict[int, asyncio.Lock] = {}


def load_appeals():
    if os.path.exists(APPEALS_FILE):
        with open(APPEALS_FILE) as f:
            loaded = {int(tid): record for tid, record in json.load(f).items()}
        migrated = False
        for thread_id, ticket in loaded.items():
            ban = next((record for record in reversed(infractions["bans"])
                        if record["guild_id"] == ticket["guild_id"]
                        and record["user_id"] == ticket["user_id"]
                        and ("ban_id" not in ticket or ban_record_id(record) == ticket["ban_id"])), None)
            created_at = next((entry["timestamp"] for entry in ticket.get("history", [])
                               if entry.get("timestamp")), None)
            defaults = {
                "appeal_id": str(thread_id),
                "ban_reason": ban["reason"] if ban else None,
                "created_at": created_at,
                "last_activity_at": next((entry["timestamp"] for entry in reversed(ticket.get("history", []))
                                          if entry.get("timestamp")), created_at),
                "assigned_moderator_id": None,
                "history": [],
            }
            if ban and "ban_id" not in ticket:
                ticket["ban_id"] = ban_record_id(ban)
                migrated = True
            for key, value in defaults.items():
                if key not in ticket:
                    ticket[key] = value
                    migrated = True
        appeals.clear()
        appeals.update(loaded)
        if migrated:
            save_appeals()


def save_appeals():
    save_json_atomic(APPEALS_FILE, {str(tid): record for tid, record in appeals.items()})


def save_json_atomic(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as f:
            temporary_path = f.name
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def ban_record_id(record):
    if "id" in record:
        return record["id"]
    return uuid.uuid5(uuid.NAMESPACE_OID, json.dumps(record, sort_keys=True)).hex


def appeal_lock(user_id):
    return appeal_locks.setdefault(user_id, asyncio.Lock())


def appeal_for_ban(user_id, guild_id, ban_id):
    return next((
        ticket for ticket in appeals.values()
        if ticket["user_id"] == user_id and ticket["guild_id"] == guild_id
        and (ticket.get("ban_id") == ban_id or
             ("ban_id" not in ticket and ticket["status"] == "open"))
    ), None)


def message_already_recorded(ticket, message):
    message_id = getattr(message, "id", None)
    return message_id is not None and any(
        entry.get("message_id") == message_id for entry in ticket.get("history", [])
    )


def record_appeal_message(ticket, message, direction, body, delivery_status="pending"):
    timestamp = discord.utils.utcnow().isoformat()
    entry = {
        "message_id": getattr(message, "id", None),
        "author_id": message.author.id,
        "direction": direction,
        "content": body,
        "source_channel_id": getattr(message.channel, "id", None),
        "timestamp": timestamp,
        "delivery_status": delivery_status,
    }
    ticket.setdefault("history", []).append(entry)
    ticket["last_activity_at"] = timestamp
    save_appeals()
    return entry


def update_delivery(entry, status, error=None):
    entry["delivery_status"] = status
    if error is not None:
        entry["delivery_error"] = {
            "type": type(error).__name__,
            "status": getattr(error, "status", None),
            "code": getattr(error, "code", None),
            "message": str(error),
        }
    save_appeals()


async def send_appeal_notice(destination, text):
    try:
        await destination.send(text)
    except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
        logger.warning("Could not send appeal notice (%s): %s", type(error).__name__, error)


async def send_appeal_text(destination, prefix, body):
    text = prefix + body
    for offset in range(0, len(text), 1900):
        await destination.send(text[offset:offset + 1900], allowed_mentions=discord.AllowedMentions.none())


def parse_user_id(token):
    m = USER_RE.match(token or "")
    return int(m.group(1) or m.group(2)) if m else None


def parse_duration(token):
    m = DURATION_RE.match(token or "")
    return int(m.group(1)) * UNITS[m.group(2).lower()] if m else None


def fmt_seconds(sec):
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if sec >= size:
            parts.append(f"{sec // size}{name}")
            sec %= size
    return " ".join(parts) or "0s"


async def send_dm(user, embed, view=None):
    try:
        channel = await user.create_dm()
        await channel.send(embed=embed, view=view)
    except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
        logger.warning("DM delivery to user %s failed (%s): %s", user.id, type(error).__name__, error)
        return error
    return None


async def dm_user(user, embed, view=None):
    return await send_dm(user, embed, view) is None


def describe_dm_failure(error):
    if isinstance(error, discord.HTTPException):
        if error.code == 50007:
            return "Discord refused the DM (50007: Cannot send messages to this user)."
        if error.code == 50278:
            return "Discord refused the DM (50278: no eligible mutual server or DM access)."
        return f"Discord API request failed (HTTP {error.status}, error code {error.code}); check bot logs."
    return "The Discord API request failed due to a network error; check bot logs."


def message_text(message):
    parts = [message.content] if message.content and message.content.strip() else []
    parts.extend(attachment.url for attachment in message.attachments)
    return "\n".join(parts)


def parse_appeal_request(content):
    text = content.strip()
    if text.lower().startswith(PREFIX + "appeal"):
        text = text[len(PREFIX + "appeal"):].strip()
        tokens = text.split(maxsplit=1)
        if tokens and tokens[0].isdigit():
            return int(tokens[0]), tokens[1] if len(tokens) > 1 else ""
        return None, text
    return None, content


def open_appeals_for(user_id):
    return [
        (thread_id, record) for thread_id, record in appeals.items()
        if record["user_id"] == user_id and record["status"] == "open"
    ]


async def forward_user_message(message, ticket, body=None):
    if message_already_recorded(ticket, message):
        return
    body = body if body is not None else message_text(message)
    if not body:
        await send_appeal_notice(message.channel, "Please include a message or attachment with your appeal.")
        return
    entry = record_appeal_message(ticket, message, "user", body)
    thread = client.get_channel(ticket["thread_id"])
    if thread is None:
        try:
            thread = await client.fetch_channel(ticket["thread_id"])
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.warning("Could not fetch appeal thread %s (%s): %s",
                           ticket["thread_id"], type(error).__name__, error)
            update_delivery(entry, "failed", error)
            await send_appeal_notice(message.channel, "❌ I couldn't find your open appeal thread. Please contact the server staff.")
            return
    try:
        await send_appeal_text(
            thread, f"**{getattr(message.author, 'global_name', None) or message.author.name}:**\n", body
        )
    except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
        logger.warning("Could not forward user message %s to appeal %s (%s): %s",
                       message.id, ticket["thread_id"], type(error).__name__, error)
        update_delivery(entry, "failed", error)
        await send_appeal_notice(message.channel, "❌ I couldn't deliver your message to the staff. Please send it again later.")
        return
    update_delivery(entry, "sent")
    await send_appeal_notice(message.channel, "✅ Your message was sent to the staff team.")


async def handle_appeal_dm(message):
    async with appeal_lock(message.author.id):
        await process_appeal_dm(message)


async def process_appeal_dm(message):
    guild_id, appeal_text = parse_appeal_request(message_text(message))
    active = open_appeals_for(message.author.id)
    if guild_id is not None:
        ticket = next((record for _, record in active if record["guild_id"] == guild_id), None)
        if ticket:
            await forward_user_message(message, ticket, appeal_text)
            return
    elif len(active) == 1:
        await forward_user_message(message, active[0][1], appeal_text)
        return
    elif len(active) > 1:
        guild_ids = ", ".join(str(record["guild_id"]) for _, record in active)
        await message.channel.send(
            f"You have open modmail threads for servers {guild_ids}. "
            f"Use `{PREFIX}appeal <server_id> <message>` to choose one."
        )
        return

    if not appeal_text:
        await message.channel.send(
            f"Send your appeal in a DM to open a thread. If you are banned in multiple servers, "
            f"use `{PREFIX}appeal <server_id> <reason>`."
        )
        return

    bans = {}
    for record in reversed(infractions["bans"]):
        if record["user_id"] == message.author.id and (
            guild_id is None or record["guild_id"] == guild_id
        ):
            bans.setdefault(record["guild_id"], record)
    if not bans:
        await message.channel.send("❌ I couldn't find a ban record for you. Please contact the server staff.")
        return

    active_bans = []
    for ban_guild_id, record in bans.items():
        guild = client.get_guild(ban_guild_id)
        if guild is None:
            continue
        try:
            await guild.fetch_ban(discord.Object(id=message.author.id))
        except discord.NotFound:
            continue
        except discord.Forbidden as error:
            logger.warning("Could not verify ban for user %s in guild %s: %s", message.author.id, ban_guild_id, error)
            await message.channel.send(
                f"❌ I can't verify your ban in {guild.name}. Please contact the server staff."
            )
            return
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.warning("Could not verify ban for user %s in guild %s: %s", message.author.id, ban_guild_id, error)
            await message.channel.send(
                f"❌ I couldn't verify your ban in {guild.name}. Please try again later."
            )
            return
        active_bans.append((guild, record))

    if not active_bans:
        await message.channel.send("❌ I couldn't find an active ban for you in a server I support.")
        return
    if len(active_bans) > 1:
        servers = ", ".join(f"{guild.name} (`{guild.id}`)" for guild, _ in active_bans)
        await message.channel.send(
            f"You're banned in more than one server: {servers}. "
            f"Please send `{PREFIX}appeal <server_id> <reason>`."
        )
        return

    guild, ban_record = active_bans[0]
    await create_appeal(message.author, message.channel, guild, ban_record, appeal_text, message)


async def create_appeal(user, reply_channel, guild, ban_record, appeal_text="", message=None):
    ban_id = ban_record_id(ban_record)
    open_ticket = next((ticket for _, ticket in open_appeals_for(user.id)
                        if ticket["guild_id"] == guild.id), None)
    if open_ticket and open_ticket.get("ban_id") not in (None, ban_id):
        await reply_channel.send(
            "You already have an open appeal for an earlier ban in this server. "
            "Ask staff to close it before appealing the new ban."
        )
        return
    existing = appeal_for_ban(user.id, guild.id, ban_id)
    if existing:
        if existing["status"] == "open":
            await reply_channel.send("You already have an open appeal for this ban. Reply here to continue.")
        else:
            await reply_channel.send("This ban has already been appealed and the appeal is closed.")
        return
    channel = client.get_channel(MODMAIL_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(MODMAIL_CHANNEL_ID)
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.warning("Could not fetch staff modmail channel %s: %s", MODMAIL_CHANNEL_ID, error)
            await reply_channel.send("❌ The modmail channel is unavailable. Please contact the server staff.")
            return

    user_name = getattr(user, "global_name", None) or user.name
    embed = discord.Embed(title="Ban appeal", color=discord.Color.gold(),
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Server", value=f"{guild.name} (`{guild.id}`)", inline=False)
    embed.add_field(name="Ban reason", value=clip(ban_record["reason"]), inline=False)
    embed.add_field(name="Appeal", value=clip(appeal_text or "Waiting for the user's message."), inline=False)
    embed.set_footer(text=f"User ID: {user.id} | Guild ID: {guild.id}")
    intro = (
        f"New modmail from **{user_name}** (`{user.id}`).\n"
        "Type here to reply (it is DMed to them). Start a message with `//` "
        "for an internal note that is NOT sent. Use `!close` to close this. "
        "Use `!claim` or `!unassign` to manage the assigned moderator.\n\n"
        f"**Appeal:**\n{appeal_text or 'Waiting for the user to reply.'}"
    )
    starter = None
    try:
        starter = await channel.send(
            content=" ".join(f"<@&{role_id}>" for role_id in ROLE_IDS),
            embed=embed,
            view=AppealReviewView(),
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[discord.Object(id=role_id) for role_id in ROLE_IDS],
                replied_user=False,
            ),
        )
        thread = await starter.create_thread(name=f"Appeal - {user_name} ({user.id})"[:100])
    except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
        logger.warning("Could not create appeal thread for user %s: %s", user.id, error)
        if starter is not None:
            try:
                await starter.delete()
            except discord.HTTPException as cleanup_error:
                logger.warning("Could not remove failed appeal starter message: %s", cleanup_error)
        await reply_channel.send("❌ I couldn't create your modmail thread. Please contact the server staff.")
        return

    created_at = discord.utils.utcnow().isoformat()
    appeals[thread.id] = {
        "appeal_id": str(thread.id),
        "thread_id": thread.id,
        "user_id": user.id,
        "guild_id": guild.id,
        "ban_id": ban_id,
        "ban_reason": ban_record["reason"],
        "created_at": created_at,
        "last_activity_at": created_at,
        "assigned_moderator_id": None,
        "starter_id": getattr(starter, "id", thread.id),
        "status": "open",
        "history": [],
    }
    if message is not None:
        appeals[thread.id]["history"].append({
            "message_id": getattr(message, "id", None),
            "author_id": user.id,
            "direction": "user",
            "content": appeal_text,
            "timestamp": created_at,
            "source_channel_id": getattr(message.channel, "id", None),
            "delivery_status": "pending",
        })
    save_appeals()
    try:
        await send_appeal_text(thread, "", intro)
    except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
        logger.warning("Could not send opening message to appeal %s: %s", thread.id, error)
        if message is not None:
            update_delivery(appeals[thread.id]["history"][0], "failed", error)
        await reply_channel.send(
            "⚠️ Your appeal thread was created, but I couldn't add the opening message. "
            "Please resend your message so staff receive the full text."
        )
        return
    if message is not None:
        update_delivery(appeals[thread.id]["history"][0], "sent")
    await reply_channel.send("✅ Your appeal was sent to the staff team. Reply here to continue the conversation.")


async def handle_appeal_staff_message(message, ticket):
    async with appeal_lock(ticket["user_id"]):
        await process_appeal_staff_message(message, ticket)


async def process_appeal_staff_message(message, ticket):
    if not message.author.guild_permissions.ban_members:
        await message.channel.send("❌ You need the Ban Members permission to manage this appeal.")
        return
    if ticket["status"] != "open":
        await message.channel.send("❌ This appeal is closed; your message was not sent.")
        return

    text = message.content.strip()
    if text.lower() in {"!claim", "!unassign"}:
        ticket["assigned_moderator_id"] = message.author.id if text.lower() == "!claim" else None
        ticket["last_activity_at"] = discord.utils.utcnow().isoformat()
        save_appeals()
        await message.channel.send(
            f"✅ Assigned to <@{message.author.id}>." if text.lower() == "!claim"
            else "✅ Moderator assignment cleared.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    if text.lower() in {"!close", PREFIX + "close"}:
        ticket["status"] = "closed"
        ticket["last_activity_at"] = discord.utils.utcnow().isoformat()
        save_appeals()
        await message.channel.send("🔒 This modmail thread is closed.")
        try:
            user = await client.fetch_user(ticket["user_id"])
            await user.send("🔒 Your ban appeal conversation has been closed.")
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.warning("Could not notify user %s of closed appeal: %s", ticket["user_id"], error)
            await message.channel.send("⚠️ I couldn't notify the user that this appeal was closed.")
        try:
            await message.channel.edit(archived=True, locked=True)
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.warning("Could not archive appeal thread %s: %s", ticket["thread_id"], error)
            await message.channel.send("⚠️ The thread could not be archived. Please archive it manually.")
        return
    if text.startswith("//"):
        return

    body = message_text(message)
    if not body:
        return
    if message_already_recorded(ticket, message):
        return
    entry = record_appeal_message(ticket, message, "staff", body)
    try:
        user = await client.fetch_user(ticket["user_id"])
        await send_appeal_text(user, "**Staff reply:**\n", body)
    except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
        logger.warning("Could not deliver staff message %s to user %s (%s): %s",
                       message.id, ticket["user_id"], type(error).__name__, error)
        update_delivery(entry, "failed", error)
        await message.channel.send(f"❌ I couldn't DM the user. {describe_dm_failure(error)}")
        return
    update_delivery(entry, "sent")


class AppealStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Appeal Ban", style=discord.ButtonStyle.primary, custom_id="appeal_start")
    async def start(self, interaction, button):
        if interaction.guild is not None or not interaction.message.embeds:
            await interaction.response.send_message("This button can only be used in the ban DM.", ephemeral=True)
            return
        footer = interaction.message.embeds[0].footer.text or ""
        match = re.fullmatch(r"Server ID: (\d+) \| Ban ID: ([0-9a-f]{32})", footer)
        if not match:
            await interaction.response.send_message("❌ This ban notice is invalid.", ephemeral=True)
            return
        guild_id, ban_id = int(match.group(1)), match.group(2)
        await interaction.response.defer()
        async with appeal_lock(interaction.user.id):
            ban = next((record for record in reversed(infractions["bans"])
                        if record["guild_id"] == guild_id and record["user_id"] == interaction.user.id), None)
            if ban is None or ban_record_id(ban) != ban_id:
                await interaction.followup.send("❌ This ban notice is no longer valid.")
                return
            guild = client.get_guild(guild_id)
            if guild is None:
                await interaction.followup.send("❌ The server is unavailable. Please contact its staff.")
                return
            try:
                await guild.fetch_ban(discord.Object(id=interaction.user.id))
            except discord.NotFound:
                await interaction.followup.send("❌ This ban is no longer active.")
                return
            except discord.Forbidden as error:
                logger.warning("Could not verify appeal button ban for user %s: %s", interaction.user.id, error)
                await interaction.followup.send("❌ I can't verify this ban. Please contact the server staff.")
                return
            except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
                logger.warning("Could not verify appeal button ban for user %s: %s", interaction.user.id, error)
                await interaction.followup.send("❌ I couldn't verify this ban. Please try again later.")
                return
            await create_appeal(interaction.user, interaction.followup, guild, ban)


class AppealReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def handle(self, interaction, approved):
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ You need the Ban Members permission.", ephemeral=True)
            return
        
        if not interaction.message.embeds:
            await interaction.response.send_message("❌ Couldn't find this appeal.", ephemeral=True)
            return
        old = interaction.message.embeds[0]
        footer_text = old.footer.text or ""
        
        user_match = re.search(r"User ID: (\d+)", footer_text)
        guild_match = re.search(r"Guild ID: (\d+)", footer_text)
        
        if not user_match:
            await interaction.response.send_message("❌ Couldn't find the user in this appeal.", ephemeral=True)
            return
        
        uid = int(user_match.group(1))
        guild_id = int(guild_match.group(1)) if guild_match else None
        
        await interaction.response.defer()
        async with appeal_lock(uid):
            await self.review(interaction, approved, old, uid, guild_id)

    async def review(self, interaction, approved, old, uid, guild_id):
        tickets = [
            (thread_id, ticket) for thread_id, ticket in open_appeals_for(uid)
            if ticket["guild_id"] == guild_id
            and ("starter_id" not in ticket or ticket["starter_id"] == interaction.message.id)
        ]
        if len(tickets) != 1:
            await interaction.followup.send("❌ This appeal is closed or could not be found.", ephemeral=True)
            return
        thread_id, ticket = tickets[0]
        guild = client.get_guild(guild_id) if guild_id else None
        if guild is None:
            await interaction.followup.send("❌ The appealed server is unavailable.", ephemeral=True)
            return
        latest_ban = next((record for record in reversed(infractions["bans"])
                           if record["guild_id"] == guild_id and record["user_id"] == uid), None)
        if ticket.get("ban_id") and (latest_ban is None or ban_record_id(latest_ban) != ticket["ban_id"]):
            await interaction.followup.send(
                "❌ This appeal is for an older ban. Close it and review the current ban separately.",
                ephemeral=True,
            )
            return
        try:
            user = await client.fetch_user(uid)
        except discord.HTTPException:
            await interaction.followup.send("❌ Couldn't find the user. Please try again.", ephemeral=True)
            return
        
        guild_name = guild.name

        if approved:
            try:
                await guild.unban(user, reason=f"Appeal approved by {interaction.user}")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                await interaction.followup.send("❌ I can't unban that user (missing Ban Members).", ephemeral=True)
                return
            except discord.HTTPException:
                await interaction.followup.send("❌ Couldn't unban that user. Please try again.", ephemeral=True)
                return
        
        result = "approved" if approved else "declined"
        ticket["status"] = result
        ticket["reviewer_id"] = interaction.user.id
        ticket["last_activity_at"] = discord.utils.utcnow().isoformat()
        save_appeals()
        dm = discord.Embed(title=f"Appeal {result}",
                           color=discord.Color.green() if approved else discord.Color.red(),
                           description=f"Your ban appeal for {guild_name} has been {result}.")
        sent = await dm_user(user, dm)
        embed = old.copy()
        embed.color = discord.Color.green() if approved else discord.Color.red()
        embed.add_field(name="Result", value=f"{'Approved' if approved else 'Declined'} by {interaction.user.mention}", inline=False)
        disabled_view = AppealReviewView()
        for child in disabled_view.children:
            child.disabled = True
        await interaction.edit_original_response(embed=embed, view=disabled_view)
        thread = client.get_channel(thread_id)
        if thread is None:
            try:
                thread = await client.fetch_channel(thread_id)
            except discord.HTTPException:
                await interaction.followup.send(
                    "⚠️ The appeal was reviewed, but its modmail thread could not be found.", ephemeral=True
                )
        if thread is not None:
            try:
                await thread.edit(archived=True, locked=True)
            except (discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    "⚠️ The appeal was reviewed, but its modmail thread could not be archived.",
                    ephemeral=True,
                )
        if not sent:
            await interaction.followup.send(
                f"⚠️ Appeal {result}, but I couldn't DM the user.", ephemeral=True
            )

    @discord.ui.button(label="Approved", style=discord.ButtonStyle.success, custom_id="appeal_approve")
    async def approve(self, interaction, button):
        await self.handle(interaction, True)

    @discord.ui.button(label="Declined", style=discord.ButtonStyle.danger, custom_id="appeal_decline")
    async def decline(self, interaction, button):
        await self.handle(interaction, False)


async def unmute_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        now = int(time.time())
        changed = False
        for m in infractions["mutes"]:
            if m.get("active") and m.get("duration") and m["ts"] + m["duration"] <= now:
                m["active"] = False
                changed = True
                guild = client.get_guild(m["guild_id"])
                member = guild.get_member(m["user_id"]) if guild else None
                role = guild.get_role(MUTE_ROLE_ID) if guild else None
                if member and role:
                    try:
                        await member.remove_roles(role, reason="Mute expired")
                    except discord.HTTPException:
                        pass
        if changed:
            save_infractions()
        await asyncio.sleep(30)


async def setup():
    print("setup_hook: registering views")
    client.add_view(AppealReviewView())
    client.add_view(AppealStartView())
    asyncio.create_task(unmute_loop())


client.setup_hook = setup


def need(message, perm):
    if getattr(message.author.guild_permissions, perm):
        return True
    return False


async def list_infractions(message, kind, args):
    label = {"bans": "Ban", "mutes": "Mute", "warns": "Warn"}[kind]
    uid = parse_user_id(args.split()[0]) if args.split() else None
    entries = [e for e in infractions[kind] if e["guild_id"] == message.guild.id
               and (uid is None or e["user_id"] == uid)]
    if not entries:
        await message.channel.send(f"No {kind} found.")
        return
    lines = []
    for i, e in enumerate(entries, 1):
        if uid is not None:
            block = f"**{label} {i}**\n"
        else:
            block = f"**{label} {i}** - <@{e['user_id']}> ({e['user_id']})\n"
        if kind == "mutes":
            block += f"Time: {fmt_seconds(e['duration']) if e.get('duration') else 'Permanent'}\n"
        block += f"Reason: {e['reason']}\n"
        lines.append(block)
    title = f"{label}s" + (f" for user {uid}" if uid else "")
    embeds, buf = [], ""
    for block in lines:
        if len(buf) + len(block) > 3500:
            embeds.append(buf)
            buf = ""
        buf += block + "\n"
    embeds.append(buf)
    for i in range(0, len(embeds), 10):
        await message.channel.send(embeds=[
            discord.Embed(title=title, description=d, color=discord.Color.blurple())
            for d in embeds[i:i + 10]])


async def run_mod_command(message, name, args):
    guild = message.guild
    if name in ("bans", "mutes", "warns"):
        if not need(message, "moderate_members"):
            await message.channel.send("❌ You need the Timeout Members permission to do that.")
            return
        await list_infractions(message, name, args)
        return

    needed = "ban_members" if name in ("ban", "unban") else "moderate_members"
    if not need(message, needed):
        await message.channel.send("❌ You don't have permission to do that.")
        return

    tokens = args.split()
    uid = parse_user_id(tokens[0]) if tokens else None
    if uid is None:
        usage = {"mute": "s!mute @user <time> <reason>", "ban": "s!ban @user <reason>", "unban": "s!unban @user|userid", "warn": "s!warn @user <reason>"}[name]
        await message.channel.send(f"Usage: `{usage}`")
        return
    rest = args.split(maxsplit=1)[1] if len(tokens) > 1 else ""

    if name == "unban":
        try:
            await guild.unban(discord.Object(id=uid), reason=f"Unbanned by {message.author}")
        except discord.NotFound:
            await message.channel.send("❌ That user isn't banned.")
            return
        except discord.Forbidden:
            await message.channel.send("❌ I can't unban users. Give me the Ban Members permission.")
            return
        await message.channel.send(f"✅ Unbanned <@{uid}> ({uid}).")
        return

    if uid in (message.author.id, client.user.id):
        await message.channel.send("❌ You can't do that to that user.")
        return

    ts = int(time.time())
    base = {"guild_id": guild.id, "user_id": uid, "mod_id": message.author.id, "ts": ts}

    if name == "warn":
        reason = rest or "No reason given"
        user = guild.get_member(uid) or await client.fetch_user(uid)
        infractions["warns"].append({**base, "reason": reason})
        save_infractions()
        embed = discord.Embed(title=f"You have been warned in {guild.name}", color=discord.Color.yellow())
        embed.add_field(name="Reason", value=clip(reason))
        embed.add_field(name="Wrong?", value="If you think this was wrong, please ping a staff member.", inline=False)
        sent = await dm_user(user, embed)
        await message.channel.send(f"✅ Warned <@{uid}>." + ("" if sent else " (Couldn't DM them.)"))
        return

    if name == "mute":
        member = guild.get_member(uid)
        role = guild.get_role(MUTE_ROLE_ID)
        if member is None:
            await message.channel.send("❌ That user isn't in the server.")
            return
        if role is None:
            await message.channel.send("❌ Mute role not found.")
            return
        sub = rest.split(maxsplit=1)
        duration = parse_duration(sub[0]) if sub else None
        if duration is None:
            await message.channel.send("Usage: `s!mute @user <time> <reason>` - time like `10m`, `2h`, `1d`, `1w`")
            return
        reason = sub[1] if len(sub) > 1 else "No reason given"
        try:
            await member.add_roles(role, reason=f"Muted by {message.author}: {reason}")
        except discord.Forbidden:
            await message.channel.send("❌ I can't give that role. Move my role above the mute role and the user's top role.")
            return
        infractions["mutes"].append({**base, "reason": reason, "duration": duration, "active": True})
        save_infractions()
        embed = discord.Embed(title=f"You have been muted in {guild.name}", color=discord.Color.orange())
        embed.add_field(name="Time", value=fmt_seconds(duration))
        embed.add_field(name="Reason", value=clip(reason), inline=False)
        embed.add_field(name="Wrong?", value="If you think this was wrong, please ping a staff member.", inline=False)
        sent = await dm_user(member, embed)
        await message.channel.send(f"✅ Muted <@{uid}> for {fmt_seconds(duration)}." + ("" if sent else " (Couldn't DM them.)"))
        return

    reason = rest or "No reason given"
    is_hacked = "hacked" in rest.lower()
    member = guild.get_member(uid)
    if member and message.author.top_role <= member.top_role:
        await message.channel.send("❌ You can't ban a member with a role equal to or higher than yours.")
        return

    user = member
    lookup_error = None
    if user is None:
        try:
            user = await client.fetch_user(uid)
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.warning("Could not fetch user %s for ban DM (%s): %s", uid, type(error).__name__, error)
            lookup_error = error
    
    if is_hacked:
        await message.channel.send(f"🔄 Deleting all messages from <@{uid}>...")
        deleted_count = 0
        try:
            for channel in guild.text_channels:
                try:
                    async for msg in channel.history(limit=None):
                        if msg.author.id == uid:
                            await msg.delete()
                            deleted_count += 1
                except discord.Forbidden:
                    pass
        except Exception as e:
            pass
    
    ban_id = uuid.uuid4().hex
    dm_embed = discord.Embed(title=f"A moderator is banning you from {guild.name}",
                             color=discord.Color.red())
    dm_embed.add_field(name="Reason", value=clip(reason), inline=False)
    dm_embed.add_field(
        name="Appeal",
        value=(f"If the ban succeeds, click Appeal Ban or DM this bot to appeal. "
               f"For this server, use `{PREFIX}appeal {guild.id} <reason>`."),
        inline=False,
    )
    dm_embed.set_footer(text=f"Server ID: {guild.id} | Ban ID: {ban_id}")
    dm_error = lookup_error if lookup_error is not None else await send_dm(
        user, dm_embed, view=AppealStartView()
    )
    sent = dm_error is None
    ban_target = user if user is not None else discord.Object(id=uid)

    try:
        await guild.ban(ban_target, reason=f"{message.author}: {reason}")
    except discord.Forbidden:
        await message.channel.send(
            "❌ I can't ban that user. Move my role above theirs and give me Ban Members."
            + (" The user was notified that a ban was attempted." if sent else "")
        )
        return
    except discord.HTTPException:
        await message.channel.send(
            "❌ The ban failed. Please try again."
            + (" The user was notified that a ban was attempted." if sent else "")
        )
        return
    
    infractions["bans"].append({**base, "reason": reason, "id": ban_id})
    save_infractions()
    
    result = (f"✅ Banned <@{uid}> and deleted {deleted_count} messages."
              if is_hacked else f"✅ Banned <@{uid}>.")
    if dm_error is not None:
        if lookup_error is not None:
            result += " ⚠️ I couldn't fetch the user to send appeal instructions. "
        else:
            result += " ⚠️ I couldn't DM them the appeal instructions. "
        result += describe_dm_failure(dm_error)
    await message.channel.send(result)
    
    embed = discord.Embed(title=f"User banned", color=discord.Color.red())
    embed.add_field(name="User", value=f"{ban_target} (ID: {uid})", inline=False)
    embed.add_field(name="Reason", value=clip(reason), inline=False)
    embed.add_field(name="Appeal instructions", 
                    value="The user can DM this bot to appeal.",
                    inline=False)
    embed.set_footer(text=f"Server ID: {guild.id}")
    await send_log(guild.id, "server", embed)


@client.event
async def on_interaction(interaction):
    print(f"interaction: type={interaction.type.name} data={interaction.data}")


@client.event
async def on_member_join(member):
    how = await detect_join_method(member)
    embed = discord.Embed(title="Member joined", color=discord.Color.green(),
                          description=f"{member.mention} ({member})",
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
    embed.add_field(name="Joined via", value=clip(how), inline=False)
    embed.set_footer(text=f"ID: {member.id}")
    embed.set_thumbnail(url=member.display_avatar.url)
    await send_log(member.guild.id, "member", embed)


@client.event
async def on_member_remove(member):
    await asyncio.sleep(2)  # give Discord time to register a ban
    ban = None
    try:
        ban = await member.guild.fetch_ban(member)
    except discord.HTTPException:
        pass
    embed = discord.Embed(title="Member banned" if ban else "Member left", color=discord.Color.red(),
                          description=f"{member.mention} ({member})",
                          timestamp=discord.utils.utcnow())
    if ban:
        embed.add_field(name="Reason", value=clip(ban.reason or "No reason given"), inline=False)
    embed.set_footer(text=f"ID: {member.id}")
    embed.set_thumbnail(url=member.display_avatar.url)
    await send_log(member.guild.id, "member", embed)


@client.event
async def on_automod_action(execution):
    embed = discord.Embed(title="AutoMod action", color=discord.Color.orange(),
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="User", value=f"<@{execution.user_id}> ({execution.user_id})")
    if execution.channel_id:
        embed.add_field(name="Channel", value=f"<#{execution.channel_id}>")
    embed.add_field(name="Rule", value=clip(getattr(execution.rule_trigger_type, "name", str(execution.rule_trigger_type))))
    embed.add_field(name="Action", value=clip(execution.action.type.name))
    if execution.matched_keyword:
        embed.add_field(name="Matched keyword", value=clip(execution.matched_keyword), inline=False)
    if execution.content:
        embed.add_field(name="Message", value=clip(execution.content), inline=False)
    await send_log(execution.guild_id, "automod", embed)


@client.event
async def on_raw_message_delete(payload):
    if not payload.guild_id:
        for ticket in list(appeals.values()):
            async with appeal_lock(ticket["user_id"]):
                entry = next((entry for entry in ticket.get("history", [])
                              if entry.get("direction") == "user"
                              and entry.get("message_id") == payload.message_id), None)
                if entry is None or entry.get("deleted_at"):
                    continue
                entry["deleted_at"] = discord.utils.utcnow().isoformat()
                ticket["last_activity_at"] = entry["deleted_at"]
                save_appeals()
                thread = client.get_channel(ticket["thread_id"])
                try:
                    if thread is None:
                        thread = await client.fetch_channel(ticket["thread_id"])
                    await send_appeal_text(
                        thread, "", f"ℹ️ The user deleted their DM (message ID: `{payload.message_id}`)."
                    )
                except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
                    logger.warning("Could not report deleted DM %s to appeal %s: %s",
                                   payload.message_id, ticket["thread_id"], error)
                break
        return
    msg = payload.cached_message
    if msg is None or msg.author.bot:
        return  # can't tell who sent it (or it was a bot), so don't log it
    embed = discord.Embed(title="Message deleted", color=discord.Color.red(),
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
    embed.add_field(name="Author", value=f"{msg.author.mention} ({msg.author})")
    embed.add_field(name="Content", value=clip(msg.content), inline=False)
    if msg.attachments:
        embed.add_field(name="Attachments", value=clip("\n".join(a.url for a in msg.attachments)), inline=False)
    extra = msg.embeds
    embed.set_footer(text=f"Author ID: {msg.author.id} | Message ID: {msg.id}")
    await send_log(payload.guild_id, "message", embed, extra)


@client.event
async def on_raw_message_edit(payload):
    if not payload.guild_id:
        return
    after = payload.message
    before = payload.cached_message
    if after.author.bot:
        return
    if before is None:
        return  # not cached, so we can't show what changed
    if before.content == after.content and before.embeds == after.embeds:
        return
    embed = discord.Embed(title="Message edited", color=discord.Color.blue(),
                          description=f"[Jump to message]({after.jump_url})",
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Author", value=f"{after.author.mention} ({after.author})")
    embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
    embed.add_field(name="Before", value=clip(before.content), inline=False)
    embed.add_field(name="After", value=clip(after.content), inline=False)
    embed.set_footer(text=f"Author ID: {after.author.id} | Message ID: {after.id}")
    await send_log(payload.guild_id, "message", embed, after.embeds)


@client.event
async def on_guild_channel_create(channel):
    embed = discord.Embed(
        title="Channel created",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Name", value=channel.mention)
    embed.add_field(name="Type", value=channel.type.name.replace("_", " ").title())
    embed.set_footer(text=f"Channel ID: {channel.id}")
    await send_log(channel.guild.id, "server", embed)


@client.event
async def on_guild_channel_delete(channel):
    embed = discord.Embed(
        title="Channel deleted",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Name", value=channel.name)
    embed.add_field(name="Type", value=channel.type.name.replace("_", " ").title())
    embed.set_footer(text=f"Channel ID: {channel.id}")
    await send_log(channel.guild.id, "server", embed)


@client.event
async def on_guild_channel_update(before, after):
    if before.name != after.name:
        embed = discord.Embed(
            title="Channel renamed",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Before", value=before.name)
        embed.add_field(name="After", value=after.mention)
        embed.set_footer(text=f"Channel ID: {after.id}")
        await send_log(after.guild.id, "server", embed)
    elif before.topic != after.topic:
        embed = discord.Embed(
            title="Channel topic updated",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Channel", value=after.mention)
        embed.add_field(name="Before", value=clip(before.topic or "*(no topic)*"), inline=False)
        embed.add_field(name="After", value=clip(after.topic or "*(no topic)*"), inline=False)
        embed.set_footer(text=f"Channel ID: {after.id}")
        await send_log(after.guild.id, "server", embed)


@client.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        added_roles = [r for r in after.roles if r not in before.roles]
        removed_roles = [r for r in before.roles if r not in after.roles]
        
        if added_roles:
            embed = discord.Embed(
                title="Role(s) added",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Member", value=f"{after.mention} ({after})")
            embed.add_field(
                name="Roles added",
                value="\n".join(r.mention for r in added_roles),
                inline=False
            )
            embed.set_footer(text=f"User ID: {after.id}")
            await send_log(after.guild.id, "server", embed)
        
        if removed_roles:
            embed = discord.Embed(
                title="Role(s) removed",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Member", value=f"{after.mention} ({after})")
            embed.add_field(
                name="Roles removed",
                value="\n".join(r.mention for r in removed_roles),
                inline=False
            )
            embed.set_footer(text=f"User ID: {after.id}")
            await send_log(after.guild.id, "server", embed)


@client.event
async def on_guild_role_create(role):
    embed = discord.Embed(
        title="Role created",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Role", value=role.mention)
    embed.add_field(name="Color", value=str(role.color))
    embed.set_footer(text=f"Role ID: {role.id}")
    await send_log(role.guild.id, "server", embed)


@client.event
async def on_guild_role_delete(role):
    embed = discord.Embed(
        title="Role deleted",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Role", value=role.name)
    embed.add_field(name="Color", value=str(role.color))
    embed.set_footer(text=f"Role ID: {role.id}")
    await send_log(role.guild.id, "server", embed)


@client.event
async def on_guild_role_update(before, after):
    if before.name != after.name:
        embed = discord.Embed(
            title="Role renamed",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Before", value=before.name)
        embed.add_field(name="After", value=after.mention)
        embed.set_footer(text=f"Role ID: {after.id}")
        await send_log(after.guild.id, "server", embed)


client.run(token)
