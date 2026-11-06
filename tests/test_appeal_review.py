import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

with patch.dict(os.environ, {"DISCORD_TOKEN": "test-token"}), patch.object(discord.Client, "run"):
    main = importlib.import_module("main")


class AppealReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        embed = discord.Embed(title="Ban appeal")
        embed.set_footer(text="User ID: 123456789012345678")
        self.interaction = SimpleNamespace(
            user=SimpleNamespace(
                guild_permissions=SimpleNamespace(ban_members=True),
                mention="<@123>",
            ),
            message=SimpleNamespace(embeds=[embed]),
            guild=SimpleNamespace(name="Test server", unban=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        self.user = SimpleNamespace(send=AsyncMock())
        self.view = main.AppealReviewView()

    async def run_review(self, approved, dm_succeeds=True):
        with patch.object(main.client, "fetch_user", AsyncMock(return_value=self.user)), \
                patch.object(main, "dm_user", AsyncMock(return_value=dm_succeeds)) as dm:
            await self.view.handle(self.interaction, approved)
        return dm

    async def test_approval_unbans_and_notifies(self):
        dm = await self.run_review(True)
        self.interaction.guild.unban.assert_awaited_once()
        self.assertEqual(dm.await_args.args[1].title, "Appeal approved")
        self.assertIn("Test server", dm.await_args.args[1].description)
        self.interaction.response.defer.assert_awaited_once()
        self.interaction.followup.send.assert_not_awaited()
        self.assertTrue(all(c.disabled for c in self.interaction.edit_original_response.await_args.kwargs["view"].children))
        self.assertTrue(all(not c.disabled for c in self.view.children))

    async def test_decline_notifies_without_unbanning(self):
        dm = await self.run_review(False)
        self.interaction.guild.unban.assert_not_awaited()
        self.assertEqual(dm.await_args.args[1].title, "Appeal declined")
        self.interaction.edit_original_response.assert_awaited_once()

    async def test_failed_dm_warns_staff_for_both_results(self):
        for approved in (True, False):
            with self.subTest(approved=approved):
                self.setUp()
                await self.run_review(approved, dm_succeeds=False)
                self.assertIn("couldn't DM", self.interaction.followup.send.await_args.args[0])
                self.assertTrue(self.interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_missing_permission_does_not_notify_or_unban(self):
        self.interaction.user.guild_permissions.ban_members = False
        dm = await self.run_review(True)
        dm.assert_not_awaited()
        self.interaction.guild.unban.assert_not_awaited()
        self.interaction.response.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
