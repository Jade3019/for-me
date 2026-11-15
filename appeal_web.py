import asyncio
import base64
import hashlib
import html
import logging
import os
import secrets
import time
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import aiohttp
import discord
from aiohttp import web

logger = logging.getLogger(__name__)
COOKIE = "appeal_session"


def configuration():
    url = os.getenv("APPEAL_PUBLIC_URL", "").strip().rstrip("/")
    client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
    if not any((url, client_id, secret)):
        return None
    parsed = urlsplit(url)
    if (not url or not client_id.isdigit() or not secret or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or not (parsed.scheme == "https" or
                    (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")))):
        raise RuntimeError("Set APPEAL_PUBLIC_URL to an HTTPS origin and configure DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET.")
    try:
        port = int(os.getenv("APPEAL_PORT", "8080"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as error:
        raise RuntimeError("APPEAL_PORT must be a port number from 1 to 65535.") from error
    return {
        "url": url, "client_id": client_id, "secret": secret,
        "host": os.getenv("APPEAL_HOST", "127.0.0.1"), "port": port,
        "secure": parsed.scheme == "https",
    }


def page(body, status=200):
    return web.Response(
        status=status, content_type="text/html",
        text=("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
              "<meta name='viewport' content='width=device-width, initial-scale=1'>"
              "<title>Ban appeal</title><style>"
              "body{font-family:system-ui,sans-serif;background:#f4f2fa;color:#242133;margin:0;padding:24px}"
              "main{max-width:720px;margin:auto}h1{border-top:8px solid #6750a4;padding-top:20px}"
              "section{background:white;border:1px solid #dedbe7;border-radius:12px;padding:24px;margin:20px 0}"
              "textarea{box-sizing:border-box;width:100%;margin:12px 0;font:inherit;padding:12px;border:1px solid #aaa;border-radius:6px}"
              "button{background:#6750a4;color:white;border:0;border-radius:6px;padding:10px 18px;cursor:pointer}"
              "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;background:#f6f5f9;padding:12px;border-radius:6px}"
              "</style></head><body><main>"
              "<h1>Ban appeal</h1>" + body + "</main></body></html>"),
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        },
    )


async def discord_identity(config, code, verifier):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post("https://discord.com/api/v10/oauth2/token", data={
            "client_id": config["client_id"], "client_secret": config["secret"],
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": config["url"] + "/callback", "code_verifier": verifier,
        }) as response:
            response.raise_for_status()
            token = (await response.json())["access_token"]
        async with session.get("https://discord.com/api/v10/users/@me",
                               headers={"Authorization": f"Bearer {token}"}) as response:
            response.raise_for_status()
            return await response.json()


class ReplyCollector:
    def __init__(self):
        self.messages = []
        self.id = None

    async def send(self, text):
        self.messages.append(text)


class AppealWebsite:
    def __init__(self, bot, config):
        self.bot = bot
        self.config = config
        self.sessions = {}

    def get_session(self, request):
        session_id = request.cookies.get(COOKIE, "")
        session = self.sessions.get(session_id)
        if session and session["expires"] > time.monotonic():
            return session
        self.sessions.pop(session_id, None)
        return None

    def cookie(self, response, session_id):
        response.set_cookie(COOKIE, session_id, httponly=True, secure=self.config["secure"],
                            samesite="Lax", max_age=3600, path="/")

    async def login(self, request):
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value["expires"] > now}
        if len(self.sessions) >= 10000:
            raise web.HTTPServiceUnavailable(text="Too many active sessions. Please try again later.")
        session_id = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.sessions[session_id] = {
            "state": state, "verifier": verifier, "expires": now + 600,
        }
        query = urlencode({
            "client_id": self.config["client_id"], "response_type": "code", "scope": "identify",
            "redirect_uri": self.config["url"] + "/callback", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        response = web.HTTPFound("https://discord.com/oauth2/authorize?" + query,
                                 headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        self.cookie(response, session_id)
        raise response

    async def callback(self, request):
        session_id = request.cookies.get(COOKIE, "")
        session = self.sessions.pop(session_id, None)
        state = request.query.get("state", "")
        if (not session or session["expires"] <= time.monotonic()
                or not secrets.compare_digest(session["state"].encode(), state.encode())
                or not request.query.get("code")):
            return page("<p>Sign-in failed or expired. Please try again.</p><a href='/login'>Sign in</a>", 400)
        try:
            identity = await discord_identity(self.config, request.query["code"], session["verifier"])
            user_id = identity["id"]
            if not isinstance(user_id, str) or not user_id.isdigit() or identity.get("bot"):
                raise ValueError("Invalid Discord user")
            name = identity.get("global_name") or identity["username"]
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as error:
            logger.warning("Discord OAuth sign-in failed (%s)", type(error).__name__)
            return page("<p>Discord sign-in failed. Please try again.</p><a href='/login'>Sign in</a>", 502)
        new_id = secrets.token_urlsafe(32)
        self.sessions[new_id] = {
            "user_id": int(user_id), "name": name, "csrf": secrets.token_urlsafe(32),
            "expires": time.monotonic() + 3600,
        }
        response = web.HTTPFound("/", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        self.cookie(response, new_id)
        raise response

    async def index(self, request):
        session = self.get_session(request)
        if not session or "user_id" not in session:
            return page("<p>Sign in with Discord to submit an appeal or check its status.</p>"
                        "<a href='/login'>Sign in with Discord</a>")
        user_id = session["user_id"]
        latest = {}
        for ban in reversed(self.bot.infractions["bans"]):
            if ban["user_id"] == user_id:
                latest.setdefault(ban["guild_id"], ban)
        body = f"<p>Signed in as {html.escape(session['name'])}.</p>"
        if session.get("notice"):
            body += f"<p>{html.escape(session.pop('notice'))}</p>"
        for guild_id, ban in latest.items():
            guild = self.bot.client.get_guild(guild_id)
            if guild is None:
                continue
            ticket = self.bot.appeal_for_ban(user_id, guild_id, self.bot.ban_record_id(ban))
            body += f"<section><h2>{html.escape(guild.name)}</h2>"
            body += f"<p>Ban reason: {html.escape(ban['reason'])}</p>"
            if ticket:
                body += f"<p>Status: {html.escape(ticket['status'])}</p>"
                if ticket["status"] in ("approved", "declined"):
                    if ticket["status"] == "approved":
                        invite = ticket.get("invite_url")
                        if invite:
                            body += (f"<p>Server invite (single use; expires 7 days after creation): "
                                     f"<a href='{html.escape(invite, quote=True)}'>{html.escape(invite)}</a></p>")
                        else:
                            body += "<p>No invite is available. Please contact staff.</p>"
                    body += f"<pre>{html.escape(self.bot.APPEAL_APP_REMOVAL)}</pre>"
                for entry in ticket.get("history", []):
                    if entry.get("direction") in ("user", "staff"):
                        body += (f"<p><strong>{html.escape(entry['direction'].title())}:</strong></p>"
                                 f"<pre>{html.escape(entry.get('content', ''))}</pre>")
            if not ticket or ticket["status"] == "open":
                body += ("<form method='post' action='/appeal'>"
                         f"<input type='hidden' name='csrf' value='{html.escape(session['csrf'])}'>"
                         f"<input type='hidden' name='guild_id' value='{guild_id}'>"
                         "<label>Your appeal or message<br><textarea name='message' required maxlength='4000' "
                         "rows='8' cols='60'></textarea></label><br>"
                         "<button type='submit'>Send to staff</button></form>")
            body += "</section>"
        if not latest:
            body += "<p>No ban records were found for this Discord account.</p>"
        body += (f"<form method='post' action='/logout'><input type='hidden' name='csrf' "
                 f"value='{html.escape(session['csrf'])}'><button type='submit'>Sign out</button></form>")
        return page(body)

    async def form(self, request):
        session = self.get_session(request)
        if not session or "user_id" not in session:
            raise web.HTTPUnauthorized(text="Please sign in again.")
        data = await request.post()
        if not secrets.compare_digest(str(data.get("csrf", "")).encode(), session["csrf"].encode()):
            raise web.HTTPForbidden(text="Invalid or expired form. Reload and try again.")
        session["csrf"] = secrets.token_urlsafe(32)
        return session, data

    async def logout(self, request):
        await self.form(request)
        self.sessions.pop(request.cookies.get(COOKIE, ""), None)
        response = web.HTTPFound("/")
        response.del_cookie(COOKIE, path="/")
        raise response

    async def submit(self, request):
        session, data = await self.form(request)
        guild_id = str(data.get("guild_id", ""))
        text = str(data.get("message", "")).strip()
        if not guild_id.isdigit() or not text or len(text) > 4000:
            return page("<p>Enter an appeal of at most 4000 characters.</p><a href='/'>Go back</a>", 400)
        user_id = session["user_id"]
        async with self.bot.appeal_lock(user_id):
            ban = next((record for record in reversed(self.bot.infractions["bans"])
                        if record["user_id"] == user_id and record["guild_id"] == int(guild_id)), None)
            guild = self.bot.client.get_guild(int(guild_id))
            if not ban or guild is None:
                return page("<p>No matching ban record was found.</p><a href='/'>Go back</a>", 404)
            try:
                await guild.fetch_ban(discord.Object(id=user_id))
            except discord.NotFound:
                return page("<p>This ban is no longer active.</p><a href='/'>Go back</a>", 409)
            except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError) as error:
                logger.warning("Could not verify website appeal ban for %s: %s", user_id, error)
                return page("<p>Could not verify the ban. Please try again later.</p><a href='/'>Go back</a>", 503)
            user = SimpleNamespace(id=user_id, name=session["name"], global_name=session["name"])
            reply = ReplyCollector()
            message = SimpleNamespace(id=None, author=user, channel=reply, content=text, attachments=[])
            ticket = self.bot.appeal_for_ban(user_id, int(guild_id), self.bot.ban_record_id(ban))
            if ticket and ticket["status"] == "open":
                await self.bot.forward_user_message(message, ticket, text)
            elif ticket:
                await reply.send("This appeal is already closed.")
            else:
                await self.bot.create_appeal(user, reply, guild, ban, text, message, source="website")
            session["notice"] = " ".join(reply.messages) or "No response was received. Please contact staff."
        raise web.HTTPSeeOther("/")

    def app(self):
        app = web.Application(client_max_size=16 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/login", self.login)
        app.router.add_get("/callback", self.callback)
        app.router.add_post("/appeal", self.submit)
        app.router.add_post("/logout", self.logout)
        return app


async def start(bot, config):
    runner = web.AppRunner(AppealWebsite(bot, config).app(), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, config["host"], config["port"]).start()
    except OSError:
        await runner.cleanup()
        raise
    return runner
