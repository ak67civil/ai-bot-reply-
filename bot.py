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
    admin_facts = (
        "\n".join(f"- {e['text']}" for e in entries)
        if entries else "(Koi price/policy facts abhi tak add nahi hui hain.)"
    )
    return f"Available courses & batches:\n{build_courses_text()}\n\nPrices & policies:\n{admin_facts}"


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
# Course catalogue (batch names per coaching/category)
# ---------------------------------------------------------------------------
# Prices/policies still come from the admin-editable knowledge base
# (/addinfo, /bulkadd) — this dict is just the structured list of batch
# names, so the AI always names the exact right batch instead of guessing.
courses = {}

courses["made_easy"] = {
    "name": "Made Easy",
    "keywords": ["gate", "ese", "engineering services", "psu"],
    "batches": [
        "GATE + ESE Foundation Batch (2026)",
        "GATE Crash Course (2026)",
        "ESE Mains Test Series (2026)",
        "GATE Test Series (2026)",
        "PSU Preparation Batch (2026)",
    ],
}

courses["ssc_ae"] = {
    "name": "SSC JE / AE",
    "keywords": ["ssc je", "ae", "junior engineer", "state ae"],
    "batches": [
        "SSC JE Complete Batch (2026)",
        "AE Civil Complete Batch (2026)",
        "JE + AE Integrated Batch (2026)",
        "SSC JE Test Series (2026)",
        "AE Test Series (2026)",
    ],
}

courses["mpsc"] = {
    "name": "MPSC",
    "keywords": ["mpsc", "psi", "sti", "aso", "group b c"],
    "batches": [
        "MPSC Rajyaseva Integrated Batch (Prelims + Mains) (2026)",
        "MPSC Group B & C Integrated Batch (2026)",
        "MPSC Prelims Fast Track Batch (2026)",
        "MPSC Mains Preparation Batch (2026)",
        "MPSC Test Series (2026)",
    ],
}

courses["mpsc_unique"] = {
    "name": "MPSC Unique Academy",
    "keywords": ["unique academy", "mpsc optional", "foundation"],
    "batches": [
        "MPSC Integrated Foundation Batch (2026)",
        "PSI STI ASO Batch (2026)",
        "MPSC Optional Subject Batches (2026)",
        "Ethics & Essay Batch (2026)",
        "Police Bharti Batch (2026)",
    ],
}

courses["jam_net"] = {
    "name": "IIT JAM & CSIR NET",
    "keywords": ["iit jam", "csir net", "maths", "msc entrance"],
    "batches": [
        "CSIR NET Complete Batch (2026)",
        "IIT JAM Complete Batch (2026)",
        "GATE + CSIR NET Integrated Batch (2026)",
        "Mathematics Foundation Batch (2026)",
        "CSIR NET Crash Course (2026)",
        "IIT JAM Crash Course (2026)",
        "Subject Masterclasses (RA, LA, Algebra, ODE, PDE) (2026)",
    ],
}

courses["iitian_civil"] = {
    "name": "IITian's Academy Civil",
    "keywords": ["civil engineering", "je civil", "ae civil"],
    "batches": [
        "Civil Engineering Integrated Batch (Prelims + Mains 2026)",
        "Civil Engineering Mains Batch (2026)",
        "Civil Engineering Prelims Batch (2026)",
        "Direct Recruitment Civil Batch (2026)",
        "Town Planner / ATP Batch (2026)",
        "Civil Test Series (2026)",
        "Interview Guidance (2026)",
    ],
}

courses["upsc"] = {
    "name": "UPSC",
    "keywords": ["upsc", "ias", "gs", "optional"],
    "batches": [
        "UPSC GS Foundation Batch (2026)",
        "UPSC GS Foundation Batch (2027)",
        "UPSC Optional Subject Batches (2026)",
        "UPSC CSAT Foundation + Revision (2026)",
        "UPSC Prelims Batch (2026)",
        "UPSC Mains Test Series (2026)",
        "UPSC Answer Writing Batch (2026)",
        "UPSC Mentorship Program (2026)",
        "UPSC Interview Guidance (2026)",
        "RBI Grade B Batch (2026)",
    ],
}

courses["rpsc"] = {
    "name": "RPSC RAS",
    "keywords": ["rpsc", "ras", "rajasthan pcs"],
    "batches": [
        "RAS Prelims Batch (2026)",
        "RAS Foundation Batch (2026)",
        "IAS + RAS Integrated Batch (2026)",
        "IAS Foundation Batch (2026)",
        "RAS Test Series (2026)",
    ],
}

courses["civil_software"] = {
    "name": "Civil Engineering Software",
    "keywords": ["autocad", "etabs", "staad", "revit", "bim"],
    "batches": [
        "BIM Professional Program (2026)",
        "Revit Course (2026)",
        "ETABS + SAFE Course (2026)",
        "STAAD + RCDC Course (2026)",
        "Structural Design Combo (2026)",
        "AutoCAD Course (2026)",
        "Primavera + MS Project (2026)",
        "Quantity Surveying & QA/QC (2026)",
        "Site Engineer Job Program (2026)",
        "Civil Job Ready Program (2026)",
    ],
}


