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
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.guild and message.content.lower().startswith(("!sticky", "!memberlog", "!automodlog", "!messagelog")):
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
    embed = discord.Embed(title="Member left", color=discord.Color.red(),
                          description=f"{member.mention} ({member})",
                          timestamp=discord.utils.utcnow())
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
    if msg and msg.author == client.user:
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
    if after.author == client.user:
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
