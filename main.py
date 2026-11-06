import asyncio
import json
import os

import discord


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Set the DISCORD_TOKEN environment variable before starting the bot.")

PREFIX = "l!"
STICKY_FILE = "stickies.json"

intents = discord.Intents.default()
intents.message_content = True  # also enable "Message Content Intent" in the Developer Portal
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
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.guild and message.content.startswith(PREFIX + "sticky"):
        if not message.author.guild_permissions.manage_messages:
            await message.channel.send("You need the Manage Messages permission to do that.")
            return
        if not message.channel_mentions:
            await message.channel.send("Usage: `l!sticky <message> #channel`")
            return
        target = message.channel_mentions[-1]
        text = message.content[len(PREFIX + "sticky"):]
        for ch in message.channel_mentions:
            text = text.replace(ch.mention, "")
        text = text.strip()
        if not text:
            await message.channel.send("Usage: `l!sticky <message> #channel`")
            return
        stickies[target.id] = {"text": text, "last_id": None}
        await repost_sticky(target)
        return

    if message.channel.id in stickies:
        await repost_sticky(message.channel)


client.run(token)
