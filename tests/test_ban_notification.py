import importlib
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

        async def ban(user, *, reason):
            self.events.append(("ban", user))

        self.user = SimpleNamespace(id=self.user_id, top_role=1, send=AsyncMock(side_effect=send_dm))
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

        self.assertEqual([event[0] for event in self.events], ["dm", "ban"])
        embed = self.events[0][1]
        self.assertIn("Test server", embed.title)
        self.assertIn("spam", embed.fields[0].value)
        self.assertIn("DM this bot", embed.fields[1].value)
        self.assertIn(str(self.guild.id), embed.fields[1].value)
        view = self.user.send.await_args.kwargs["view"]
        self.assertEqual(view.children[0].label, "Appeal Ban")
        self.assertIn("Ban ID:", embed.footer.text)

    async def test_failed_dm_does_not_prevent_ban_and_warns_staff(self):
        self.user.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "DMs closed"
        )
        await self.run_ban()

        self.guild.ban.assert_awaited_once()
        self.assertIn("couldn't DM", self.message.channel.send.await_args.args[0])

    async def test_failed_ban_notice_does_not_claim_ban_succeeded(self):
        self.guild.ban.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing permissions"
        )
        await self.run_ban()

        embed = self.user.send.await_args.kwargs["embed"]
        self.assertIn("If the ban succeeds", embed.fields[1].value)
        self.assertIn("ban was attempted", self.message.channel.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
