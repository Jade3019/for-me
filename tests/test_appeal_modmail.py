import asyncio
import importlib
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

with patch.dict(os.environ, {
    "DISCORD_TOKEN": "test-token",
    "modmail_channel_id": "456",
    "role_ids": "111,222",
}), \
        patch.object(discord.Client, "run"):
    main = importlib.import_module("main")


class AppealModmailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.appeals.clear()
        main.infractions["bans"] = [{
            "guild_id": 123,
            "user_id": 789,
            "reason": "spam",
            "ts": 100,
        }]
        self.dm_channel = SimpleNamespace(send=AsyncMock())
        self.user = SimpleNamespace(
            id=789,
            name="user",
            global_name="Global User",
            send=AsyncMock(),
        )
        self.thread = SimpleNamespace(
            id=654,
            send=AsyncMock(),
            edit=AsyncMock(),
        )
        self.starter = SimpleNamespace(create_thread=AsyncMock(return_value=self.thread))
        self.modmail_channel = SimpleNamespace(send=AsyncMock(return_value=self.starter))
        self.guild = SimpleNamespace(
            id=123,
            name="Test server",
            fetch_ban=AsyncMock(),
        )
        self.message = SimpleNamespace(
            id=1001,
            content="I believe this ban was a mistake.",
            author=self.user,
            channel=self.dm_channel,
            attachments=[],
        )

    async def test_dm_opens_thread_with_requested_modmail_instructions(self):
        with patch.object(main.client, "get_guild", return_value=self.guild), \
                patch.object(main.client, "get_channel", return_value=self.modmail_channel), \
                patch.object(main, "save_appeals") as save:
            await main.handle_appeal_dm(self.message)

        self.guild.fetch_ban.assert_awaited_once()
        intro = self.thread.send.await_args.args[0]
        staff_notice = self.modmail_channel.send.await_args.kwargs
        self.assertEqual(staff_notice["content"], "<@&111> <@&222>")
        self.assertEqual(
            [role.id for role in staff_notice["allowed_mentions"].roles],
            [111, 222],
        )
        self.assertIn("New modmail from **Global User** (`789`).", intro)
        self.assertIn("`//` for an internal note that is NOT sent", intro)
        self.assertIn("`!close` to close this.", intro)
        self.thread.send.assert_awaited_once()
        self.assertEqual(main.appeals[654]["status"], "open")
        self.assertEqual(save.call_count, 2)

    async def test_user_reply_is_forwarded_to_existing_thread(self):
        main.appeals[654] = {
            "thread_id": 654,
            "user_id": 789,
            "guild_id": 123,
            "status": "open",
        }
        self.message.content = "More context"
        with patch.object(main.client, "get_channel", return_value=self.thread), \
                patch.object(main, "save_appeals"):
            await main.handle_appeal_dm(self.message)

        self.thread.send.assert_awaited_once()
        self.assertIn("More context", self.thread.send.await_args.args[0])

    async def test_on_message_routes_banned_user_dm_without_guild_membership(self):
        main.appeals[654] = {
            "thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open"
        }
        self.message.guild = None
        self.message.content = "I am still banned, please review this."
        with patch.object(main.client, "_connection") as connection, \
                patch.object(main.client, "get_channel", return_value=self.thread), \
                patch.object(main, "save_appeals"):
            connection.user = SimpleNamespace(id=999)
            await main.on_message(self.message)
        self.thread.send.assert_awaited_once()
        self.assertIn(self.message.content, self.thread.send.await_args.args[0])

    async def test_on_message_opens_appeal_for_banned_nonmember(self):
        self.message.guild = None
        with patch.object(main.client, "_connection") as connection, \
                patch.object(main.client, "get_guild", return_value=self.guild), \
                patch.object(main.client, "get_channel", return_value=self.modmail_channel), \
                patch.object(main, "save_appeals"):
            connection.user = SimpleNamespace(id=999)
            await main.on_message(self.message)
        self.assertEqual(self.guild.fetch_ban.await_args.args[0].id, self.user.id)
        self.assertEqual(main.appeals[654]["user_id"], self.user.id)

    async def test_multiple_active_bans_require_a_server_id(self):
        main.infractions["bans"].append({
            "guild_id": 321,
            "user_id": 789,
            "reason": "spam",
            "ts": 200,
        })
        self.message.content = "Please review my ban."
        second_guild = SimpleNamespace(
            id=321,
            name="Second server",
            fetch_ban=AsyncMock(),
        )
        guilds = {123: self.guild, 321: second_guild}

        with patch.object(main.client, "get_guild", side_effect=guilds.get):
            await main.handle_appeal_dm(self.message)

        self.dm_channel.send.assert_awaited_once()
        self.assertIn("123", self.dm_channel.send.await_args.args[0])
        self.modmail_channel.send.assert_not_awaited()

    def test_open_thread_records_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "appeals.json")
            with patch.object(main, "APPEALS_FILE", path):
                main.appeals[654] = {
                    "thread_id": 654,
                    "user_id": 789,
                    "guild_id": 123,
                    "status": "open",
                }
                main.save_appeals()
                main.appeals.clear()
                main.load_appeals()

            with open(path) as appeals_file:
                saved = json.load(appeals_file)
            self.assertEqual(main.appeals[654]["status"], "open")
            self.assertIn("654", saved)

    async def test_internal_note_is_not_sent_to_user(self):
        main.appeals[654] = {
            "thread_id": 654,
            "user_id": 789,
            "guild_id": 123,
            "status": "open",
        }
        staff_message = SimpleNamespace(
            content="// check the ban logs",
            author=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(ban_members=True)),
            channel=self.thread,
            attachments=[],
        )
        with patch.object(main.client, "fetch_user", AsyncMock(return_value=self.user)):
            await main.handle_appeal_staff_message(staff_message, main.appeals[654])

        self.user.send.assert_not_awaited()

    async def test_staff_reply_is_sent_to_user_and_close_archives_thread(self):
        main.appeals[654] = {
            "thread_id": 654,
            "user_id": 789,
            "guild_id": 123,
            "status": "open",
        }
        staff_message = SimpleNamespace(
            content="We reviewed your appeal.",
            id=1002,
            author=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(ban_members=True)),
            channel=self.thread,
            attachments=[],
        )
        with patch.object(main.client, "fetch_user", AsyncMock(return_value=self.user)), \
                patch.object(main, "save_appeals"):
            await main.handle_appeal_staff_message(staff_message, main.appeals[654])
            self.assertIn("We reviewed your appeal.", self.user.send.await_args.args[0])

            staff_message.content = "!close"
            with patch.object(main, "save_appeals") as save:
                await main.handle_appeal_staff_message(staff_message, main.appeals[654])

        self.assertEqual(main.appeals[654]["status"], "closed")
        self.thread.edit.assert_awaited_once_with(archived=True, locked=True)
        save.assert_called_once()

    async def test_button_opens_one_appeal_and_rejects_another_user(self):
        ban = main.infractions["bans"][0]
        ban["id"] = "a" * 32
        notice = discord.Embed()
        notice.set_footer(text=f"Server ID: 123 | Ban ID: {ban['id']}")
        interaction = SimpleNamespace(
            guild=None, user=self.user, message=SimpleNamespace(embeds=[notice]),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.starter.id = 555
        view = main.AppealStartView()
        with patch.object(main.client, "get_guild", return_value=self.guild), \
                patch.object(main.client, "get_channel", return_value=self.modmail_channel), \
                patch.object(main, "save_appeals"):
            await asyncio.gather(view.start.callback(interaction), view.start.callback(interaction))
            self.assertEqual(self.modmail_channel.send.await_count, 1)
            self.assertEqual(main.appeals[654]["ban_id"], ban["id"])
            interaction.user = SimpleNamespace(id=999)
            await view.start.callback(interaction)
        self.assertIn("no longer valid", interaction.followup.send.await_args.args[0])

    async def test_message_deduplication_and_history_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "appeals.json")
            main.appeals[654] = {
                "thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open"
            }
            self.message.content = "123 is the message, not a server ID"
            with patch.object(main, "APPEALS_FILE", path), \
                    patch.object(main.client, "get_channel", return_value=self.thread):
                await asyncio.gather(main.handle_appeal_dm(self.message), main.handle_appeal_dm(self.message))
                main.appeals.clear()
                main.load_appeals()
                await main.handle_appeal_dm(self.message)
            self.thread.send.assert_awaited_once()
            self.assertIn(self.message.content, self.thread.send.await_args.args[0])
            self.assertEqual(main.appeals[654]["history"][0]["content"], self.message.content)

    async def test_long_messages_are_not_truncated(self):
        main.appeals[654] = {
            "thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open"
        }
        self.message.content = "x" * 2000
        with patch.object(main.client, "get_channel", return_value=self.thread), \
                patch.object(main, "save_appeals"):
            await main.handle_appeal_dm(self.message)
        forwarded = "".join(call.args[0] for call in self.thread.send.await_args_list)
        self.assertEqual(forwarded, "**Global User:**\n" + self.message.content)

    async def test_staff_reply_deduplication_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "appeals.json")
            main.appeals[654] = {
                "thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open"
            }
            message = SimpleNamespace(
                id=1002, content="Staff response", channel=self.thread, attachments=[],
                author=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(ban_members=True))
            )
            with patch.object(main, "APPEALS_FILE", path), \
                    patch.object(main.client, "fetch_user", AsyncMock(return_value=self.user)):
                await main.handle_appeal_staff_message(message, main.appeals[654])
                main.appeals.clear()
                main.load_appeals()
                await main.handle_appeal_staff_message(message, main.appeals[654])
            self.user.send.assert_awaited_once()
            self.assertEqual(main.appeals[654]["history"][0]["direction"], "staff")

    async def test_failed_staff_dm_is_reported_and_recorded(self):
        ticket = {"thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open"}
        message = SimpleNamespace(
            id=1002, content="Staff response", channel=self.thread, attachments=[],
            author=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(ban_members=True))
        )
        self.user.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "DMs closed"
        )
        with patch.object(main.client, "fetch_user", AsyncMock(return_value=self.user)), \
                patch.object(main, "save_appeals"):
            await main.handle_appeal_staff_message(message, ticket)
        self.assertIn("couldn't DM", self.thread.send.await_args.args[0])
        self.assertEqual(ticket["history"][0]["delivery_status"], "failed")

    async def test_50278_retains_conversation_and_survives_restart(self):
        error = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50278, "message": "Cannot send messages to this user"},
        )
        self.message.guild = None
        self.user.send.side_effect = error
        staff_message = SimpleNamespace(
            id=1002, content="We are reviewing your appeal.", channel=self.thread, attachments=[],
            author=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(ban_members=True))
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "appeals.json")
            with patch.object(main, "APPEALS_FILE", path), \
                    patch.object(main.client, "_connection") as connection, \
                    patch.object(main.client, "get_guild", return_value=self.guild), \
                    patch.object(main.client, "get_channel", return_value=self.modmail_channel), \
                    patch.object(main.client, "fetch_user", AsyncMock(return_value=self.user)):
                connection.user = SimpleNamespace(id=999)
                await main.on_message(self.message)
                ticket = main.appeals[654]
                self.assertEqual(ticket["ban_reason"], "spam")
                self.assertEqual(ticket["appeal_id"], "654")
                self.assertTrue(ticket["created_at"])
                with self.assertLogs(main.logger, level="WARNING") as logs:
                    await main.handle_appeal_staff_message(staff_message, ticket)
                self.assertIn("50278", logs.output[0])
                self.assertIn("50278", self.thread.send.await_args.args[0])
                self.assertEqual(ticket["history"][1]["delivery_status"], "failed")
                self.assertEqual(ticket["history"][1]["delivery_error"]["code"], 50278)
                self.user.send.side_effect = None
                self.message.id = 1003
                self.message.content = "One more message"
                with patch.object(main.client, "get_channel", return_value=self.thread):
                    await main.on_message(self.message)
                    main.appeals.clear()
                    main.load_appeals()
                    self.message.id = 1004
                    self.message.content = "After restart"
                    await main.on_message(self.message)
                self.assertEqual(main.appeals[654]["status"], "open")
                self.assertEqual([entry["content"] for entry in main.appeals[654]["history"]],
                                 ["I believe this ban was a mistake.",
                                  "We are reviewing your appeal.", "One more message", "After restart"])

    async def test_claim_unassign_and_closed_reply(self):
        ticket = {"thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open"}
        message = SimpleNamespace(
            id=1002, content="!claim", channel=self.thread, attachments=[],
            author=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(ban_members=True))
        )
        with patch.object(main, "save_appeals"), \
                patch.object(main.client, "fetch_user", AsyncMock()) as fetch:
            await main.handle_appeal_staff_message(message, ticket)
            self.assertEqual(ticket["assigned_moderator_id"], 42)
            message.content = "!unassign"
            await main.handle_appeal_staff_message(message, ticket)
            self.assertIsNone(ticket["assigned_moderator_id"])
            ticket["status"] = "closed"
            message.content = "Hello"
            await main.handle_appeal_staff_message(message, ticket)
        self.assertIn("closed", self.thread.send.await_args.args[0])
        fetch.assert_not_awaited()

    async def test_deleted_user_dm_is_recorded_and_reported(self):
        ticket = {
            "thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open",
            "history": [{"message_id": 1001, "direction": "user", "content": "original"}],
        }
        main.appeals[654] = ticket
        payload = SimpleNamespace(guild_id=None, message_id=1001)
        with patch.object(main, "save_appeals"), \
                patch.object(main.client, "get_channel", return_value=self.thread):
            await main.on_raw_message_delete(payload)
            await main.on_raw_message_delete(payload)
        self.assertIn("deleted_at", ticket["history"][0])
        self.assertEqual(ticket["history"][0]["content"], "original")
        self.thread.send.assert_awaited_once()

    async def test_attachments_are_preserved_as_links(self):
        ticket = {"thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open"}
        main.appeals[654] = ticket
        self.message.content = "Evidence"
        self.message.attachments = [SimpleNamespace(url="https://cdn.discord.test/file")]
        with patch.object(main.client, "get_channel", return_value=self.thread), \
                patch.object(main, "save_appeals"):
            await main.handle_appeal_dm(self.message)
        self.assertIn("https://cdn.discord.test/file", self.thread.send.await_args.args[0])
        self.assertIn("https://cdn.discord.test/file", ticket["history"][0]["content"])

    async def test_incoming_dm_is_recorded_if_acknowledgement_is_rejected(self):
        error = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50278, "message": "Cannot send messages to this user"},
        )
        self.dm_channel.send.side_effect = error
        self.message.guild = None
        with patch.object(main.client, "_connection") as connection, \
                patch.object(main.client, "get_guild", return_value=self.guild), \
                patch.object(main.client, "get_channel", return_value=self.modmail_channel), \
                patch.object(main, "save_appeals"), \
                self.assertLogs(main.logger, level="WARNING") as logs:
            connection.user = SimpleNamespace(id=999)
            await main.on_message(self.message)
        self.assertIn("50278", logs.output[0])
        self.assertEqual(main.appeals[654]["status"], "open")
        self.assertEqual(main.appeals[654]["history"][0]["delivery_status"], "sent")

    def test_legacy_record_migrates_without_fabricating_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "appeals.json")
            with open(path, "w") as appeals_file:
                json.dump({"654": {
                    "thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open"
                }}, appeals_file)
            with patch.object(main, "APPEALS_FILE", path):
                main.load_appeals()
                with open(path) as appeals_file:
                    ticket = json.load(appeals_file)["654"]
        self.assertEqual(ticket["ban_reason"], "spam")
        self.assertEqual(ticket["appeal_id"], "654")
        self.assertIsNone(ticket["created_at"])
        self.assertIsNone(ticket["assigned_moderator_id"])

    async def test_outbound_attachment_url_and_formatting_are_retained(self):
        ticket = {"thread_id": 654, "user_id": 789, "guild_id": 123, "status": "open"}
        message = SimpleNamespace(
            id=1002, content="**Evidence**\n  indented", channel=self.thread,
            attachments=[SimpleNamespace(url="https://cdn.discord.test/file")],
            author=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(ban_members=True))
        )
        with patch.object(main.client, "fetch_user", AsyncMock(return_value=self.user)), \
                patch.object(main, "save_appeals"):
            await main.handle_appeal_staff_message(message, ticket)
        self.assertIn("**Evidence**\n  indented\nhttps://cdn.discord.test/file",
                      self.user.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
