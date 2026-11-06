import asyncio
import json
import os

import discord


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Set the DISCORD_TOKEN environment variable before starting the bot.")

PREFIX = "s!"
STICKY_FILE = "stickies.json"
LOG_FILE = "logchannels.json"
INFRACTION_FILE = "infractions.json"
MUTE_ROLE_ID = 1558947443444555958
APPEAL_CHANNEL_ID = 1568339091702026300
LOG_COMMANDS = {"memberlog": "member", "automodlog": "automod", "messagelog": "message"}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # also enable "Server Members Intent" in the Developer Portal
intents.auto_moderation_execution = True  # also enable "Message Content Intent" in the Developer Portal
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


@client.event
async def on_ready():
    load_stickies()
    load_logs()
    load_infractions()
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.guild and message.content.startswith(PREFIX):
        parts = message.content[len(PREFIX):].split(maxsplit=1)
        if parts and parts[0].lower() in MOD_COMMANDS:
            await run_mod_command(message, parts[0].lower(), parts[1] if len(parts) > 1 else "")
            return

    if message.guild and message.content.lower().startswith(("!sticky", "!mute", "!ban", "!warn", "!memberlog", "!automodlog", "!messagelog")):
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

MOD_COMMANDS = {"mute", "ban", "warn", "bans", "mutes", "warns"}
infractions = {"bans": [], "mutes": [], "warns": []}
USER_RE = re.compile(r"^<@!?(\d+)>$|^(\d{15,22})$")
DURATION_RE = re.compile(r"^(\d+)([smhdw])$", re.I)
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def load_infractions():
    if os.path.exists(INFRACTION_FILE):
        with open(INFRACTION_FILE) as f:
            infractions.update(json.load(f))


def save_infractions():
    with open(INFRACTION_FILE, "w") as f:
        json.dump(infractions, f)


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


async def dm_user(user, embed, view=None):
    try:
        await user.send(embed=embed, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


class AppealModal(discord.ui.Modal, title="Ban appeal"):
    reason = discord.ui.TextInput(label="Why should you be unbanned?",
                                  style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction):
        record = next((b for b in reversed(infractions["bans"])
                       if b["guild_id"] == self.guild_id and b["user_id"] == interaction.user.id), None)
        channel = client.get_channel(APPEAL_CHANNEL_ID)
        if channel is None:
            await interaction.response.send_message("❌ Appeal channel not found, please contact staff.")
            return
        embed = discord.Embed(title="Ban appeal", color=discord.Color.gold(),
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="Appeal from", value=f"<@{interaction.user.id}> ({interaction.user})", inline=False)
        embed.add_field(name="Ban reason", value=clip(record["reason"]) if record else "*(unknown)*", inline=False)
        if record:
            embed.add_field(name="Banned for", value=f"{fmt_seconds(int(time.time()) - record['ts'])} (since <t:{record['ts']}:f>)", inline=False)
        embed.add_field(name="Appeal", value=clip(self.reason.value), inline=False)
        embed.set_footer(text=f"User ID: {interaction.user.id}")
        await channel.send(embed=embed)
        await interaction.response.send_message("✅ Your appeal has been sent to the staff team.")


class AppealView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Appeal", style=discord.ButtonStyle.primary, custom_id="ban_appeal")
    async def appeal(self, interaction, button):
        footer = interaction.message.embeds[0].footer.text if interaction.message.embeds else ""
        m = re.search(r"Server ID: (\d+)", footer or "")
        await interaction.response.send_modal(AppealModal(int(m.group(1)) if m else 0))


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
    client.add_view(AppealView())
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

    needed = "ban_members" if name == "ban" else "moderate_members"
    if not need(message, needed):
        await message.channel.send("❌ You don't have permission to do that.")
        return

    tokens = args.split()
    uid = parse_user_id(tokens[0]) if tokens else None
    if uid is None:
        usage = {"mute": "s!mute @user <time> <reason>", "ban": "s!ban @user <reason>", "warn": "s!warn @user <reason>"}[name]
        await message.channel.send(f"Usage: `{usage}`")
        return
    rest = args.split(maxsplit=1)[1] if len(tokens) > 1 else ""

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
    member = guild.get_member(uid)
    try:
        user = member or await client.fetch_user(uid)
    except discord.NotFound:
        await message.channel.send("❌ User not found.")
        return
    embed = discord.Embed(title=f"You have been banned from {guild.name}", color=discord.Color.red())
    embed.add_field(name="Reason", value=clip(reason), inline=False)
    embed.add_field(name="Want to be unbanned?", value="Click **Appeal** below and explain why you should be unbanned.", inline=False)
    embed.set_footer(text=f"Server ID: {guild.id}")
    sent = await dm_user(user, embed, AppealView())
    try:
        await guild.ban(user, reason=f"{message.author}: {reason}")
    except discord.Forbidden:
        await message.channel.send("❌ I can't ban that user. Move my role above theirs and give me Ban Members.")
        return
    infractions["bans"].append({**base, "reason": reason})
    save_infractions()
    await message.channel.send(f"✅ Banned <@{uid}>." + ("" if sent else " (Couldn't DM them.)"))


@client.event
async def on_member_join(member):
    embed = discord.Embed(title="Member joined", color=discord.Color.green(),
                          description=f"{member.mention} ({member})",
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
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
        return
    msg = payload.cached_message
    if msg and (msg.author.bot):
        return
    embed = discord.Embed(title="Message deleted", color=discord.Color.red(),
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
    extra = []
    if msg:
        bot_tag = " [BOT]" if msg.author.bot else ""
        embed.add_field(name="Author", value=f"{msg.author.mention} ({msg.author}){bot_tag}")
        embed.add_field(name="Content", value=clip(msg.content), inline=False)
        if msg.attachments:
            embed.add_field(name="Attachments", value=clip("\n".join(a.url for a in msg.attachments)), inline=False)
        extra = msg.embeds
        embed.set_footer(text=f"Author ID: {msg.author.id} | Message ID: {msg.id}")
    else:
        embed.description = "The message wasn't cached, so its content is unknown."
        embed.set_footer(text=f"Message ID: {payload.message_id}")
    await send_log(payload.guild_id, "message", embed, extra)


@client.event
async def on_raw_message_edit(payload):
    if not payload.guild_id:
        return
    after = payload.message
    before = payload.cached_message
    if after.author.bot:
        return
    if before is None and after.edited_at is None:
        return  # embed-only update on an uncached message
    if before and before.content == after.content and before.embeds == after.embeds:
        return
    embed = discord.Embed(title="Message edited", color=discord.Color.blue(),
                          description=f"[Jump to message]({after.jump_url})",
                          timestamp=discord.utils.utcnow())
    bot_tag = " [BOT]" if after.author.bot else ""
    embed.add_field(name="Author", value=f"{after.author.mention} ({after.author}){bot_tag}")
    embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
    embed.add_field(name="Before", value=clip(before.content) if before else "*(not cached)*", inline=False)
    embed.add_field(name="After", value=clip(after.content), inline=False)
    embed.set_footer(text=f"Author ID: {after.author.id} | Message ID: {after.id}")
    await send_log(payload.guild_id, "message", embed, after.embeds)


client.run(token)
