import os
import re
import time
import random
import logging
import asyncio
import requests
import pymongo
from pyrogram import Client, filters
from pyrogram.raw import functions
from pyrogram.raw.types import InputChannel
from pyrogram.enums import ParseMode, ChatType, ChatAction
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (set these as environment variables on your host)
# ---------------------------------------------------------------------------
API_ID = int(os.environ["API_ID"])            # from my.telegram.org
API_HASH = os.environ["API_HASH"]              # from my.telegram.org
BOT_TOKEN = os.environ["BOT_TOKEN"]             # from @BotFather
MONGO_URL = os.environ["MONGO_URL"]             # MongoDB connection string
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]   # from aistudio.google.com
OWNER_ID = int(os.environ["OWNER_ID"])          # YOUR Telegram numeric user ID

PERSONA_NAME = "Assistant"
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

app = Client("ai_reply_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
mongo = pymongo.MongoClient(MONGO_URL)
db = mongo["ai_reply_bot"]
config_col = db["config"]
knowledge_col = db["knowledge"]
history_col = db["history"]
topics_col = db["topics"]  # one doc per user: {_id: user_id, thread_id, name, username, banned}
users_col = db["users"]  # one doc per user who has EVER messaged the bot: {_id: user_id, name, username}


def get_config():
    doc = config_col.find_one({"_id": "settings"})
    if not doc:
        doc = {"_id": "settings", "ai_enabled": True}
        config_col.insert_one(doc)
    return doc


def set_config(key, value):
    config_col.update_one({"_id": "settings"}, {"$set": {key: value}}, upsert=True)


def get_knowledge_text():
    entries = list(knowledge_col.find().sort("added_at", 1))
    if not entries:
        return "(Koi specific info abhi tak add nahi hui hai.)"
    return "\n".join(f"- {e['text']}" for e in entries)


def add_knowledge(text):
    entry_id = int(time.time() * 1000)
    knowledge_col.insert_one({"id": entry_id, "text": text, "added_at": time.time()})
    return entry_id


def del_knowledge(entry_id):
    return knowledge_col.delete_one({"id": entry_id}).deleted_count > 0


def get_history(user_id, limit=6):
    doc = history_col.find_one({"_id": user_id})
    if not doc:
        return []
    return doc.get("messages", [])[-limit:]


def append_history(user_id, role, text):
    history_col.update_one(
        {"_id": user_id},
        {"$push": {"messages": {"role": role, "text": text, "ts": time.time()}}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Support-desk forum topics — one topic per user in the linked group
# ---------------------------------------------------------------------------
async def get_or_create_topic(client: Client, user):
    """Returns (thread_id, is_banned). Creates a new forum topic for this
    user the first time they message the bot, with a header (name, ID,
    username) and a Ban button, and remembers the mapping for later."""
    doc = topics_col.find_one({"_id": user.id})
    if doc and doc.get("thread_id"):
        return doc["thread_id"], doc.get("banned", False)

    group_id = get_config().get("support_group_id")
    if not group_id:
        return None, False  # no group linked yet — skip topic mirroring

    title = f"{user.first_name or 'User'} | {user.id}"[:128]
    try:
        # This Pyrogram version has no high-level create_forum_topic(), so
        # we call the underlying raw Telegram API method directly instead.
        peer = await client.resolve_peer(int(group_id))
        channel = InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
        result = await client.invoke(
            functions.channels.CreateForumTopic(
                channel=channel,
                title=title,
                random_id=random.randint(0, 0x7FFFFFFFFFFFFFFF),
            )
        )
        thread_id = None
        for upd in getattr(result, "updates", []):
            msg = getattr(upd, "message", None)
            if msg is not None:
                thread_id = msg.id
                break
        if thread_id is None:
            raise RuntimeError(f"Could not find new topic's message id in: {result}")
    except Exception as e:
        logger.error(f"Failed to create forum topic for user {user.id}: {e}")
        return None, False

    topics_col.update_one(
        {"_id": user.id},
        {"$set": {
            "thread_id": thread_id,
            "name": user.first_name or "",
            "username": user.username or "",
            "banned": False,
        }},
        upsert=True,
    )

    uname = f"@{user.username}" if user.username else "—"
    header = (
        f"👤 <b>New Conversation</b>\n\n"
        f"Name: {user.first_name or ''}\n"
        f"Username: {uname}\n"
        f"User ID: <code>{user.id}</code>\n\n"
        f"<i>Reply in this topic to message the user directly.</i>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Ban this user", callback_data=f"ban_{user.id}")]])
    try:
        await client.send_message(
            int(group_id), header, reply_to_message_id=thread_id,
            reply_markup=kb, parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Failed to send topic header for user {user.id}: {e}")

    return thread_id, False


async def mirror_to_topic(client: Client, thread_id, label, text):
    """Post a copy of a message into the user's topic so the admin can
    follow the whole conversation from the group."""
    group_id = get_config().get("support_group_id")
    if not group_id or not thread_id:
        return
    try:
        await client.send_message(
            int(group_id), f"<b>{label}:</b> {text}",
            reply_to_message_id=thread_id, parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Failed to mirror message to topic {thread_id}: {e}")


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = """You are "{persona}", a warm, professional sales & support team member for an education company that sells GATE/ESE, SSC JE/AE/JE, and MPSC exam-preparation course batches on Telegram.

═══════════════════════════════
LANGUAGE
═══════════════════════════════
Always reply in the SAME language the student is using — Hindi, English, Marathi, Gujarati, Tamil, Telugu, Kannada, Bengali, Punjabi, or any other Indian or regional language, including romanized/Hinglish-style typing. Match their language and script on every message, not just the first one. If they mix languages, mirror that mix naturally. If unsure which language they mean, default to Hindi+English mix (Hinglish).

═══════════════════════════════
CONVERSATION FLOW — always follow this order
═══════════════════════════════
Get straight to business. Do NOT make small talk, do NOT ask "how are you" or anything personal — students are here for course info, not a chat.

1. The student was already asked (before you got involved) to tell you their course name and coaching name together. Read their message carefully for both.
2. If EITHER the course/stream/exam OR the coaching name is missing or unclear, ask ONLY for the missing piece — briefly, no small talk. Example: "Kaunsi coaching ka batao — Made Easy, IES Master, ya PW?"
3. Once you know both, respond immediately with the course details from the knowledge base:
   - Confirm the exact batch/course name and coaching.
   - What's included: complete lectures (technical + non-technical, when applicable), workbook PDF, test series.
   - Delivery: private Telegram channel, LIFETIME access, joined automatically right after payment.
   - The price.
4. For SSC JE/AE/JE students specifically: pitch the all-access Membership FIRST (see membership info below) before individual coaching prices.
5. End with a clear, confident push toward purchase — but stay honest, never pushy to the point of being dishonest.

═══════════════════════════════
NEGOTIATION RULES (MPSC only)
═══════════════════════════════
- MPSC CSE/Rajyaseva course: fixed price, do not negotiate down.
- MPSC Descriptive course: quote the starting price first. If the student negotiates, you may bring the price down, but NEVER go below the minimum listed in the knowledge base — hold firm there as your final price.
- For all other courses (GATE/ESE, SSC JE/AE/JE): prices are fixed, do not negotiate.

═══════════════════════════════
KNOWLEDGE — use ONLY this for course names, prices, and links
═══════════════════════════════
{knowledge}

═══════════════════════════════
GENERAL RULES
═══════════════════════════════
- Stay in character as {persona}, a support/sales team member — never claim to be the company owner personally.
- If someone sincerely and directly asks "are you a bot/AI?", answer honestly that you're an automated assistant. Never lie about this.
- Never invent a price, batch name, or link that isn't in the knowledge base above — if unsure, say you'll confirm with the team.
- Write in plain, casual text ONLY — the way a real person types on WhatsApp/Telegram. NEVER use markdown formatting: no **bold**, no bullet points (•, -, *), no numbered lists, no headers. If you need to list a few things (like what's included in a course), just write them in a flowing sentence separated by commas, not as a list.
- Keep replies short and natural (2-4 sentences), like a real chat message — not a wall of text, not a corporate template.
- If it's a complaint, refund dispute, or payment issue, say you're flagging it for the team to personally follow up, and stay polite.
"""


def strip_markdown(text):
    """Safety net: strip markdown formatting the AI might slip in despite
    instructions, so replies always read like plain human texting."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)   # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)         # __bold__
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)  # *italic*
    text = re.sub(r"^\s*[\*\-•]\s+", "", text, flags=re.MULTILINE)  # bullets
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)     # 1. 2. 3.
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)       # headers
    return text.strip()


def ask_gemini(persona, knowledge, history, user_message):
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(persona=persona, knowledge=knowledge)
    contents = []
    for h in history:
        role = "user" if h["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": h["text"]}]})
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 300},
    }
    try:
        resp = requests.post(
            GEMINI_URL,
            json=payload,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return strip_markdown(text)
    except requests.exceptions.HTTPError as e:
        logger.error(f"Gemini API error: {e} | Response body: {resp.text}")
        return None
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return None


# ---------------------------------------------------------------------------
# Admin commands (only YOU, in private chat with the bot)
# ---------------------------------------------------------------------------
ADMIN_COMMANDS = ["addinfo", "bulkadd", "delinfo", "listinfo", "away", "status", "start", "setgroup", "listmodels", "broadcast"]


@app.on_message(filters.command("start") & filters.private & filters.user(OWNER_ID))
async def admin_start(client, message: Message):
    cfg = get_config()
    status = "ON ✅" if cfg.get("ai_enabled", True) else "OFF (paused) ⏸️"
    await message.reply_text(
        f"<b>🤖 AI Auto-Reply Bot — Admin Panel</b>\n\n"
        f"Auto-reply status: {status}\n\n"
        f"<b>Commands:</b>\n"
        f"/addinfo &lt;text&gt; — Add a new fact/FAQ\n"
        f"/bulkadd — Add many facts at once (one per line)\n"
        f"/listinfo — Show all saved info\n"
        f"/delinfo &lt;id&gt; — Remove an entry by ID\n"
        f"/away — Toggle auto-reply ON/OFF\n"
        f"/setgroup — Link a Topics-enabled group as the support desk\n"
        f"/broadcast &lt;text&gt; — Message everyone who has ever messaged the bot\n"
        f"/listmodels — Show available Gemini models for your key\n"
        f"/status — Show current status",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("broadcast") & filters.private & filters.user(OWNER_ID))
async def admin_broadcast(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text(
            "Usage: /broadcast <message>\n\n"
            "Ye message un SABHI users ko jaayega jinhone bot ko kabhi bhi message kiya hai."
        )
        return
    text = parts[1].strip()

    all_users = list(users_col.find())
    if not all_users:
        await message.reply_text("Abhi tak koi user nahi mila jisne bot ko message kiya ho.")
        return

    status_msg = await message.reply_text(f"📤 Bhej raha hoon... 0/{len(all_users)}")
    sent, failed = 0, 0
    for u in all_users:
        try:
            await client.send_message(u["_id"], text)
            sent += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await client.send_message(u["_id"], text)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limits
        if (sent + failed) % 20 == 0:
            try:
                await status_msg.edit_text(f"📤 Bhej raha hoon... {sent + failed}/{len(all_users)}")
            except Exception:
                pass

    await status_msg.edit_text(f"✅ Broadcast complete.\nSent: {sent}\nFailed: {failed}")


@app.on_message(filters.command("setgroup") & filters.private & filters.user(OWNER_ID))
async def admin_setgroup(client, message: Message):
    set_config("awaiting_group_link", True)
    await message.reply_text(
        "📎 Ab us GROUP se koi bhi message yahan <b>forward</b> karo\n\n"
        "(Zaroori: us group me <b>Topics feature ON</b> ho, aur bot wahan "
        "<b>admin</b> ho 'Manage Topics' permission ke saath.)",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.private & filters.forwarded & filters.user(OWNER_ID))
async def admin_group_link(client, message: Message):
    cfg = get_config()
    if not cfg.get("awaiting_group_link"):
        return
    chat = message.forward_from_chat
    if not chat or chat.type != ChatType.SUPERGROUP:
        await message.reply_text("❌ Ye kisi group ka message nahi laga. Group se hi ek message forward karo.")
        return
    set_config("support_group_id", chat.id)
    set_config("awaiting_group_link", False)
    is_forum = bool(getattr(chat, "is_forum", False))
    warn = (
        "" if is_forum else
        "\n\n⚠️ Warning: Is group me Topics feature ON nahi dikh raha — "
        "Group Settings me 'Topics' enable karo, warna support-desk kaam nahi karega."
    )
    await message.reply_text(f"✅ Support group linked: {chat.title}{warn}")


@app.on_message(filters.command("bulkadd") & filters.private & filters.user(OWNER_ID))
async def admin_bulkadd(client, message: Message):
    parts = message.text.split("\n")
    lines = [parts[0].split(" ", 1)[1]] if len(parts[0].split(" ", 1)) > 1 else []
    lines += [l for l in parts[1:] if l.strip()]
    lines = [l.strip() for l in lines if l.strip()]
    if not lines:
        await message.reply_text(
            "Usage: /bulkadd, phir har fact/FAQ ek naye line par likho.\n\n"
            "Example:\n/bulkadd\nHistory Batch price is Rs 999.\n"
            "Hindi Batch starts every Monday.\nRefund window is 3 days."
        )
        return
    ids = [add_knowledge(l) for l in lines]
    await message.reply_text(f"✅ {len(ids)} facts added.")


@app.on_message(filters.command("addinfo") & filters.private & filters.user(OWNER_ID))
async def admin_addinfo(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text(
            "Usage: /addinfo <fact/FAQ text>\n\n"
            "Example:\n/addinfo History Batch price is Rs 999, includes 30 videos + PDF notes."
        )
        return
    entry_id = add_knowledge(parts[1].strip())
    await message.reply_text(f"✅ Added (ID: <code>{entry_id}</code>)", parse_mode=ParseMode.HTML)


@app.on_message(filters.command("listinfo") & filters.private & filters.user(OWNER_ID))
async def admin_listinfo(client, message: Message):
    entries = list(knowledge_col.find().sort("added_at", 1))
    if not entries:
        await message.reply_text("Koi info abhi tak add nahi hui.")
        return
    lines = [f"<code>{e['id']}</code> — {e['text']}" for e in entries]
    await message.reply_text("<b>📚 Saved Info:</b>\n\n" + "\n\n".join(lines), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("delinfo") & filters.private & filters.user(OWNER_ID))
async def admin_delinfo(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.reply_text("Usage: /delinfo <id>  (ID /listinfo se milega)")
        return
    ok = del_knowledge(int(parts[1].strip()))
    await message.reply_text("✅ Removed." if ok else "❌ Ye ID nahi mili.")


@app.on_message(filters.command("away") & filters.private & filters.user(OWNER_ID))
async def admin_toggle(client, message: Message):
    cfg = get_config()
    new_state = not cfg.get("ai_enabled", True)
    set_config("ai_enabled", new_state)
    await message.reply_text(
        "✅ Auto-reply ab ON hai — bot khud replies karega."
        if new_state
        else "⏸️ Auto-reply ab OFF hai — aap khud manually reply karoge."
    )


@app.on_message(filters.command("listmodels") & filters.private & filters.user(OWNER_ID))
async def admin_listmodels(client, message: Message):
    try:
        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        names = [
            m["name"].replace("models/", "")
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
        if not names:
            await message.reply_text("Koi model nahi mila — key check karo.")
            return
        await message.reply_text(
            "<b>Available models for your key:</b>\n\n" + "\n".join(names),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")


@app.on_message(filters.command("status") & filters.private & filters.user(OWNER_ID))
async def admin_status(client, message: Message):
    cfg = get_config()
    status = "ON ✅" if cfg.get("ai_enabled", True) else "OFF ⏸️"
    count = knowledge_col.count_documents({})
    await message.reply_text(f"Auto-reply: {status}\nSaved info entries: {count}")


@app.on_message(filters.command("start") & filters.private & ~filters.user(OWNER_ID))
async def public_start(client, message: Message):
    await message.reply_text(
        "Hlo Sir, aapko konsa course chahiye? Apna course ka naam aur coaching "
        "ka naam dono sahi se batao, tabhi main aapko sahi reply de paunga."
    )


# ---------------------------------------------------------------------------
# Customer auto-reply (anyone who isn't the admin)
# ---------------------------------------------------------------------------
@app.on_message(
    filters.private
    & filters.text
    & ~filters.user(OWNER_ID)
    & ~filters.command(ADMIN_COMMANDS)
)
async def customer_reply(client, message: Message):
    cfg = get_config()

    users_col.update_one(
        {"_id": message.from_user.id},
        {"$set": {
            "name": message.from_user.first_name or "",
            "username": message.from_user.username or "",
        }},
        upsert=True,
    )

    thread_id, banned = await get_or_create_topic(client, message.from_user)
    if banned:
        return  # banned user — stay completely silent

    if not cfg.get("ai_enabled", True):
        await mirror_to_topic(client, thread_id, message.from_user.first_name or "User", message.text)
        return  # admin paused auto-reply — message is mirrored, admin replies manually in the topic

    user_id = message.from_user.id
    user_message = message.text

    await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    await mirror_to_topic(client, thread_id, message.from_user.first_name or "User", user_message)

    knowledge = get_knowledge_text()
    history = get_history(user_id)

    reply = await asyncio.to_thread(ask_gemini, PERSONA_NAME, knowledge, history, user_message)

    if reply is None:
        await client.send_message(
            OWNER_ID,
            f"⚠️ AI reply failed for user {message.from_user.id} "
            f"({message.from_user.first_name}). Please reply manually:\n\n{user_message}",
        )
        return

    await message.reply_text(reply)
    await mirror_to_topic(client, thread_id, PERSONA_NAME, reply)
    append_history(user_id, "user", user_message)
    append_history(user_id, "model", reply)

    escalate_words = ["refund", "complaint", "fraud", "scam", "cheated", "शिकायत", "पैसा वापस"]
    if any(w in user_message.lower() for w in escalate_words):
        uname = f"@{message.from_user.username}" if message.from_user.username else "no username"
        await client.send_message(
            OWNER_ID,
            f"🚨 Possible complaint/refund query from {message.from_user.first_name} "
            f"({uname}):\n\n{user_message}",
        )


# ---------------------------------------------------------------------------
# Admin/team replying from inside a user's topic → forwarded to that user
# ---------------------------------------------------------------------------
@app.on_message(filters.group & filters.text)
async def group_topic_reply(client, message: Message):
    cfg = get_config()
    group_id = cfg.get("support_group_id")
    if not group_id or message.chat.id != int(group_id):
        return
    # This Pyrogram version has no message.message_thread_id — a message
    # posted inside a topic instead shows up as a reply, either to the
    # topic's root message directly (reply_to_message_id) or, for deeper
    # threads, reply_to_top_message_id points to that root.
    thread_id = message.reply_to_top_message_id or message.reply_to_message_id
    if not thread_id:
        return
    if message.from_user and message.from_user.is_self:
        return  # ignore the bot's own mirrored messages

    doc = topics_col.find_one({"thread_id": thread_id})
    if not doc:
        return  # not a linked user-conversation topic

    user_id = doc["_id"]
    try:
        await client.send_message(user_id, message.text)
    except Exception as e:
        logger.error(f"Failed to forward reply to user {user_id}: {e}")
        await message.reply_text(f"⚠️ User ko reply nahi bhej paya: {e}")


# ---------------------------------------------------------------------------
# Ban button
# ---------------------------------------------------------------------------
@app.on_callback_query(filters.regex(r"^ban_(\d+)$"))
async def ban_callback(client, query: CallbackQuery):
    user_id = int(query.data.split("_", 1)[1])
    topics_col.update_one({"_id": user_id}, {"$set": {"banned": True}})
    await query.answer("✅ User banned — ab wo bot ko message nahi bhej payega.", show_alert=True)
    try:
        await query.message.edit_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Banned", callback_data="noop")]])
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^noop$"))
async def noop_callback(client, query: CallbackQuery):
    await query.answer()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("AI Auto-Reply Bot starting...")
    app.run()
