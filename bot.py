"""
Telegram Community Manager Bot
================================
Handles, in one bot:
  1. Spam filtering (links/keywords from non-admins)
  2. Tag members (mentions everyone who has been seen chatting)
  3. Unique per-member referral links (tracks who invited whom into the group)
  4. Welcome messages for new members
  5. Weekly "most active members" leaderboard
  6. Scheduled auto-posting of news from RSS feeds

Everything is stored in a local SQLite file (bot_data.db) so it survives restarts.
"""

import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import feedparser
from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from telethon import TelegramClient as TelethonClient
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False

# ---------------------------------------------------------------------------
# CONFIG (all pulled from environment variables — see .env.example)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])          # your community group id (negative number)
NEWS_FEED_URLS = [u.strip() for u in os.environ.get("NEWS_FEED_URLS", "").split(",") if u.strip()]
NEWS_POST_HOUR = int(os.environ.get("NEWS_POST_HOUR", "10"))  # 24-hour, Africa/Lagos time — one post/day
SPAM_KEYWORDS = [w.strip().lower() for w in os.environ.get("SPAM_KEYWORDS", "airdrop claim,free giveaway,dm me for,t.me/+").split(",") if w.strip()]

# Opportunity scanning (contests, giveaways, ambassador programs) — ON DEMAND ONLY,
# triggered by an admin typing /opportunities. Never runs automatically in the background.
TG_API_ID = os.environ.get("TG_API_ID")
TG_API_HASH = os.environ.get("TG_API_HASH")
TARGET_CHANNELS = [c.strip() for c in os.environ.get("TARGET_CHANNELS", "").split(",") if c.strip()]
PERSONAL_SESSION_NAME = os.environ.get("TG_SESSION_NAME", "member_sync_session")
OPPORTUNITY_KEYWORDS = [
    "contest", "giveaway", "airdrop", "referral", "bounty", "whitelist",
    "reward", "prize", "campaign", "ambassador", "presale bonus",
    "leaderboard", "win ", "earn ", "claim now", "free mint", "raffle",
]
opportunity_pattern = re.compile("|".join(re.escape(k) for k in OPPORTUNITY_KEYWORDS), re.IGNORECASE)

DB_PATH = os.environ.get("DB_PATH", "bot_data.db")
CALENDAR_FILE = os.environ.get("CALENDAR_FILE", "community_calendar.json")
CALENDAR_POST_HOUR = int(os.environ.get("CALENDAR_POST_HOUR", "9"))  # 24-hour, Africa/Lagos time
NIGERIA_TZ = ZoneInfo("Africa/Lagos")

RULES_TEXT = """\U0001F4D8 *Blockfest Africa Community Rules*

• Treat every member with respect, regardless of their experience or background.
• Contribute to conversations that educate, inspire or create value for the community.
• Keep discussions relevant to Blockfest, blockchain, Web3, AI and emerging technologies.
• Avoid spamming the community with repeated messages, advertisements or irrelevant promotions.
• Do not share scams, phishing links or misleading information under any circumstance.
• Network respectfully and avoid sending unsolicited direct messages to community members.
• Support fellow members by answering questions, sharing opportunities and celebrating their wins.
• Respect different opinions and engage in healthy discussions without insults or personal attacks.
• Protect your personal information and never share your wallet seed phrase or other sensitive details.
• Follow the guidance of the community manager and moderators to help keep the community safe, welcoming and organised.
• Introduce yourself, join conversations and make meaningful connections throughout your Blockfest journey.

\U0001F499 Buidl. Bridge. Become.\U0001F499"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("community_bot")


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            invite_link TEXT,
            invite_link_name TEXT,
            referral_count INTEGER DEFAULT 0,
            warnings INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS activity (
            user_id INTEGER,
            day TEXT,
            message_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, day)
        );

        CREATE TABLE IF NOT EXISTS posted_news (
            link TEXT PRIMARY KEY
        );
        """
    )
    conn.commit()
    conn.close()