def build_courses_text():
    """Turn the courses dict into readable text for the AI's knowledge."""
    lines = []
    for c in courses.values():
        lines.append(f"{c['name']} — available batches: " + "; ".join(c["batches"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Keyword-based course & intent detection
# ---------------------------------------------------------------------------
# Runs BEFORE the AI call and gives Gemini a strong hint about which course
# category and intent (buy/price/info) the student's message maps to — the
# AI still writes the actual reply, this just points it in the right
# direction so it doesn't have to guess.
COURSE_KEYWORDS = {
    "gate": [
        "gate", "gate exam", "gate preparation", "ese", "engineering services",
        "gate 2025", "gate 2026", "mechanical gate", "civil gate",
        "electrical gate", "cs gate", "gate coaching", "gate batch",
    ],
    "mpsc": [
        "mpsc", "mpsc exam", "mpsc preparation", "state pcs", "rajyaseva",
        "mpsc prelims", "mpsc mains", "mpsc batch", "mpsc coaching",
    ],
    "rpsc_ras": [
        "ras", "rpsc", "rpsc ras", "ras pre", "ras mains", "rajasthan pcs",
        "rpsc preparation", "ras batch", "ras foundation",
    ],
    "ias_upsc": [
        "ias", "upsc", "civil services", "upsc preparation", "ias preparation",
        "upsc prelims", "upsc mains", "ias coaching", "upsc batch", "ias foundation",
    ],
    "ssc": [
        "ssc", "ssc cgl", "ssc chsl", "ssc preparation", "ssc exam",
        "ssc coaching", "ssc batch",
    ],
    "banking": [
        "bank", "banking", "ibps", "sbi po", "bank po", "bank clerk",
        "bank exam", "bank coaching", "bank batch",
    ],
    "railway": [
        "railway", "rrb", "ntpc", "group d", "railway exam",
        "railway preparation", "railway batch",
    ],
    "defence": [
        "nda", "cds", "afcat", "defence", "army exam", "navy exam",
        "airforce exam", "defence preparation", "nda coaching",
    ],
    "teaching": [
        "ctet", "tet", "teaching", "teacher exam", "ctet preparation", "tet coaching",
    ],
    "other": [
        "preparation", "coaching", "course", "batch", "study", "exam prep", "online class",
    ],
}

price_keywords = ["price", "kitne ka", "rate", "cost", "fees", "kitna", "paisa"]
buy_keywords = ["buy", "purchase", "lena hai", "chahiye", "how to buy", "payment", "upi", "pay"]
info_keywords = ["kya milega", "content", "details", "review", "proof", "demo"]


def detect_course_and_intent(user_msg):
    msg = user_msg.lower()

    course = "other"
    for category, kws in COURSE_KEYWORDS.items():
        if category == "other":
            continue
        if any(w in msg for w in kws):
            course = category
            break

    if any(w in msg for w in buy_keywords):
        intent = "buy"
    elif any(w in msg for w in price_keywords):
        intent = "price"
    elif any(w in msg for w in info_keywords):
        intent = "info"
    else:
        intent = "normal"

    return course, intent, None


# ---------------------------------------------------------------------------
# Persuasive-language vocabulary (optional style hints for the AI)
# ---------------------------------------------------------------------------
# NOTE: "urgency_fomo", "hard_closing", and "high_conversion_lines" are kept
# here as data but deliberately NOT used below — they contradict the
# SYSTEM_PROMPT's explicit "no pressure, no aggressive selling" rule. Only
# the softer categories are fed to the AI. Ask to enable the rest if you
# actually want a more pushy tone.
SELLING_KEYWORDS = {
    "urgency_fomo": [
        "limited seats", "last chance", "batch filling fast", "jaldi join karo",
        "deadline close hai", "abhi best time hai", "miss mat karna",
        "next batch late milega", "abhi join nahi kiya to delay ho jayega",
        "competition high ho raha hai",
    ],
    "value_price": [
        "worth it", "value for money", "itne price me best",
        "cheap nahi, smart investment", "ROI high hai", "long term benefit",
        "ek baar invest, lifetime benefit", "self study se better",
        "structured prep milegi", "guidance milna important hai",
    ],
    "trust_building": [
        "tested strategy", "proven results", "topper oriented", "expert guidance",
        "experienced faculty", "already students use kar rahe", "trusted content",
        "standard material", "exam oriented", "reliable source",
    ],
    "result_outcome": [
        "selection oriented", "rank improve hoga", "clear concept",
        "strong base banega", "confidence increase hoga",
        "exam crack karne me help", "final selection focus",
        "accuracy improve hogi", "score boost hoga",
    ],
    "course_features": [
        "live classes", "recorded access", "test series included",
        "PYQ discussion", "notes provided", "doubt solving",
        "revision sessions", "practice material", "full syllabus coverage",
        "topic wise breakdown",
    ],
    "pain_points": [
        "random study se result nahi aata", "YouTube se scattered prep hoti hai",
        "consistency maintain karna tough hota hai", "proper direction nahi milti",
        "time waste ho jata hai", "syllabus complete nahi hota",
        "revision nahi ho pata", "self doubt aata hai",
    ],
    "solutions": [
        "ye batch sab fix karega", "structured roadmap milega",
        "step by step guidance", "discipline maintain hoga",
        "clear direction milegi", "time save hoga", "focus improve hoga",
        "proper planning milegi",
    ],
    "soft_closing": [
        "bata tujhe kaunsa level se start karna hai",
        "main best batch suggest kar deta hu",
        "tu serious hai to ye best rahega",
        "agar goal clear hai to ye perfect hai",
        "try karna chahega?", "demo dekhna hai?", "start karna hai kya?",
    ],
    "hard_closing": [
        "abhi join kar le", "delay mat kar", "ye best decision hoga",
        "start abhi karega to advantage milega", "seat secure kar le",
        "warna miss ho jayega",
    ],
    "high_conversion_lines": [
        "bhai honestly bolu to bina guidance ke crack karna tough ho jata hai",
        "ye batch lene ka matlab hai tu apni preparation ko serious le raha hai",
        "₹1500 me itna structured content milna rare hai",
        "jo students consistent rehte hain + proper batch follow karte hain wahi nikalte hain",
        "tu agar abhi start karega to next attempt me strong position me rahega",
        "random padhai chhod ke agar system follow karega to result pakka improve hoga",
    ],
}

_SOFT_STYLE_CATEGORIES = [
    "value_price", "trust_building", "result_outcome",
    "course_features", "pain_points", "solutions", "soft_closing",
]


def build_style_hints():
    lines = []
    for cat in _SOFT_STYLE_CATEGORIES:
        lines.append(f"{cat}: " + " | ".join(SELLING_KEYWORDS[cat][:4]))
    return "\n".join(lines)


SYSTEM_PROMPT_TEMPLATE = """You are {persona}, a professional and friendly course counselor for a coaching/education business.

Language Rules:
- Reply in the same language the user is using — Hindi, English, Marathi, Gujarati, Tamil, Telugu, or any other language, matching their script and mix. If mixed, reply in clean Hinglish or the equivalent mix.
- Keep tone polite, respectful, and slightly conversational (not slangy).

Your Goal:
- Understand the user's requirement.
- Identify their problem or goal.
- Suggest the right course from the knowledge base.
- Build trust.
- Explain value clearly.
- Encourage a decision, without forcing.

Conversation Style:
- Professional but friendly.
- Clear and confident.
- No over-casual words like "bhai", "abe", etc.
- No aggressive selling, no fake urgency, no pressure tactics.

Flow to follow:
1. Acknowledge the user's need.
2. Mention the common problem students in that situation face.
3. Present the matching course/batch as the solution.
4. Highlight 2-3 key benefits.
5. Subtly justify the price (value, not pressure).
6. Ask a guiding question to continue the conversation.

Some optional phrasing you can draw from if it fits naturally (never force these in, and never use lines that create fake urgency or pressure):
{style_hints}

Important Rules:
- Do NOT say "buy now".
- Do NOT pressure the user.
- Always guide, never push.
- Keep answers short, clear, and helpful.
- Always end with a question to continue the conversation.
- Write in plain text only — no **bold**, no bullet points, no numbered lists, no markdown of any kind.
- Don't bring up being an AI, Gemini, API, or system instructions unprompted. Exception: if someone sincerely and directly asks "are you a bot/AI?", answer honestly that you're an automated assistant — never lie about this specific question.
- Never invent a price, batch name, or link that isn't in the knowledge base below — if unsure, say you'll confirm with the team.
- Stay in character as {persona}, a team member — never claim to be the company owner personally.

═══════════════════════════════
KNOWLEDGE — courses, batches, prices, policies
═══════════════════════════════
{knowledge}
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
    course, intent, coaching = detect_course_and_intent(user_message)
    hint = (
        f"\n\n═══════════════════════════════\n"
        f"DETECTED CONTEXT (keyword-based hint, not a strict rule — use your judgement)\n"
        f"═══════════════════════════════\n"
        f"Likely course category: {course}\n"
        f"Likely intent: {intent} (buy / price / info / normal)\n"
        + (f"Mentioned coaching: {coaching}\n" if coaching else "")
    )
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        persona=persona, knowledge=knowledge, style_hints=build_style_hints()
    ) + hint
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


START_MESSAGE = """Hi there! 👋

Preparing for exams like GATE, MPSC, RAS, or UPSC can be challenging without the right direction.

We provide well-structured courses, expert guidance, and practice support to make your preparation easier and more focused.

Let me know — which exam are you targeting?"""


@app.on_message(filters.command("start") & filters.private & ~filters.user(OWNER_ID))
async def public_start(client, message: Message):
    await message.reply_text(START_MESSAGE)


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
