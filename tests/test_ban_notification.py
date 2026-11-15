import importlib
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

with patch.dict(os.environ, {
    "DISCORD_TOKEN": "test-token",
    "modmail_channel_id": "456",
    "role_ids": "111,222",
}), patch.object(discord.Client, "run"):
    main = importlib.import_module("main")


class BanNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user_id = 123456789012345678
        self.events = []

        async def send_dm(**kwargs):
            self.events.append(("dm", kwargs["embed"]))

        async def create_dm():
            self.events.append(("create_dm", self.user_id))
            return self.dm_channel

        async def ban(user, *, reason):
            self.events.append(("ban", user))

        self.dm_channel = SimpleNamespace(send=AsyncMock(side_effect=send_dm))
        self.user = SimpleNamespace(
            id=self.user_id, top_role=1, create_dm=AsyncMock(side_effect=create_dm)
        )
        self.guild = SimpleNamespace(
            id=123,
            name="Test server",
            get_member=lambda uid: self.user,
            ban=AsyncMock(side_effect=ban),
        )
        self.message = SimpleNamespace(
            guild=self.guild,
            author=SimpleNamespace(
                id=987,
                top_role=2,
                guild_permissions=SimpleNamespace(ban_members=True),
            ),
            channel=SimpleNamespace(send=AsyncMock()),
        )

    async def run_ban(self):
        with patch.object(main.client, "_connection") as connection, \
                patch.object(main, "save_infractions"), \
                patch.object(main, "send_log", AsyncMock()):
            connection.user = SimpleNamespace(id=999)
            await main.run_mod_command(self.message, "ban", f"{self.user_id} spam")

    async def test_dm_is_sent_before_ban_with_appeal_instructions(self):
        await self.run_ban()

        self.assertEqual([event[0] for event in self.events], ["create_dm", "dm", "ban"])
        embed = self.events[1][1]
        self.assertIn("Test server", embed.title)
        self.assertIn("spam", embed.fields[0].value)
        self.assertIn("DM this bot", embed.fields[1].value)
        self.assertIn(str(self.guild.id), embed.fields[1].value)
        install = next(field for field in embed.fields if field.name == "Add the app to your account")
        self.assertIn("https://discord.com/oauth2/authorize?client_id=1568321176034615327", install.value)
        self.assertIn("Add to my apps", install.value)
        self.assertIn("may still restrict DMs", install.value)
        view = self.dm_channel.send.await_args.kwargs["view"]
        self.assertEqual(view.children[0].label, "Appeal Ban")
        self.assertIn("Ban ID:", embed.footer.text)

    async def test_failed_dm_does_not_prevent_ban_and_warns_staff(self):
        self.dm_channel.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50007, "message": "Cannot send messages to this user"},
        )
        with self.assertLogs(main.logger, level="WARNING") as logs:
            await self.run_ban()

        self.guild.ban.assert_awaited_once()
        self.assertIn("couldn't DM", self.message.channel.send.await_args.args[0])
        self.assertIn("50007", self.message.channel.send.await_args.args[0])
        self.assertIn("Cannot send messages to this user", logs.output[0])
        self.assertIn(str(self.user_id), logs.output[0])

    async def test_other_discord_errors_are_logged_and_reported(self):
        self.user.create_dm.side_effect = discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"),
            {"code": 50035, "message": "Invalid Form Body"},
        )
        with self.assertLogs(main.logger, level="WARNING") as logs:
            await self.run_ban()
        self.guild.ban.assert_awaited_once()
        self.assertIn("50035", self.message.channel.send.await_args.args[0])
        self.assertIn("Invalid Form Body", logs.output[0])
        self.dm_channel.send.assert_not_awaited()

    async def test_nonmember_is_fetched_and_sent_dm_before_ban(self):
        self.guild.get_member = lambda uid: None
        with patch.object(main.client, "fetch_user", AsyncMock(return_value=self.user)) as fetch:
            await self.run_ban()
        fetch.assert_awaited_once_with(self.user_id)
        self.assertEqual([event[0] for event in self.events], ["create_dm", "dm", "ban"])

    async def test_fetch_failure_is_logged_and_does_not_prevent_ban(self):
        self.guild.get_member = lambda uid: None
        error = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"),
            {"code": 0, "message": "Service unavailable"},
        )
        with patch.object(main.client, "fetch_user", AsyncMock(side_effect=error)), \
                self.assertLogs(main.logger, level="WARNING") as logs:
            await self.run_ban()
        self.assertEqual(self.guild.ban.await_args.args[0].id, self.user_id)
        self.assertIn("couldn't fetch", self.message.channel.send.await_args.args[0])
        self.assertIn("HTTP 503", self.message.channel.send.await_args.args[0])
        self.assertIn("Service unavailable", logs.output[0])

    async def test_network_failure_does_not_prevent_ban(self):
        self.dm_channel.send.side_effect = asyncio.TimeoutError()
        with self.assertLogs(main.logger, level="WARNING") as logs:
            await self.run_ban()
        self.guild.ban.assert_awaited_once()
        self.assertIn("network error", self.message.channel.send.await_args.args[0])
        self.assertIn("TimeoutError", logs.output[0])

    async def test_failed_ban_notice_does_not_claim_ban_succeeded(self):
        self.guild.ban.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing permissions"
        )
        await self.run_ban()

        embed = self.dm_channel.send.await_args.kwargs["embed"]
        self.assertIn("If the ban succeeds", embed.fields[1].value)
        self.assertIn("ban was attempted", self.message.channel.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
