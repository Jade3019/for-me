import importlib
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

with patch.dict(os.environ, {"DISCORD_TOKEN": "test-token"}), patch.object(discord.Client, "run"):
    main = importlib.import_module("main")


class JoinAttributionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.invite_cache.clear()
        main.invite_sources.clear()
        main.vanity_uses.clear()
        self.creator = SimpleNamespace(mention="<@123>")
        self.channel = SimpleNamespace(mention="<#456>")
        self.invite = SimpleNamespace(code="abc123", uses=1, inviter=self.creator, channel=self.channel)
        self.guild = SimpleNamespace(
            id=789,
            invites=AsyncMock(return_value=[self.invite]),
            features=[],
        )
        self.member = SimpleNamespace(guild=self.guild, bot=False)

    async def test_reports_registered_website_and_invite_details(self):
        main.invite_cache[self.guild.id] = {"abc123": (0, self.creator, self.invite)}
        main.invite_sources[self.guild.id] = {"abc123": "https://example.com/community"}

        result = await main.detect_join_method(self.member)

        self.assertIn("Website/source: https://example.com/community", result)
        self.assertIn("https://discord.gg/abc123", result)
        self.assertIn("Created by:", result)
        self.assertIn("Invite channel: <#456>", result)

    async def test_explains_that_unregistered_website_cannot_be_inferred(self):
        main.invite_cache[self.guild.id] = {"abc123": (0, self.creator, self.invite)}

        result = await main.detect_join_method(self.member)

        self.assertIn("Website/source: unknown", result)
        self.assertIn("Discord does not share the referring webpage", result)
        self.assertIn("https://discord.gg/abc123", result)

    def test_source_labels_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "invitesources.json")
            with patch.object(main, "INVITE_SOURCE_FILE", path):
                main.invite_sources[self.guild.id] = {"abc123": "Example"}
                main.save_invite_sources()
                main.invite_sources.clear()

                main.load_invite_sources()

            with open(path) as source_file:
                saved = json.load(source_file)
            self.assertEqual(main.invite_sources, {self.guild.id: {"abc123": "Example"}})
            self.assertEqual(saved, {str(self.guild.id): {"abc123": "Example"}})

    async def test_command_registers_a_source_from_an_invite_link(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "invitesources.json")
            message = SimpleNamespace(
                author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
                channel=SimpleNamespace(send=AsyncMock()),
                content="s!invitesource https://discord.gg/abc123?ref=profile https://example.com/community",
                guild=SimpleNamespace(id=self.guild.id),
            )
            with patch.object(main, "INVITE_SOURCE_FILE", path):
                await main.on_message(message)

            self.assertEqual(
                main.invite_sources[self.guild.id],
                {"abc123": "https://example.com/community"},
            )
            message.channel.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