def upsert_user(user_id, username, first_name):
    conn = db()
    conn.execute(
        """INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
        (user_id, username, first_name),
    )
    conn.commit()
    conn.close()


def bump_activity(user_id):
    day = datetime.utcnow().strftime("%Y-%m-%d")
    conn = db()
    conn.execute(
        """INSERT INTO activity (user_id, day, message_count) VALUES (?, ?, 1)
           ON CONFLICT(user_id, day) DO UPDATE SET message_count = message_count + 1""",
        (user_id, day),
    )
    conn.commit()
    conn.close()


def top_active_this_week(limit=10):
    since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    conn = db()
    rows = conn.execute(
        """SELECT u.username, u.first_name, SUM(a.message_count) as total
           FROM activity a JOIN users u ON u.user_id = a.user_id
           WHERE a.day >= ?
           GROUP BY a.user_id
           ORDER BY total DESC
           LIMIT ?""",
        (since, limit),
    ).fetchall()
    conn.close()
    return rows


def all_known_users():
    conn = db()
    rows = conn.execute("SELECT user_id, username, first_name FROM users").fetchall()
    conn.close()
    return rows


def load_seed_members():
    """One-time catch-up: if member_sync.py has produced members_seed.json,
    load everyone in it into the users table so /tagall reaches silent
    members who joined before the bot existed. Safe to run every startup —
    it just updates existing records."""
    seed_path = os.environ.get("MEMBERS_SEED_FILE", "members_seed.json")
    if not os.path.exists(seed_path):
        return
    try:
        with open(seed_path) as f:
            members = json.load(f)
        for m in members:
            upsert_user(m["user_id"], m.get("username"), m.get("first_name"))
        log.info(f"Loaded {len(members)} members from {seed_path} into tracking DB.")
    except Exception as e:
        log.error(f"Failed to load {seed_path}: {e}")


def add_warning(user_id):
    conn = db()
    conn.execute("UPDATE users SET warnings = warnings + 1 WHERE user_id = ?", (user_id,))
    row = conn.execute("SELECT warnings FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.commit()
    conn.close()
    return row["warnings"] if row else 0


def set_invite_link(user_id, link, name):
    conn = db()
    conn.execute("UPDATE users SET invite_link = ?, invite_link_name = ? WHERE user_id = ?", (link, name, user_id))
    conn.commit()
    conn.close()


def get_invite_link(user_id):
    conn = db()
    row = conn.execute("SELECT invite_link FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row["invite_link"] if row else None


def find_user_by_invite_name(name):
    conn = db()
    row = conn.execute("SELECT user_id FROM users WHERE invite_link_name = ?", (name,)).fetchone()
    conn.close()
    return row["user_id"] if row else None


def increment_referral(user_id):
    conn = db()
    conn.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_referral_count(user_id):
    conn = db()
    row = conn.execute("SELECT referral_count FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row["referral_count"] if row else 0


def already_posted(link):
    conn = db()
    row = conn.execute("SELECT 1 FROM posted_news WHERE link = ?", (link,)).fetchone()
    conn.close()
    return row is not None


def mark_posted(link):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO posted_news (link) VALUES (?)", (link,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def mention(user_id, name):
    return f"[{name}](tg://user?id={user_id})"


def clean_summary(raw_html: str, max_len: int = 280) -> str:
    """RSS summaries often contain raw HTML tags (<p>, <a>, etc). Strip them
    and decode entities so the posted message reads as plain, clean text."""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", "", raw_html)   # remove HTML tags
    text = html.unescape(text)                 # decode &amp; &#39; etc into normal characters
    text = re.sub(r"\s+", " ", text).strip()   # collapse extra whitespace/newlines
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "..."
    return text


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# FEATURE 4: WELCOME NEW MEMBERS  +  referral-link join tracking
# ---------------------------------------------------------------------------
async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if cmu.chat.id != GROUP_CHAT_ID:
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status

    joined_now = old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, ChatMemberStatus.RESTRICTED) and \
                 new_status in (ChatMemberStatus.MEMBER,)

    if not joined_now:
        return

    user = cmu.new_chat_member.user
    upsert_user(user.id, user.username, user.first_name)

    # Welcome message
    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=f"Welcome to the community, {mention(user.id, user.first_name)}! "
             f"Glad to have you here \U0001F44B\nType /rules to see our community guidelines before you dive in.",
        parse_mode="Markdown",
    )

    # Referral tracking: Telegram tells us which invite link was used, if any
    invite_link_obj = cmu.invite_link
    if invite_link_obj is not None:
        referrer_id = find_user_by_invite_name(invite_link_obj.name or "")
        if referrer_id:
            increment_referral(referrer_id)
            log.info(f"User {user.id} joined via referral link of {referrer_id}")


# ---------------------------------------------------------------------------
# FEATURE 3: PERSONAL REFERRAL LINK  (DM the bot with /myreflink)
# ---------------------------------------------------------------------------
async def cmd_myreflink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    existing = get_invite_link(user.id)
    if existing:
        count = get_referral_count(user.id)
        await update.message.reply_text(
            f"Your personal invite link:\n{existing}\n\nPeople who joined through it: {count}"
        )
        return

    link_name = f"ref_{user.id}"
    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=GROUP_CHAT_ID,
            name=link_name,
            creates_join_request=False,
        )
    except Exception as e:
        await update.message.reply_text(
            "I couldn't create your link. Make sure I'm an admin with 'invite users via link' "
            "permission in the group."
        )
        log.error(f"invite link creation failed: {e}")
        return

    set_invite_link(user.id, invite.invite_link, link_name)
    await update.message.reply_text(
        f"Here's your personal invite link — share it and I'll track everyone who joins through it:\n"
        f"{invite.invite_link}"
    )


# ---------------------------------------------------------------------------
# FEATURE 5: ACTIVITY TRACKING + LEADERBOARD
# ---------------------------------------------------------------------------
async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = top_active_this_week()
    if not rows:
        await update.message.reply_text("No activity recorded yet this week.")
        return
    lines = ["\U0001F3C6 Most active members this week:\n"]
    for i, r in enumerate(rows, start=1):
        name = f"@{r['username']}" if r["username"] else r["first_name"]
        lines.append(f"{i}. {name} — {r['total']} messages")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# FEATURE 2: TAG MEMBERS  (mentions everyone the bot has seen chat)
# ---------------------------------------------------------------------------
async def cmd_tagall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can use /tagall.")
        return

    users = all_known_users()
    if not users:
        await update.message.reply_text("No members tracked yet — this grows as people chat.")
        return

    note = " ".join(update.message.text.split(" ")[1:]) or "\U0001F4E2 Attention everyone!"

    # Telegram hard-limits a single message to 4096 characters. We pack as many
    # mentions as will fit into one message, and only start a new message once
    # the current one is full — so it reads as a normal tag list, not a spam burst.
    MAX_LEN = 4000  # leave headroom for the note + formatting
    mentions = [mention(u["user_id"], u["first_name"] or (u["username"] or "member")) for u in users]

    messages = []
    current = note
    for m in mentions:
        candidate = current + " " + m
        if len(candidate) > MAX_LEN:
            messages.append(current)
            current = m
        else:
            current = candidate
    if current:
        messages.append(current)

    for i, text in enumerate(messages):
        await context.bot.send_message(GROUP_CHAT_ID, text, parse_mode="Markdown")
        if i < len(messages) - 1:
            time.sleep(1.5)  # stay under Telegram's rate limits between messages


# ---------------------------------------------------------------------------
# SHARED ESCALATION LOGIC — used by both automatic spam detection AND the
# manual /warn command, so every violation (automatic or admin-issued) counts
# toward the same 3-strike system: warn, warn, 7-day ban, then permanent removal.
# ---------------------------------------------------------------------------
async def apply_violation(context: ContextTypes.DEFAULT_TYPE, user_id: int, first_name: str, reason: str = ""):
    violations = add_warning(user_id)
    reason_line = f"\nReason: {reason}" if reason else ""

    if violations <= 2:
        await context.bot.send_message(
            GROUP_CHAT_ID,
            f"\u26A0\uFE0F {mention(user_id, first_name)}, you've received a warning.{reason_line}\n"
            f"Warning {violations}/2 — a 3rd violation results in a 7-day ban, and a 4th results in "
            f"permanent removal. Type /rules to review our guidelines.",
            parse_mode="Markdown",
        )
    elif violations == 3:
        until = datetime.utcnow() + timedelta(days=7)
        await context.bot.ban_chat_member(GROUP_CHAT_ID, user_id, until_date=until)
        await context.bot.send_message(
            GROUP_CHAT_ID,
            f"\U0001F6AB {mention(user_id, first_name)} has been banned for 7 days after repeated "
            f"rule violations.{reason_line}",
            parse_mode="Markdown",
        )
    else:
        await context.bot.ban_chat_member(GROUP_CHAT_ID, user_id)
        await context.bot.send_message(
            GROUP_CHAT_ID,
            f"\u274C {mention(user_id, first_name)} has been permanently removed after repeated rule "
            f"violations following a prior 7-day ban.{reason_line}",
            parse_mode="Markdown",
        )
    return violations


# ---------------------------------------------------------------------------
# MANUAL WARNING COMMAND (admins only)
# Usage: reply to the offending member's message with:  /warn spamming links
# ---------------------------------------------------------------------------
async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can use /warn.")
        return

    target_msg = update.message.reply_to_message
    if target_msg is None or target_msg.from_user is None:
        await update.message.reply_text(
            "To warn someone, reply to one of their messages with /warn (optionally add a reason), "
            "e.g.: /warn disrespecting another member"
        )
        return

    target_user = target_msg.from_user
    if target_user.id == context.bot.id:
        await update.message.reply_text("I can't warn myself.")
        return

    reason = " ".join(update.message.text.split(" ")[1:])
    upsert_user(target_user.id, target_user.username, target_user.first_name)
    await apply_violation(context, target_user.id, target_user.first_name, reason)


# ---------------------------------------------------------------------------
# FEATURE 1: SPAM FILTER  +  activity tracking on every message
# ---------------------------------------------------------------------------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.id != GROUP_CHAT_ID:
        return

    upsert_user(user.id, user.username, user.first_name)
    bump_activity(user.id)

    text = (msg.text or msg.caption or "").lower()
    if not text:
        return

    member = await context.bot.get_chat_member(GROUP_CHAT_ID, user.id)
    if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return  # never moderate admins

    is_spam = any(k in text for k in SPAM_KEYWORDS)
    if is_spam:
        try:
            await msg.delete()
        except Exception:
            pass
        await apply_violation(context, user.id, user.first_name, reason="automated spam/scam detection")


# ---------------------------------------------------------------------------
# FEATURE 6: ONE NEWS POST PER DAY (not a flood — just the single freshest item)
# ---------------------------------------------------------------------------
async def post_news_job(context: ContextTypes.DEFAULT_TYPE):
    for feed_url in NEWS_FEED_URLS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            log.error(f"Failed to parse feed {feed_url}: {e}")
            continue

        for entry in parsed.entries[:5]:
            link = entry.get("link")
            if not link or already_posted(link):
                continue
            # Found the first fresh, unposted story — post just this one and stop for today.
            title = entry.get("title", "New update")
            summary = clean_summary(entry.get("summary", ""))
            await context.bot.send_message(
                GROUP_CHAT_ID,
                f"\U0001F4F0 *{title}*\n{summary}\n\n{link}",
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
            mark_posted(link)
            log.info(f"Posted today's single news item: {title}")
            return  # stop entirely — only one post per day, no matter how many feeds/entries remain

    log.info("No fresh news found today — nothing posted.")


# ---------------------------------------------------------------------------
# FEATURE 7: DAILY COMMUNITY CALENDAR — auto-posts today's scheduled activity
# ---------------------------------------------------------------------------
def load_calendar():
    if not os.path.exists(CALENDAR_FILE):
        return {}
    try:
        with open(CALENDAR_FILE) as f:
            entries = json.load(f)
        return {e["date"]: e for e in entries}
    except Exception as e:
        log.error(f"Failed to load {CALENDAR_FILE}: {e}")
        return {}


async def post_daily_calendar_activity(context: ContextTypes.DEFAULT_TYPE):
    calendar = load_calendar()
    today = datetime.now(NIGERIA_TZ).strftime("%Y-%m-%d")
    entry = calendar.get(today)
    if not entry:
        log.info(f"No calendar activity scheduled for {today} — skipping.")
        return

    await context.bot.send_message(
        GROUP_CHAT_ID,
        f"\U0001F4C5 *Today's Community Activity — {entry['day_name']}*\n\n{entry['activity']}",
        parse_mode="Markdown",
    )
    log.info(f"Posted calendar activity for {today}")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lets anyone manually check today's scheduled activity on demand."""
    calendar = load_calendar()
    today = datetime.now(NIGERIA_TZ).strftime("%Y-%m-%d")
    entry = calendar.get(today)
    if not entry:
        await update.message.reply_text("No community activity is scheduled for today.")
        return
    await update.message.reply_text(
        f"\U0001F4C5 *Today's Community Activity — {entry['day_name']}*\n\n{entry['activity']}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# FEATURE 8: ON-DEMAND OPPORTUNITY SCAN — /opportunities (admins only)
# Scans your followed channels for contests/giveaways/airdrops ONLY when an
# admin explicitly asks. Never runs automatically or in the background.
# ---------------------------------------------------------------------------
async def cmd_opportunities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can use /opportunities.")
        return

    if not TELETHON_AVAILABLE or not TG_API_ID or not TG_API_HASH:
        await update.message.reply_text(
            "Opportunity scanning isn't set up yet — it needs a one-time personal account "
            "login (TG_API_ID / TG_API_HASH) and at least one channel in TARGET_CHANNELS."
        )
        return

    if not TARGET_CHANNELS:
        await update.message.reply_text(
            "No channels configured to scan yet. Add channel usernames to TARGET_CHANNELS "
            "(comma-separated, no @) in your Railway variables."
        )
        return

    await update.message.reply_text("\U0001F50D Scanning for opportunities, one moment...")

    client = TelethonClient(PERSONAL_SESSION_NAME, int(TG_API_ID), TG_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await update.message.reply_text(
                "The personal account session isn't authorized yet. Run member_sync.py once "
                "on a computer to log in, then upload the generated session file to your repo."
            )
            return

        cutoff = datetime.utcnow() - timedelta(hours=48)
        found = []
        for channel_username in TARGET_CHANNELS:
            try:
                entity = await client.get_entity(channel_username)
                async for msg in client.iter_messages(entity, limit=20):
                    msg_date = msg.date.replace(tzinfo=None) if msg.date else None
                    if msg_date and msg_date < cutoff:
                        break
                    text = msg.raw_text or ""
                    if opportunity_pattern.search(text):
                        found.append((channel_username, text[:300]))
                        break  # one hit per channel is enough to surface it
            except Exception as e:
                log.error(f"Failed scanning @{channel_username}: {e}")
            if len(found) >= 6:
                break

        if not found:
            await update.message.reply_text("No fresh opportunities found in the last 48 hours.")
            return

        lines = ["\U0001F514 *Opportunities spotted:*\n"]
        for src, text in found:
            lines.append(f"From @{src}:\n{text}\n")
        await update.message.reply_text("\n".join(lines)[:4000], parse_mode="Markdown")

    except Exception as e:
        log.error(f"/opportunities scan failed: {e}")
        await update.message.reply_text("Something went wrong while scanning. Try again shortly.")
    finally:
        if client.is_connected():
            await client.disconnect()


# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
def main():
    init_db()
    load_seed_members()
    application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("myreflink", cmd_myreflink))
    application.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    application.add_handler(CommandHandler("tagall", cmd_tagall))
    application.add_handler(CommandHandler("rules", cmd_rules))
    application.add_handler(CommandHandler("warn", cmd_warn))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("opportunities", cmd_opportunities))
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, on_message))
    application.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    if NEWS_FEED_URLS:
        application.job_queue.run_daily(
            post_news_job,
            time=dtime(hour=NEWS_POST_HOUR, minute=0, tzinfo=NIGERIA_TZ),
        )

    if os.path.exists(CALENDAR_FILE):
        application.job_queue.run_daily(
            post_daily_calendar_activity,
            time=dtime(hour=CALENDAR_POST_HOUR, minute=0, tzinfo=NIGERIA_TZ),
        )

    log.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
