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
INVITE_SOURCE_FILE = "invitesources.json"
INFRACTION_FILE = "infractions.json"
MUTE_ROLE_ID = 1558947443444555958
APPEAL_CHANNEL_ID = 1568339091702026300
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
    for g in client.guilds:
        await snapshot_invites(g)
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

    async def on_error(self, interaction, error):
        import traceback
        traceback.print_exception(error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Something went wrong, please contact a staff member.")

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
        await channel.send(embed=embed, view=AppealReviewView())
        await interaction.response.send_message("✅ Your appeal has been sent to the staff team.")


class AppealView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Appeal", style=discord.ButtonStyle.primary, custom_id="ban_appeal")
    async def appeal(self, interaction, button):
        await interaction.response.send_modal(AppealModal(self.guild_id))

    async def on_error(self, interaction, error, item):
        import traceback
        traceback.print_exception(error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Something went wrong, please contact a staff member.")


class AppealReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def handle(self, interaction, approved):
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ You need the Ban Members permission.", ephemeral=True)
            return
        old = interaction.message.embeds[0]
        m = re.search(r"User ID: (\d+)", old.footer.text or "")
        if not m:
            await interaction.response.send_message("❌ Couldn't find the user in this appeal.", ephemeral=True)
            return
        uid = int(m.group(1))
        await interaction.response.defer()
        try:
            user = await client.fetch_user(uid)
        except discord.HTTPException:
            await interaction.followup.send("❌ Couldn't find the user. Please try again.", ephemeral=True)
            return
        if approved:
            try:
                await interaction.guild.unban(user, reason=f"Appeal approved by {interaction.user}")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                await interaction.followup.send("❌ I can't unban that user (missing Ban Members).", ephemeral=True)
                return
            except discord.HTTPException:
                await interaction.followup.send("❌ Couldn't unban that user. Please try again.", ephemeral=True)
                return
        result = "approved" if approved else "declined"
        dm = discord.Embed(title=f"Appeal {result}",
                           color=discord.Color.green() if approved else discord.Color.red(),
                           description=f"Your ban appeal for {interaction.guild.name} has been {result}.")
        sent = await dm_user(user, dm)
        embed = old.copy()
        embed.color = discord.Color.green() if approved else discord.Color.red()
        embed.add_field(name="Result", value=f"{'Approved' if approved else 'Declined'} by {interaction.user.mention}", inline=False)
        disabled_view = AppealReviewView()
        for child in disabled_view.children:
            child.disabled = True
        await interaction.edit_original_response(embed=embed, view=disabled_view)
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
    print("setup_hook: registering appeal views")
    client.add_view(AppealView(0))
    client.add_view(AppealReviewView())
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
    try:
        user = member or await client.fetch_user(uid)
    except discord.NotFound:
        await message.channel.send("❌ User not found.")
        return
    
    if member and message.author.top_role <= member.top_role:
        await message.channel.send("❌ You can't ban a member with a role equal to or higher than yours.")
        return
    
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
    
    embed = discord.Embed(title=f"You have been banned from {guild.name}", color=discord.Color.red())
    embed.add_field(name="Reason", value=clip(reason), inline=False)
    embed.add_field(name="Want to be unbanned?", value="Click **Appeal** below and explain why you should be unbanned.", inline=False)
    embed.set_footer(text=f"Server ID: {guild.id}")
    sent = await dm_user(user, embed, AppealView(guild.id))
    try:
        await guild.ban(user, reason=f"{message.author}: {reason}")
    except discord.Forbidden:
        await message.channel.send("❌ I can't ban that user. Move my role above theirs and give me Ban Members.")
        return
    infractions["bans"].append({**base, "reason": reason})
    save_infractions()
    if is_hacked:
        await message.channel.send(f"✅ Banned <@{uid}> and deleted {deleted_count} messages." + ("" if sent else " (Couldn't DM them.)"))
    else:
        await message.channel.send(f"✅ Banned <@{uid}>." + ("" if sent else " (Couldn't DM them.)"))


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
