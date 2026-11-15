import importlib
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
import discord
from aiohttp.test_utils import TestClient, TestServer

with patch.dict(os.environ, {
    "DISCORD_TOKEN": "test-token", "modmail_channel_id": "456", "role_ids": "111,222",
}, clear=False), patch.object(discord.Client, "run"):
    main = importlib.import_module("main")

import appeal_web


class AppealWebsiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.appeals.clear()
        main.infractions["bans"] = [{
            "id": "a" * 32, "guild_id": 123, "user_id": 789, "reason": "spam",
        }]
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(main, "APPEALS_FILE", os.path.join(self.directory.name, "appeals.json"))
        self.path_patch.start()
        self.guild = SimpleNamespace(id=123, name="Test server", fetch_ban=AsyncMock())
        self.thread = SimpleNamespace(id=654, send=AsyncMock(), edit=AsyncMock())
        self.starter = SimpleNamespace(id=555, create_thread=AsyncMock(return_value=self.thread))
        self.channel = SimpleNamespace(send=AsyncMock(return_value=self.starter))
        self.guild_patch = patch.object(main.client, "get_guild", return_value=self.guild)
        self.channel_patch = patch.object(main.client, "get_channel", side_effect=lambda cid:
                                          self.thread if cid == 654 else self.channel)
        self.guild_patch.start()
        self.channel_patch.start()
        config = {
            "url": "http://localhost:8080", "client_id": "123", "secret": "test",
            "secure": False, "host": "127.0.0.1", "port": 8080,
        }
        self.website = appeal_web.AppealWebsite(main, config)
        self.client = TestClient(TestServer(self.website.app()), cookie_jar=aiohttp.CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.channel_patch.stop()
        self.guild_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    async def sign_in(self):
        response = await self.client.get("/login", allow_redirects=False)
        params = parse_qs(urlsplit(response.headers["Location"]).query)
        self.assertEqual(params["scope"], ["identify"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        with patch.object(appeal_web, "discord_identity",
                          AsyncMock(return_value={"id": "789", "username": "user"})):
            response = await self.client.get("/callback", params={"state": params["state"][0], "code": "code"},
                                             allow_redirects=False)
        self.assertEqual(response.status, 302)

    async def csrf(self):
        response = await self.client.get("/")
        body = await response.text()
        return re.search(r"name='csrf' value='([^']+)'", body).group(1), body

    async def test_signin_submit_reply_and_restart(self):
        await self.sign_in()
        csrf, body = await self.csrf()
        self.assertIn("Test server", body)
        response = await self.client.post("/appeal", data={
            "csrf": csrf, "guild_id": "123", "message": "Please review my ban"
        }, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.guild.fetch_ban.assert_awaited_once()
        self.assertEqual(main.appeals[654]["source"], "website")
        self.assertEqual(main.appeals[654]["history"][0]["content"], "Please review my ban")
        self.assertIn("appeal website", self.thread.send.await_args.args[0])

        response = await self.client.post("/appeal", data={
            "csrf": csrf, "guild_id": "123", "message": "duplicate"
        }, allow_redirects=False)
        self.assertEqual(response.status, 403)
        self.channel.send.assert_awaited_once()

        staff = SimpleNamespace(
            id=1002, content="We are reviewing this.", attachments=[], channel=self.thread,
            author=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(ban_members=True)),
        )
        with patch.object(main.client, "fetch_user", AsyncMock()) as fetch:
            await main.handle_appeal_staff_message(staff, main.appeals[654])
        fetch.assert_not_awaited()
        main.appeals.clear()
        main.load_appeals()
        await self.client.close()
        self.website = appeal_web.AppealWebsite(main, self.website.config)
        self.client = TestClient(TestServer(self.website.app()), cookie_jar=aiohttp.CookieJar(unsafe=True))
        await self.client.start_server()
        await self.sign_in()
        csrf, body = await self.csrf()
        self.assertIn("We are reviewing this.", body)
        self.assertIn("Status: open", body)
        response = await self.client.post("/appeal", data={
            "csrf": csrf, "guild_id": "123", "message": "More evidence"
        }, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertIn("More evidence", self.thread.send.await_args.args[0])
        self.channel.send.assert_awaited_once()

    async def test_oauth_state_and_unauthenticated_submission_rejected(self):
        response = await self.client.post("/appeal", data={"guild_id": "123", "message": "bad"})
        self.assertEqual(response.status, 401)
        await self.client.get("/login", allow_redirects=False)
        with patch.object(appeal_web, "discord_identity", AsyncMock()) as identity:
            response = await self.client.get("/callback", params={"state": "wrong", "code": "code"})
        self.assertEqual(response.status, 400)
        identity.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_cannot_submit_for_another_user_or_inactive_ban(self):
        await self.sign_in()
        csrf, _ = await self.csrf()
        response = await self.client.post("/appeal", data={
            "csrf": csrf, "guild_id": "999", "message": "bad"
        })
        self.assertEqual(response.status, 404)
        csrf, _ = await self.csrf()
        self.guild.fetch_ban.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not found"), "Not banned"
        )
        response = await self.client.post("/appeal", data={
            "csrf": csrf, "guild_id": "123", "message": "bad"
        })
        self.assertEqual(response.status, 409)
        self.channel.send.assert_not_awaited()

    async def test_html_escapes_user_supplied_history(self):
        main.appeals[654] = {
            "user_id": 789, "guild_id": 123, "ban_id": "a" * 32, "status": "open",
            "history": [{"direction": "staff", "content": "<script>alert(1)</script>"}],
        }
        await self.sign_in()
        _, body = await self.csrf()
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)

    async def test_review_result_is_visible_without_dm(self):
        await self.sign_in()
        csrf, _ = await self.csrf()
        await self.client.post("/appeal", data={
            "csrf": csrf, "guild_id": "123", "message": "Please review"
        })
        self.guild.unban = AsyncMock()
        embed = self.channel.send.await_args.kwargs["embed"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42, mention="<@42>",
                                 guild_permissions=SimpleNamespace(ban_members=True)),
            message=SimpleNamespace(id=555, embeds=[embed]),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        with patch.object(main.client, "fetch_user", AsyncMock(return_value=SimpleNamespace(id=789))), \
                patch.object(main, "dm_user", AsyncMock()) as dm:
            await main.AppealReviewView().handle(interaction, True)
        dm.assert_not_awaited()
        self.guild.unban.assert_awaited_once()
        _, body = await self.csrf()
        self.assertIn("Status: approved", body)
        self.assertIn("PC:", body)
        self.assertIn("Mobile:", body)
        self.assertIn("Deauthorize", body)

    async def test_discord_and_staff_creation_failures_are_reported(self):
        await self.sign_in()
        csrf, _ = await self.csrf()
        self.channel.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50013, "message": "Missing permissions"},
        )
        with self.assertLogs(main.logger, level="WARNING"):
            response = await self.client.post("/appeal", data={
                "csrf": csrf, "guild_id": "123", "message": "Please review"
            })
        self.assertEqual(response.status, 200)
        self.assertIn("couldn&#x27;t create your modmail thread", await response.text())
        self.assertFalse(main.appeals)

    async def test_form_validates_full_configuration(self):
        with patch.dict(os.environ, {
            "APPEAL_PUBLIC_URL": "http://example.com", "DISCORD_CLIENT_ID": "123",
            "DISCORD_CLIENT_SECRET": "test",
        }):
            with self.assertRaises(RuntimeError):
                appeal_web.configuration()

    async def test_staff_can_share_form_link(self):
        message = SimpleNamespace(
            guild=SimpleNamespace(id=123),
            content="s!appealform",
            channel=SimpleNamespace(send=AsyncMock()),
            author=SimpleNamespace(id=42, bot=False),
        )
        with patch.object(main, "APPEAL_WEB_CONFIG", {"url": "https://appeals.example.com"}), \
                patch.object(main.client, "_connection") as connection:
            connection.user = SimpleNamespace(id=999)
            await main.on_message(message)
        self.assertIn("https://appeals.example.com", message.channel.send.await_args.args[0])

    async def test_oauth_exchanges_code_and_fetches_identity(self):
        token_response = SimpleNamespace(raise_for_status=lambda: None,
                                         json=AsyncMock(return_value={"access_token": "token"}))
        user_response = SimpleNamespace(raise_for_status=lambda: None,
                                        json=AsyncMock(return_value={"id": "789", "username": "user"}))
        post_context = AsyncMock()
        post_context.__aenter__.return_value = token_response
        get_context = AsyncMock()
        get_context.__aenter__.return_value = user_response
        session = SimpleNamespace(post=Mock(return_value=post_context), get=Mock(return_value=get_context))
        session_context = AsyncMock()
        session_context.__aenter__.return_value = session
        with patch.object(appeal_web.aiohttp, "ClientSession", return_value=session_context):
            identity = await appeal_web.discord_identity(self.website.config, "code", "verifier")
        self.assertEqual(identity["id"], "789")
        self.assertEqual(session.post.call_args.kwargs["data"]["code_verifier"], "verifier")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "Bearer token")
        token_response.json.assert_awaited_once()
        user_response.json.assert_awaited_once()
