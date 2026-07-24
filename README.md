# AI Sales & Support Bot

Telegram bot that auto-replies to students as "Assistant" using Gemini AI,
and mirrors every conversation into a per-user forum topic in your support
group (with a Ban button), so your team can jump in anytime.

## Files
- `ai_reply_bot.py` — main bot code
- `requirements.txt` — Python dependencies
- `Procfile` — tells Render/Heroku how to run the worker

## Environment Variables (set these on Render/Heroku)

| Variable | Where to get it |
|---|---|
| `API_ID` | https://my.telegram.org |
| `API_HASH` | https://my.telegram.org |
| `BOT_TOKEN` | @BotFather on Telegram → `/newbot` |
| `MONGO_URL` | Your MongoDB connection string (same cluster as your other bot is fine — this bot uses its own separate database inside it, no data mixing) |
| `GEMINI_API_KEY` | https://aistudio.google.com → "Get API Key" (keep this secret, never share/screenshot it) |
| `OWNER_ID` | Your personal Telegram numeric user ID (get it from @userinfobot) |

## First-time setup (after deploying)

1. DM your new bot `/start` — admin panel appears.
2. Send the knowledge base in one go with `/bulkadd` (one fact per line).
3. Send `/setgroup`, then forward any message from your Topics-enabled
   support group — links it for the per-user topic system.
   - The bot must be **admin** in that group with **Manage Topics** permission.
   - The group must have **Topics** turned ON in its settings.
4. Test: message the bot from a different account and confirm a new topic
   appears in the group with Name/Username/User ID + a Ban button.

## Admin Commands

| Command | What it does |
|---|---|
| `/addinfo <text>` | Add one fact/FAQ |
| `/bulkadd` | Add many facts at once (one per line) |
| `/listinfo` | Show all saved facts with their IDs |
| `/delinfo <id>` | Remove a fact by ID |
| `/away` | Toggle auto-reply ON/OFF |
| `/setgroup` | Link a Topics-enabled group as the support desk |
| `/status` | Show current status + fact count |

## How the support desk works

- First message from any user → bot creates a new forum topic for them in
  your linked group, with their Name/Username/User ID and a Ban button.
- Every message (user's and the bot's AI reply) is mirrored into that topic.
- Reply inside that topic yourself → it's forwarded straight to the user's DM.
- Press "Ban this user" → the bot goes permanently silent to that user.

## Notes

- If `/away` is OFF, the bot stays silent and only mirrors user messages
  into their topic — you're expected to reply manually from there.
- Complaint/refund-related keywords trigger an extra alert DM to `OWNER_ID`.
- The bot always replies in whatever language the student is using
  (Hindi/English/Marathi/Gujarati).
