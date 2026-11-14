# for-me

## Ban appeals via modmail

Set the bot token and the staff-only modmail channel ID in `.env`:

```env
DISCORD_TOKEN=your-discord-bot-token
modmail_channel_id=123456789012345678
```

The bot needs permission to view and send messages in that channel, create threads, and send messages in threads. Banned users can DM the bot their appeal directly. If they are banned from multiple servers, they should include the server ID; staff can reply in the created thread, use `//` for notes that are not forwarded, and `!close` to close the conversation.

## Join website attribution

Discord does not tell the bot which webpage referred a new member. To track a website, create a different Discord invite for each site, then register each invite's code:

```text
s!invitesource <invite-code-or-link> https://example.com/community
s!invitesources
s!invitesource <invite-code-or-link> remove
```

Use a unique invite for each website. These commands require Manage Server permission. When Discord identifies the invite used, the member log will show its registered website label and invite URL, plus the creator and channel when available. If members use an unregistered link, Server Discovery, or another route Discord does not identify, the log reports the website as unknown rather than guessing.