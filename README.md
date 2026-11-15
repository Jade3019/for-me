# for-me

## Ban appeals via modmail

Set the bot token and the staff-only modmail channel ID in `.env`:

```env
DISCORD_TOKEN=your-discord-bot-token
modmail_channel_id=123456789012345678
role_ids=1530963363155279923,1531477075347636355,1532820561833496876,1532820446599319736
```

`role_ids` is a comma-separated list of roles to ping when an appeal arrives. The bot needs permission to view and send messages in the staff-only modmail channel, create threads, send messages in threads, mention those roles (or the roles must be mentionable), and view and remove bans in the appealed server. Enable the Message Content and Server Members intents in the Discord developer portal.

For bans made with `s!ban`, the bot attempts to DM the server name, reason, and **Appeal Ban** button before banning the user. The notice says the ban is being attempted, since it may fail. If Discord rejects delivery, the bot still bans the user and reports the Discord error code to the moderator; the underlying error is also logged to the bot's console. Error 50007 means Discord refused the DM, for example because of privacy settings or a block; the bot cannot override that decision. Once the ban succeeds, the button opens a staff thread; the user can then send ordinary DM messages. A user can also DM an appeal without the button. If there are multiple active appeals or bans, use `s!appeal <server_id> <message>` to select the server. An existing appeal is identified by user ID, not server membership. The bot can only identify bans recorded by its `s!ban` command.

Staff with Ban Members permission can reply in the thread, use `//` for internal notes, `!claim` to assign themselves, `!unassign` to clear the assignment, or `!close` to close it. The original modmail message has Approved and Declined buttons; approval unbans the user. Each decision closes the conversation and attempts to notify the user. Attachments are forwarded as Discord URLs, not reuploaded; links may expire or stop working when the original is deleted. When a DM deletion event is received, the history is marked deleted and staff are notified; the recorded text remains in the history.

Discord may reject outbound DMs, including acknowledgements and decisions, even after the user sends the bot a message. Its [error list](https://docs.discord.com/developers/topics/opcodes-and-status-codes#json) defines 50278 as no mutual guilds; 50007 and other 403 errors can also prevent delivery. A previously opened DM is not a guarantee of permission to send: an [API issue](https://github.com/discord/discord-api-docs/issues/8238) reports 50278 when sending through an existing DM channel. Interaction responses are tied to an actual interaction and [tokens expire after 15 minutes](https://docs.discord.com/developers/interactions/receiving-and-responding#followup-messages); they are not a verified solution for later moderator replies. The bot does not bypass Discord restrictions or claim delivery succeeded when it fails. Staff see the error and the appeal stays open.

Appeals, message history, delivery states, and processed message IDs are stored in `appeals.json`; ban records are stored in `infractions.json`. These JSON files are the existing storage, not a separate database. Keep them private and persist/back up both across restarts or deployments. Existing JSON records are migrated automatically; legacy appeals are associated with the matching or latest known ban where possible. Missing historical timestamps or ban reasons remain unknown rather than being fabricated. Only one appeal per recorded ban is allowed. Discord's API and local storage do not share a transaction: a crash or network failure during a send can leave a `pending` or failed delivery uncertain; staff should check the thread before manually resending. The Discord library handles API rate-limit waits.

## Join website attribution

Discord does not tell the bot which webpage referred a new member. To track a website, create a different Discord invite for each site, then register each invite's code:

```text
s!invitesource <invite-code-or-link> https://example.com/community
s!invitesources
s!invitesource <invite-code-or-link> remove
```

Use a unique invite for each website. These commands require Manage Server permission. When Discord identifies the invite used, the member log will show its registered website label and invite URL, plus the creator and channel when available. If members use an unregistered link, Server Discovery, or another route Discord does not identify, the log reports the website as unknown rather than guessing.