# for-me

## Ban appeals via modmail

Set the bot token and the staff-only modmail channel ID in `.env`:

```env
DISCORD_TOKEN=your-discord-bot-token
modmail_channel_id=123456789012345678
role_ids=1530963363155279923,1531477075347636355,1532820561833496876,1532820446599319736
```

`role_ids` is a comma-separated list of roles to ping when an appeal arrives. The bot needs permission to view and send messages in the staff-only modmail channel, create threads, send messages in threads, mention those roles (or the roles must be mentionable), and view and remove bans in the appealed server. Enable the Message Content and Server Members intents in the Discord developer portal.

For bans made with `s!ban`, the bot attempts to DM the server name, reason, and **Appeal Ban** button before banning the user. The notice says the ban is being attempted, since it may fail. Once the ban succeeds, the button opens a staff thread; the user can then send ordinary DM messages. A user can also DM an appeal without the button. If there are multiple active appeals or bans, use `s!appeal <server_id> <message>` to select the server. An existing appeal is identified by user ID, not server membership. The bot can only identify bans recorded by its `s!ban` command.

Staff with Ban Members permission can reply in the thread, use `//` for internal notes, or `!close` to close it. The original modmail message has Approved and Declined buttons; approval unbans the user. Each decision closes the conversation and attempts to notify the user. Discord can reject DMs if the user blocks the bot or their privacy settings prevent delivery, including after they leave the server; staff are warned when delivery fails. No rejoin is required, but Discord controls whether messages can be delivered.

Appeals, message history, and processed message IDs are stored in `appeals.json`; ban records are stored in `infractions.json`. Keep these files private and persist/back up both across restarts or deployments. Existing JSON records are read automatically; legacy appeals are associated with the latest known ban for that user and server at startup. Only one appeal per recorded ban is allowed. Discord's API and local storage do not share a transaction: a crash or network failure during a send can leave delivery uncertain and a retry may duplicate a message. The Discord library handles API rate-limit waits.

## Join website attribution

Discord does not tell the bot which webpage referred a new member. To track a website, create a different Discord invite for each site, then register each invite's code:

```text
s!invitesource <invite-code-or-link> https://example.com/community
s!invitesources
s!invitesource <invite-code-or-link> remove
```

Use a unique invite for each website. These commands require Manage Server permission. When Discord identifies the invite used, the member log will show its registered website label and invite URL, plus the creator and channel when available. If members use an unregistered link, Server Discovery, or another route Discord does not identify, the log reports the website as unknown rather than guessing.