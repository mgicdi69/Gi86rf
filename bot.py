import os
import sqlite3
import logging
from html import escape
from datetime import datetime
from pathlib import Path
import csv
import io
import xml.etree.ElementTree as ET
import zipfile
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton as _TelegramKeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton as _TelegramInlineKeyboardButton
)

try:
    from telegram import CopyTextButton as _TelegramCopyTextButton
except ImportError:
    _TelegramCopyTextButton = None
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)


# ============================================================
# TELEGRAM BUTTON STYLING
# Uses Telegram Bot API button styles: primary (blue),
# success (green), danger (red). Existing button text/callbacks
# remain unchanged; only the visual style is added.
# ============================================================

def _button_style(text):
    """Choose a color style from the existing button label."""
    t = str(text or "").strip().lower()

    # Destructive / negative actions -> red
    danger_words = (
        "delete", "remove", "reject", "cancel", "close", "disable",
        "block", "unblock", "ban", "danger", "clear", "confirm clear",
        "❌", "🗑", "🚫", "⚠️"
    )
    if any(word in t for word in danger_words):
        # Explicit positive confirmations should stay green.
        if "confirm clear" in t:
            return "danger"
        return "danger"

    # Positive / approval actions -> green
    success_words = (
        "approve", "approved", "confirm", "add", "enable", "enabled",
        "save", "submit", "success", "joined", "check joined", "verify",
        "deposit", "buy", "purchase", "pay", "done", "✓", "✅", "➕"
    )
    if any(word in t for word in success_words):
        return "success"

    # Everything else -> blue
    return "primary"


def KeyboardButton(text, **kwargs):
    original = str(text or "")
    clean = _strip_normal_emojis(original)
    if clean != original:
        _BUTTON_ALIASES[clean] = original
    icon = _button_icon(original)
    if icon:
        kwargs.setdefault("icon_custom_emoji_id", icon)
    kwargs.setdefault("style", _button_style(original))
    try:
        return _TelegramKeyboardButton(text=clean, **kwargs)
    except TypeError:
        # Older python-telegram-bot versions may not expose the new Bot API
        # fields as constructor arguments. Keep BOTH fields in api_kwargs so
        # Telegram still receives them instead of silently dropping the
        # Premium Emoji icon.
        style = kwargs.pop("style", None)
        icon_id = kwargs.pop("icon_custom_emoji_id", None)
        api_kwargs = dict(kwargs.pop("api_kwargs", {}) or {})
        if style:
            api_kwargs.setdefault("style", style)
        if icon_id:
            api_kwargs.setdefault("icon_custom_emoji_id", str(icon_id))
        if api_kwargs:
            kwargs["api_kwargs"] = api_kwargs
        return _TelegramKeyboardButton(text=clean, **kwargs)


def InlineKeyboardButton(text, **kwargs):
    original = str(text or "")
    clean = _strip_normal_emojis(original)
    if clean != original:
        _BUTTON_ALIASES[clean] = original
    icon = _button_icon(original)
    if icon:
        kwargs.setdefault("icon_custom_emoji_id", icon)
    kwargs.setdefault("style", _button_style(original))
    try:
        return _TelegramInlineKeyboardButton(text=clean, **kwargs)
    except TypeError:
        # Same compatibility path for inline buttons: never discard the
        # custom emoji ID when the installed PTB version is older.
        style = kwargs.pop("style", None)
        icon_id = kwargs.pop("icon_custom_emoji_id", None)
        api_kwargs = dict(kwargs.pop("api_kwargs", {}) or {})
        if style:
            api_kwargs.setdefault("style", style)
        if icon_id:
            api_kwargs.setdefault("icon_custom_emoji_id", str(icon_id))
        if api_kwargs:
            kwargs["api_kwargs"] = api_kwargs
        return _TelegramInlineKeyboardButton(text=clean, **kwargs)

# ============================================================
# PREMIUM CUSTOM EMOJI ENGINE
# User-supplied IDs: 1-47 + bKash/Nogod.
# Normal emojis are converted to Telegram Premium Custom Emoji entities
# in outgoing text, and button icons use icon_custom_emoji_id.
# ============================================================
PREMIUM_EMOJI_IDS = {
    1: "5325943606647729565",
    2: "6312321827297299611",
    3: "5413664465377847694",
    4: "5852440446051028724",
    5: "6206108815075579644",
    6: "6206375377925839184",
    7: "6206495649895028694",
    8: "5217877998937595307",
    9: "5422536330213088080",
    10: "6206174450765796040",
    11: "6190336264940559752",
    12: "5359681227592854334",
    13: "6206505206197261313",
    14: "5465448495223697750",
    15: "5399967660052081305",
    16: "6113685078825505075",
    17: "5249050854392091366",
    18: "5193125071618594501",
    19: "5325971446625758812",
    20: "5330194932781050507",
    21: "5215313353706057331",
    22: "5370978567834318206",
    23: "5893236738372932548",
    24: "5379742233853451967",
    25: "5875450995332353523",
    26: "5204040271439871033",
    27: "5449446525115575986",
    28: "5341715473882955310",
    29: "5233464138902030789",
    30: "6249244193831524306",
    31: "6206112371308500200",
    32: "4967762670104085632",
    33: "6206378324273403309",
    34: "5454259950798778496",
    35: "5854965264050818921",
    36: "6224470607719829544",
    37: "6206190608432764318",
    38: "5291824687096027834",
    39: "5870972873450984431",
    40: "5765140653029724793",
    41: "5980930633298350051",
    42: "5291873529464122510",
    43: "5429405838345265327",
    44: "5375177250553487549",
    45: "5292013274815028523",
    46: "5293993521026453119",
    47: "6203722870548338074",
    "bkash": "6237975191784266396",
    "nogod": "6235336389647407554",
}
UNICODE_TO_PREMIUM = {
    '❌': PREMIUM_EMOJI_IDS[1],
    '✅': PREMIUM_EMOJI_IDS[2],
    '💰': PREMIUM_EMOJI_IDS[3],
    '💳': PREMIUM_EMOJI_IDS[4],
    '🗑️': PREMIUM_EMOJI_IDS[5],
    '➕': PREMIUM_EMOJI_IDS[6],
    '📞': PREMIUM_EMOJI_IDS[7],
    '📋': PREMIUM_EMOJI_IDS[8],
    '📦': PREMIUM_EMOJI_IDS[9],
    '⚠️': PREMIUM_EMOJI_IDS[10],
    '💵': PREMIUM_EMOJI_IDS[11],
    '🛍️': PREMIUM_EMOJI_IDS[12],
    '🔙': PREMIUM_EMOJI_IDS[13],
    '📝': PREMIUM_EMOJI_IDS[14],
    '📢': PREMIUM_EMOJI_IDS[15],
    '🟢': PREMIUM_EMOJI_IDS[16],
    '👥': PREMIUM_EMOJI_IDS[17],
    '🆔': PREMIUM_EMOJI_IDS[18],
    '👤': PREMIUM_EMOJI_IDS[19],
    '🚫': PREMIUM_EMOJI_IDS[20],
    '🔴': PREMIUM_EMOJI_IDS[21],
    '📥': PREMIUM_EMOJI_IDS[22],
    '📤': PREMIUM_EMOJI_IDS[23],
    '🔗': PREMIUM_EMOJI_IDS[24],
    '📏': PREMIUM_EMOJI_IDS[25],
    '🔘': PREMIUM_EMOJI_IDS[26],
    '📱': PREMIUM_EMOJI_IDS[27],
    '⚙️': PREMIUM_EMOJI_IDS[28],
    '🧾': PREMIUM_EMOJI_IDS[29],
    '🛒': PREMIUM_EMOJI_IDS[30],
    '📩': PREMIUM_EMOJI_IDS[31],
    '🗄️': PREMIUM_EMOJI_IDS[32],
    '🎉': PREMIUM_EMOJI_IDS[33],
    '🔹': PREMIUM_EMOJI_IDS[34],
    '🏷️': PREMIUM_EMOJI_IDS[35],
    '➖': PREMIUM_EMOJI_IDS[36],
    '📌': PREMIUM_EMOJI_IDS[37],
    '🇧🇩': PREMIUM_EMOJI_IDS[38],
    '🔢': PREMIUM_EMOJI_IDS[39],
    '🔓': PREMIUM_EMOJI_IDS[40],
    '✓': PREMIUM_EMOJI_IDS[41],
    '🔐': PREMIUM_EMOJI_IDS[42],
    '🔒': PREMIUM_EMOJI_IDS[43],
    '🌐': PREMIUM_EMOJI_IDS[44],
    '🇩🇪': PREMIUM_EMOJI_IDS[45],
    '🇬🇧': PREMIUM_EMOJI_IDS[46],
    '❓': PREMIUM_EMOJI_IDS[47],
}
UNICODE_TO_PREMIUM.update({"🗑": PREMIUM_EMOJI_IDS[5], "🗄": PREMIUM_EMOJI_IDS[32]})
_PREMIUM_EMOJI_RE = re.compile("|".join(re.escape(x) for x in sorted(UNICODE_TO_PREMIUM, key=len, reverse=True)))
_BUTTON_ALIASES = {}

def _strip_normal_emojis(text):
    return _PREMIUM_EMOJI_RE.sub("", str(text or "")).strip()

def _button_icon(text):
    raw = str(text or "")
    m = _PREMIUM_EMOJI_RE.search(raw)
    if m:
        return UNICODE_TO_PREMIUM.get(m.group(0))
    low = raw.lower()
    if "bkash" in low or "বিকাশ" in raw:
        return PREMIUM_EMOJI_IDS["bkash"]
    if "nogod" in low or "nagad" in low or "নগদ" in raw:
        return PREMIUM_EMOJI_IDS["nogod"]
    return None

def premiumize_html(text):
    if text is None:
        return text
    def repl(m):
        ch=m.group(0)
        eid=UNICODE_TO_PREMIUM[ch]
        return f'<tg-emoji emoji-id="{eid}">{ch}</tg-emoji>'
    return _PREMIUM_EMOJI_RE.sub(repl, str(text))

# ============================================================
# PRODUCT SELLING BOT
# Clean replacement for the old OTP/number-selling logic.
# UI is inspired by the supplied reference video.
# ============================================================

# ============================================================
# 🔐 BOT CONFIGURATION
# এখানে আপনার Bot Token এবং Admin ID বসান
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8903481827:AAF4NrO8El9pSZZsHGOp5bSmfv0qF42KITs")

# আপনার Telegram numeric User ID এখানে বসান
# একাধিক Admin হলে: {123456789, 987654321}
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "6995426618").split(",") if x.strip().isdigit()}

# ============================================================


DB_FILE = os.getenv("DB_FILE", "store.db")

# Free Android SMS Relay -> Railway webhook configuration.
# Keep AUTO_PAYMENT_ENABLED=false until the SMS relay is configured.
AUTO_PAYMENT_ENABLED = os.getenv("AUTO_PAYMENT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
PAYMENT_WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET", "").strip()
PAYMENT_WEBHOOK_HOST = os.getenv("PAYMENT_WEBHOOK_HOST", "0.0.0.0")
PAYMENT_WEBHOOK_PORT = int(os.getenv("PORT", os.getenv("PAYMENT_WEBHOOK_PORT", "8080")))

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "product_files"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Change these to your own payment details.
PAYMENT_INFO = os.getenv(
    "PAYMENT_INFO",
    "💳 Payment Methods\n\n"
    "bKash: YOUR_BKASH_NUMBER\n"
    "Nagad: YOUR_NAGAD_NUMBER\n\n"
    "টাকা পাঠানোর পর আপনার Transaction ID পাঠান।"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ---------------- DATABASE ----------------

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            language TEXT DEFAULT 'bn',
            blocked INTEGER DEFAULT 0,
            referrer_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referral_earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_user_id INTEGER NOT NULL,
            deposit_id INTEGER NOT NULL UNIQUE,
            deposit_amount REAL NOT NULL,
            reward_amount REAL NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Safe migration for existing databases created before referral support.
    try:
        cur.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
    except sqlite3.OperationalError:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    # Each row is one sellable digital item/file.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_name TEXT DEFAULT '',
            content TEXT DEFAULT '',
            row_number INTEGER DEFAULT 0,
            sold INTEGER DEFAULT 0,
            sold_to INTEGER,
            sold_at TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            total REAL NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            payment_method TEXT DEFAULT '',
            trx_id TEXT DEFAULT '',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payment_methods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            account TEXT NOT NULL,
            instructions TEXT DEFAULT '',
            min_amount REAL DEFAULT 10,
            max_amount REAL DEFAULT 50000,
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    # Backward-compatible migration for payment-specific deposit limits.
    for col, definition in [("min_amount", "REAL DEFAULT 10"), ("max_amount", "REAL DEFAULT 50000")]:
        try:
            cur.execute(f"ALTER TABLE payment_methods ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact TEXT NOT NULL,
            description TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS force_join (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL UNIQUE,
            title TEXT DEFAULT '',
            invite_link TEXT DEFAULT '',
            active INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            reply TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            created_at TEXT NOT NULL,
            replied_at TEXT DEFAULT ''
        )
    """)

    defaults = {
        "payment_info": PAYMENT_INFO,
        "support_text": "📞 Support\n\nআপনার সমস্যা লিখে পাঠান। Admin আপনাকে Inbox-এ উত্তর দেবে।",
        "broadcast_last": "",
        "deposit_min": "10",
        "deposit_max": "50000",
    }
    for k, v in defaults.items():
        cur.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (k, v)
        )

    # Safe migrations for databases created by older versions.
    # Safe migrations for row-based stock.
    stock_cols = {row[1] for row in cur.execute("PRAGMA table_info(stock)").fetchall()}
    if "content" not in stock_cols:
        cur.execute("ALTER TABLE stock ADD COLUMN content TEXT DEFAULT ''")
    if "row_number" not in stock_cols:
        cur.execute("ALTER TABLE stock ADD COLUMN row_number INTEGER DEFAULT 0")

    user_cols = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "blocked" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0")

    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(deposits)").fetchall()}
    if "payment_method" not in existing_cols:
        cur.execute("ALTER TABLE deposits ADD COLUMN payment_method TEXT DEFAULT ''")
    if "auto_processed" not in existing_cols:
        cur.execute("ALTER TABLE deposits ADD COLUMN auto_processed INTEGER DEFAULT 0")
    if "auto_processed_at" not in existing_cols:
        cur.execute("ALTER TABLE deposits ADD COLUMN auto_processed_at TEXT DEFAULT ''")

    con.commit()
    con.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_user(user, referrer_id=None):
    con = db()
    existing = con.execute(
        "SELECT user_id, referrer_id FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    if existing:
        con.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (user.username or "", user.first_name or "", user.id)
        )
    else:
        # Never allow self-referral.
        if referrer_id == user.id:
            referrer_id = None
        con.execute("""
            INSERT INTO users(
                user_id, username, first_name, referrer_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            referrer_id,
            now()
        ))

    con.commit()
    con.close()


def get_referrer_id(user_id):
    con = db()
    row = con.execute(
        "SELECT referrer_id FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    con.close()
    return int(row["referrer_id"]) if row and row["referrer_id"] else None


def get_referral_stats(user_id):
    con = db()
    invited = con.execute(
        "SELECT COUNT(*) AS c FROM users WHERE referrer_id=?",
        (user_id,)
    ).fetchone()["c"]
    earned = con.execute(
        "SELECT COALESCE(SUM(reward_amount),0) AS s "
        "FROM referral_earnings WHERE referrer_id=?",
        (user_id,)
    ).fetchone()["s"]
    con.close()
    return int(invited), float(earned)


async def show_referral(update, context):
    uid = update.effective_user.id
    me = await context.bot.get_me()
    bot_username = me.username or ""
    link = f"https://t.me/{bot_username}?start=ref_{uid}" if bot_username else ""

    invited, earned = get_referral_stats(uid)

    description = (
        "🔗 <b>Referral Program</b>\n\n"
        "আপনার Referral Link শেয়ার করে নতুন user আনুন।\n"
        "আপনার Referral দিয়ে কোনো user Deposit করলে "
        "আপনি তার <b>প্রতিটি approved Deposit-এর 10%</b> Referral Income পাবেন।\n\n"
        "💰 এই Referral Income আপনার Balance-এ যোগ হবে এবং "
        "সেই Balance দিয়ে Product কেনা যাবে।\n\n"
        f"👥 Total Referred: <b>{invited}</b>\n"
        f"💵 Total Referral Income: <b>{money(earned)} ৳</b>"
    )

    # Telegram's native copy_text button puts the referral URL in an inline
    # button. The URL is no longer printed as a copyable code line in the text.
    referral_button = None
    if link:
        if _TelegramCopyTextButton is not None:
            referral_button = InlineKeyboardButton(
                "📋 Copy Referral Link",
                copy_text=_TelegramCopyTextButton(text=link)
            )
        else:
            referral_button = InlineKeyboardButton(
                "📋 Copy Referral Link",
                api_kwargs={"copy_text": {"text": link}}
            )

    markup = InlineKeyboardMarkup([[referral_button]]) if referral_button else None
    await update.message.reply_text(
        premiumize_html(description),
        parse_mode="HTML",
        reply_markup=markup
    )


def apply_referral_reward(con, deposit_row):
    """
    Credit 10% of an approved deposit to the original referrer.
    The deposit_id UNIQUE constraint makes the reward idempotent.
    """
    user = con.execute(
        "SELECT referrer_id FROM users WHERE user_id=?",
        (deposit_row["user_id"],)
    ).fetchone()

    if not user or not user["referrer_id"]:
        return 0.0

    referrer_id = int(user["referrer_id"])
    if referrer_id == int(deposit_row["user_id"]):
        return 0.0

    reward = round(float(deposit_row["amount"]) * 0.10, 2)
    if reward <= 0:
        return 0.0

    try:
        con.execute("""
            INSERT INTO referral_earnings(
                referrer_id, referred_user_id, deposit_id,
                deposit_amount, reward_amount, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            referrer_id,
            deposit_row["user_id"],
            deposit_row["id"],
            float(deposit_row["amount"]),
            reward,
            now()
        ))
    except sqlite3.IntegrityError:
        return 0.0

    con.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (reward, referrer_id)
    )
    return reward


def get_balance(user_id):
    con = db()
    row = con.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    con.close()
    return float(row["balance"]) if row else 0.0


def add_balance(user_id, amount):
    con = db()
    con.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id=?",
        (amount, user_id)
    )
    con.commit()
    con.close()


def money(value):
    return f"{float(value):.2f}".rstrip("0").rstrip(".")



def get_setting(key, default=""):
    con = db()
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else default


def set_setting(key, value):
    con = db()
    con.execute("""
        INSERT INTO settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    con.commit()
    con.close()


def get_force_join_channels():
    con = db()
    rows = con.execute("""
        SELECT * FROM force_join WHERE active=1 ORDER BY id ASC
    """).fetchall()
    con.close()
    return rows


def force_join_keyboard(channels):
    rows = []
    for ch in channels:
        if ch["invite_link"]:
            rows.append([InlineKeyboardButton(
                f"📢 {ch['title'] or ch['chat_id']}",
                url=ch["invite_link"]
            )])
    rows.append([InlineKeyboardButton("✅ I Joined — Check Now", callback_data="force_join_check")])
    return InlineKeyboardMarkup(rows)


async def check_force_join(update, context):
    if is_admin(update.effective_user.id):
        return True

    channels = get_force_join_channels()
    if not channels:
        return True

    missing = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(
                chat_id=ch["chat_id"],
                user_id=update.effective_user.id
            )
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            # If the bot cannot inspect the channel, do not hard-block the user.
            continue

    if missing:
        target = update.message or update.callback_query.message
        await target.reply_text(
            premiumize_html("🔒 <b>আগে আমাদের Channel/Group Join করুন</b>\n\n"
            "Join করার পর নিচের <b>✅ I Joined / Joined Check</b> চাপুন।"),
            parse_mode="HTML",
            reply_markup=force_join_keyboard(missing)
        )
        return False
    return True


async def force_join_callback(update, context):
    q = update.callback_query
    await q.answer()

    # Verify every active Force Join channel directly.
    missing = []
    if not is_admin(q.from_user.id):
        for ch in get_force_join_channels():
            try:
                member = await context.bot.get_chat_member(
                    chat_id=ch["chat_id"],
                    user_id=q.from_user.id
                )
                if member.status in ("left", "kicked"):
                    missing.append(ch)
            except Exception:
                # Do not falsely block when Telegram cannot inspect a channel.
                continue

    if missing:
        await q.answer("❌ এখনও সব Channel/Group Join করা হয়নি।", show_alert=True)
        # Keep the current message and refresh only the missing-channel buttons.
        try:
            await q.message.edit_reply_markup(
                reply_markup=force_join_keyboard(missing)
            )
        except Exception:
            pass
        return

    # SUCCESS: remove the Force Join message/UI.
    # Do NOT send a separate verification-success message or a separate
    # "Main Menu" message. After verification, a new user should receive
    # exactly the same welcome message as an existing user who sends /start.
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        try:
            await q.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await context.bot.send_message(
        chat_id=q.from_user.id,
        text=welcome_message(q.from_user),
        parse_mode="HTML",
        reply_markup=main_keyboard(q.from_user.id)
    )


async def support_start(update, context):
    con = db()
    rows = con.execute(
        "SELECT * FROM support_options ORDER BY id"
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            premiumize_html("📞 <b>Support</b>\n\n❌ বর্তমানে কোনো Support চালু নেই।"),
            parse_mode="HTML",
            reply_markup=main_keyboard(update.effective_user.id)
        )
        return

    buttons = []
    description_lines = [
        "📞 <b>Support</b>",
        "",
        "নিচের Support থেকে যোগাযোগ করুন:"
    ]

    for r in rows:
        name = escape(str(r["name"] or "Support"))
        contact = str(r["contact"] or "").strip()
        description = str(r["description"] or "").strip()

        # Telegram inline URL buttons require a valid absolute URL.
        # Convert common @username/plain username formats safely.
        url = contact
        if contact.startswith("@"):
            url = f"https://t.me/{contact[1:]}"
        elif contact and not re.match(r"^https?://", contact, re.I):
            if re.match(r"^[A-Za-z0-9_]{4,64}$", contact):
                url = f"https://t.me/{contact}"
            else:
                url = ""

        if url:
            buttons.append([
                InlineKeyboardButton(f"📞 {name}", url=url)
            ])
        else:
            # Non-URL contacts are shown as callback buttons so they cannot
            # make Telegram reject the entire keyboard.
            buttons.append([
                InlineKeyboardButton(
                    f"📞 {name}",
                    callback_data=f"user_support_contact:{r['id']}"
                )
            ])

        if description:
            description_lines.append(
                f"\n<b>{name}</b>\n{escape(description)}"
            )

    buttons.append([
        InlineKeyboardButton(
            "📝 Message Support",
            callback_data="user_support_ticket"
        )
    ])
    buttons.append([
        InlineKeyboardButton(
            "❌ Close",
            callback_data="user_support_close"
        )
    ])

    await update.message.reply_text(
        premiumize_html("\n".join(description_lines)),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def support_message(update, context, message):
    con = db()
    cur = con.execute("""
        INSERT INTO support_tickets(user_id, message, status, created_at)
        VALUES (?, ?, 'open', ?)
    """, (update.effective_user.id, message, now()))
    ticket_id = cur.lastrowid
    con.commit()
    con.close()

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                premiumize_html(f"📞 <b>NEW SUPPORT TICKET</b>\n\n"
                f"Ticket ID: <code>{ticket_id}</code>\n"
                f"Name: <b>{escape(update.effective_user.full_name or 'User')}</b>\n"
                f"Username: <code>{'@' + escape(update.effective_user.username) if update.effective_user.username else 'Not set'}</code>\n"
                f"User ID: <code>{update.effective_user.id}</code>\n\n"
                f"<b>Message:</b>\n{escape(message)}"),
                parse_mode="HTML"
            )
        except Exception:
            pass

    context.user_data.clear()
    user = update.effective_user
    display_name = escape(user.full_name or "User")
    username = f"@{escape(user.username)}" if user.username else "Not set"

    await update.message.reply_text(
        premiumize_html(f"✅ <b>Support Request Submitted</b>\n\n"
        f"Your support request has been successfully submitted.\n\n"
        f"Ticket ID: <code>{ticket_id}</code>\n"
        f"Name: <b>{display_name}</b>\n"
        f"Username: <code>{username}</code>\n\n"
        f"Our support team will review your message and reply as soon as possible."),
        parse_mode="HTML",
        reply_markup=main_keyboard(update.effective_user.id)
    )


async def support_admin(update, context):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    rows = con.execute("""
        SELECT * FROM support_tickets
        WHERE status='open' ORDER BY id DESC LIMIT 30
    """).fetchall()
    con.close()
    if not rows:
        await update.message.reply_text(premiumize_html("✅ কোনো open support ticket নেই।"), parse_mode="HTML")
        return
    lines = ["📞 <b>OPEN SUPPORT TICKETS</b>\n"]
    for r in rows:
        lines.append(
            f"ID: <code>{r['id']}</code> | User: <code>{r['user_id']}</code>\n"
            f"{r['message']}"
        )
    await update.message.reply_text(premiumize_html("\n\n".join(lines)), parse_mode="HTML")


async def reply_ticket(update, context):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text(premiumize_html("Usage: /reply_ticket ID আপনার উত্তর"), parse_mode="HTML")
        return
    try:
        ticket_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক Ticket ID দিন।"), parse_mode="HTML")
        return
    reply = " ".join(context.args[1:]).strip()

    con = db()
    row = con.execute(
        "SELECT * FROM support_tickets WHERE id=? AND status='open'",
        (ticket_id,)
    ).fetchone()
    if not row:
        con.close()
        await update.message.reply_text(premiumize_html("❌ Ticket পাওয়া যায়নি বা already closed।"), parse_mode="HTML")
        return

    con.execute("""
        UPDATE support_tickets
        SET reply=?, status='closed', replied_at=?
        WHERE id=?
    """, (reply, now(), ticket_id))
    con.execute("""
        INSERT INTO messages(user_id, text, is_read, created_at)
        VALUES (?, ?, 0, ?)
    """, (row["user_id"], f"📞 Support Reply:\\n{reply}", now()))
    con.commit()
    con.close()

    try:
        await context.bot.send_message(
            row["user_id"],
            premiumize_html(f"📞 <b>Support Reply</b>\n\n{reply}\n\n📩 Mail Inbox-এও সংরক্ষণ করা হয়েছে।"),
            parse_mode="HTML"
        )
    except Exception:
        pass

    await update.message.reply_text(premiumize_html("✅ Support reply sent."), parse_mode="HTML")


async def broadcast_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["admin_state"] = "broadcast"
    await update.message.reply_text(
        premiumize_html("📢 যে message সবাইকে পাঠাতে চান সেটি এখন পাঠান।\n\n"
        "Text, Photo, Video, Document, Sticker—যেকোনো message পাঠাতে পারবেন।\n"
        "Premium/Custom Emoji থাকলে সেটিও original entity সহ copy হয়ে যাবে:"),
        reply_markup=back_keyboard()
    , parse_mode="HTML")


async def do_broadcast(update, context):
    """Broadcast the admin's original Telegram message as a copied message.

    copy_message preserves Telegram message entities (including custom/premium
    emoji entities) and also supports photos, videos, documents, stickers,
    captions, formatting, etc. Unlike rebuilding text with send_message, this
    does not convert premium/custom emoji into ordinary emoji.
    """
    if not is_admin(update.effective_user.id):
        return

    source = update.effective_message
    if not source:
        return

    con = db()
    users = con.execute("SELECT user_id FROM users").fetchall()
    con.close()

    sent = failed = 0
    source_chat_id = update.effective_chat.id
    source_message_id = source.message_id

    for row in users:
        try:
            await context.bot.copy_message(
                chat_id=row["user_id"],
                from_chat_id=source_chat_id,
                message_id=source_message_id
            )
            sent += 1
        except Exception:
            failed += 1

    context.user_data.clear()
    set_setting("broadcast_last", now())
    await update.message.reply_text(
        premiumize_html("📢 Broadcast complete.\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n\n"
        "ℹ️ Messageটি copy/forward-style পাঠানো হয়েছে, তাই Telegram-এর "
        "original formatting ও Premium/Custom Emoji entities সংরক্ষিত থাকবে।"),
        reply_markup=admin_keyboard()
    , parse_mode="HTML")


async def product_management(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        premiumize_html("📦 <b>PRODUCT MANAGEMENT</b>\n\nSelect an option:"),
        parse_mode="HTML",
        reply_markup=product_management_keyboard()
    )


async def force_join_management(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        premiumize_html("📢 <b>FORCE JOIN MANAGEMENT</b>\n\nSelect an option:"),
        parse_mode="HTML",
        reply_markup=force_join_management_keyboard()
    )


async def force_join_admin(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        premiumize_html("📢 <b>FORCE JOIN MANAGEMENT</b>\n\nSelect an option:"),
        parse_mode="HTML",
        reply_markup=force_join_management_keyboard()
    )


async def force_join_add_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["admin_state"] = "force_join_add_chat_id"
    await update.message.reply_text(
        premiumize_html("➕ <b>Add Channel</b>\n\n<b>ধাপ ১/৩</b>\nপ্রথমে <b>Chat ID</b> দিন:\n\nউদাহরণ: <code>-1001234567890</code>"),
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )


async def force_join_add_chat_id(update, context):
    chat_id = (update.message.text or "").strip()
    if not chat_id:
        await update.message.reply_text(premiumize_html("❌ Chat ID দিন।"), parse_mode="HTML")
        return
    context.user_data["force_join_chat_id"] = chat_id
    context.user_data["admin_state"] = "force_join_add_title"
    await update.message.reply_text(
        premiumize_html("📝 <b>ধাপ ২/৩</b>\nএখন <b>Channel Name</b> দিন:"),
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )


async def force_join_add_title(update, context):
    title = (update.message.text or "").strip()
    if not title:
        await update.message.reply_text(premiumize_html("❌ Channel Name দিন।"), parse_mode="HTML")
        return
    context.user_data["force_join_title"] = title
    context.user_data["admin_state"] = "force_join_add_invite"
    await update.message.reply_text(
        premiumize_html("🔗 <b>ধাপ ৩/৩</b>\nএখন <b>Invite Link</b> দিন:"),
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )


async def force_join_add_invite(update, context):
    invite = (update.message.text or "").strip()
    if not invite:
        await update.message.reply_text(premiumize_html("❌ Invite Link দিন।"), parse_mode="HTML")
        return
    chat_id = context.user_data.get("force_join_chat_id")
    title = context.user_data.get("force_join_title")
    con = db()
    try:
        con.execute(
            "INSERT INTO force_join(chat_id, title, invite_link, active) VALUES (?, ?, ?, 1)",
            (chat_id, title, invite)
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        context.user_data.clear()
        await update.message.reply_text(premiumize_html("❌ এই Chat ID already added।"), reply_markup=force_join_management_keyboard(), parse_mode="HTML")
        return
    con.close()
    context.user_data.clear()
    await update.message.reply_text(premiumize_html("✅ Force Join channel added."), reply_markup=force_join_management_keyboard(), parse_mode="HTML")


async def force_join_remove_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    rows = con.execute("SELECT id, title, chat_id FROM force_join ORDER BY id DESC").fetchall()
    con.close()
    if not rows:
        await update.message.reply_text(premiumize_html("❌ কোনো Force Join channel নেই."), reply_markup=force_join_management_keyboard(), parse_mode="HTML")
        return
    lines = ["🗑️ <b>Remove Channel</b>\n"]
    for r in rows:
        lines.append(f"ID: <code>{r['id']}</code> | {escape(r['title'] or '')} | <code>{escape(str(r['chat_id']))}</code>")
    context.user_data["admin_state"] = "force_join_remove"
    await update.message.reply_text(
        premiumize_html("\n".join(lines) + "\n\nযে ID delete করতে চান শুধু সেই ID পাঠান।"),
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )


async def force_join_remove_save(update, context):
    try:
        fid = int((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক Channel ID দিন।"), parse_mode="HTML")
        return
    con = db()
    cur = con.execute("DELETE FROM force_join WHERE id=?", (fid,))
    con.commit()
    con.close()
    context.user_data.clear()
    await update.message.reply_text(
        premiumize_html("✅ Force Join channel removed." if cur.rowcount else "❌ ID not found."),
        reply_markup=force_join_management_keyboard()
    , parse_mode="HTML")


async def force_join_view_list(update, context):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    rows = con.execute("SELECT * FROM force_join ORDER BY id DESC").fetchall()
    con.close()
    lines = ["📋 <b>FORCE JOIN CHANNEL LIST</b>\n"]
    if not rows:
        lines.append("কোনো channel configured নেই।")
    else:
        for r in rows:
            lines.append(
                f"ID: <code>{r['id']}</code>\n"
                f"Name: {escape(r['title'] or '')}\n"
                f"Chat ID: <code>{escape(str(r['chat_id']))}</code>\n"
                f"Status: {'🟢 Active' if r['active'] else '🔴 Disabled'}\n"
                f"Link: {escape(r['invite_link'] or '')}"
            )
    await update.message.reply_text(premiumize_html("\n\n".join(lines)), parse_mode="HTML", reply_markup=force_join_management_keyboard())


def payment_methods_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ Add Payment Method")],
        [KeyboardButton("🗑️ Delete Payment Method")],
        [KeyboardButton("🔘 Enable / Disable Payment Method")],
        [KeyboardButton("📋 Payment Methods")],
        [KeyboardButton("🔙 Return")],
    ], resize_keyboard=True)



def support_management_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ Add Support")],
        [KeyboardButton("🗑️ Delete Support")],
        [KeyboardButton("📋 Support List")],
        [KeyboardButton("Pending Support")],
        [KeyboardButton("🔙 Return")]
    ], resize_keyboard=True)


async def support_management(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(
        premiumize_html("📞 <b>SUPPORT MANAGEMENT</b>\n\nManage support options:"),
        parse_mode="HTML", reply_markup=support_management_keyboard()
    )


async def support_list(update, context):
    if not is_admin(update.effective_user.id): return
    con=db()
    rows=con.execute("SELECT * FROM support_options ORDER BY id").fetchall()
    con.close()
    if not rows:
        await update.message.reply_text(premiumize_html("📋 No support options."), reply_markup=support_management_keyboard(), parse_mode="HTML")
        return
    lines=["📋 <b>SUPPORT LIST</b>\n"]
    for r in rows:
        lines.append(f"🆔 {r['id']} | <b>{r['name']}</b>\n📞 <code>{r['contact']}</code>\n📌 {'🟢 Enabled' if r['enabled'] else '🔴 Disabled'}\n📝 {r['description'] or '—'}")
    await update.message.reply_text(premiumize_html("\n\n".join(lines)),parse_mode="HTML",reply_markup=support_management_keyboard())


async def add_support_start(update, context):
    if not is_admin(update.effective_user.id): return
    context.user_data["admin_state"]="support_name"
    await update.message.reply_text(premiumize_html("➕ <b>Add Support</b>\n\nSupport-এর নাম লিখুন:"),parse_mode="HTML",reply_markup=back_keyboard())


async def add_support_name(update, context):
    context.user_data["new_support_name"]=update.message.text.strip()
    context.user_data["admin_state"]="support_contact"
    await update.message.reply_text(premiumize_html("📞 Support username/link/contact দিন:"), parse_mode="HTML")


async def add_support_contact(update, context):
    context.user_data["new_support_contact"]=update.message.text.strip()
    context.user_data["admin_state"]="support_description"
    await update.message.reply_text(premiumize_html("📝 Support-এর description লিখুন:"), parse_mode="HTML")


async def add_support_description(update, context):
    con=db()
    con.execute("INSERT INTO support_options(name,contact,description,enabled,created_at) VALUES (?,?,?,1,?)",
                (context.user_data["new_support_name"],context.user_data["new_support_contact"],update.message.text.strip(),now()))
    con.commit(); con.close()
    context.user_data.clear()
    await update.message.reply_text(premiumize_html("✅ Support added."),reply_markup=support_management_keyboard(), parse_mode="HTML")


async def delete_support_start(update, context):
    con=db(); rows=con.execute("SELECT id,name FROM support_options ORDER BY id").fetchall(); con.close()
    if not rows:
        await update.message.reply_text(premiumize_html("❌ No support options."), parse_mode="HTML"); return
    buttons=[[InlineKeyboardButton(f"🗑️ {r['id']} - {r['name']}",callback_data=f"support_delete_confirm:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("❌ Cancel",callback_data="support_cancel")])
    await update.message.reply_text(premiumize_html("🗑️ <b>Delete Support</b>\n\nSelect one:"),parse_mode="HTML",reply_markup=InlineKeyboardMarkup(buttons))


async def toggle_support_start(update, context):
    con=db(); rows=con.execute("SELECT id,name,enabled FROM support_options ORDER BY id").fetchall(); con.close()
    if not rows:
        await update.message.reply_text(premiumize_html("❌ No support options."), parse_mode="HTML"); return
    buttons=[[InlineKeyboardButton(f"{'🟢' if r['enabled'] else '🔴'} {r['id']} - {r['name']}",callback_data=f"support_toggle:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("❌ Cancel",callback_data="support_cancel")])
    await update.message.reply_text(premiumize_html("🔘 <b>Enable / Disable Support</b>"),parse_mode="HTML",reply_markup=InlineKeyboardMarkup(buttons))


async def pending_support(update, context):
    if not is_admin(update.effective_user.id):
        return

    con = db()
    rows = con.execute("""
        SELECT * FROM support_tickets
        WHERE status='open'
        ORDER BY id DESC
        LIMIT 50
    """).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            premiumize_html("No pending support message."),
            reply_markup=support_management_keyboard()
        , parse_mode="HTML")
        return

    for r in rows:
        message_text = escape(str(r["message"] or ""))
        reply_text = str(r["reply"] or "").strip()
        reply_part = f"\n\n<b>Previous Reply:</b>\n{escape(reply_text)}" if reply_text else ""
        text = (
            f"<b>Pending Support #{r['id']}</b>\n\n"
            f"User ID: <code>{r['user_id']}</code>\n"
            f"Date: <code>{r['created_at']}</code>\n\n"
            f"<b>Message:</b>\n{message_text}"
            f"{reply_part}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Reply", callback_data=f"support_ticket_reply:{r['id']}"),
            InlineKeyboardButton("Close", callback_data=f"support_ticket_close:{r['id']}")
        ]])
        await update.message.reply_text(premiumize_html(text), parse_mode="HTML", reply_markup=kb)


async def support_ticket_reply_start(update, context, ticket_id):
    con = db()
    row = con.execute(
        "SELECT * FROM support_tickets WHERE id=? AND status='open'",
        (ticket_id,)
    ).fetchone()
    con.close()
    if not row:
        await update.callback_query.edit_message_text(premiumize_html("Support ticket not found or already closed."), parse_mode="HTML")
        return

    context.user_data["admin_state"] = "support_reply"
    context.user_data["support_reply_ticket_id"] = ticket_id
    await update.callback_query.edit_message_text(
        premiumize_html(f"<b>Reply to Support #{ticket_id}</b>\n\n"
        "এখন আপনার reply message পাঠান।\n"
        "Reply পাঠানোর পর ticket Pending Support-এ open থাকবে; প্রয়োজন হলে Close চাপুন।"),
        parse_mode="HTML"
    )


async def support_ticket_reply_save(update, context):
    if not is_admin(update.effective_user.id):
        return
    ticket_id = context.user_data.get("support_reply_ticket_id")
    reply = (update.message.text or "").strip()
    if not ticket_id or not reply:
        await update.message.reply_text(premiumize_html("সঠিক reply message দিন।"), parse_mode="HTML")
        return

    con = db()
    row = con.execute(
        "SELECT * FROM support_tickets WHERE id=? AND status='open'",
        (ticket_id,)
    ).fetchone()
    if not row:
        con.close()
        context.user_data.clear()
        await update.message.reply_text(premiumize_html("Support ticket not found or already closed."), reply_markup=support_management_keyboard(), parse_mode="HTML")
        return

    con.execute(
        "UPDATE support_tickets SET reply=?, replied_at=?, status='closed' WHERE id=?",
        (reply, now(), ticket_id)
    )
    con.execute(
        "INSERT INTO messages(user_id, text, is_read, created_at) VALUES (?, ?, 0, ?)",
        (row["user_id"], f"Support Reply:\n{reply}", now())
    )
    con.commit()
    con.close()

    try:
        await context.bot.send_message(
            row["user_id"],
            premiumize_html(f"<b>Support Reply</b>\n\n{escape(reply)}"),
            parse_mode="HTML"
        )
    except Exception:
        pass

    context.user_data.clear()
    await update.message.reply_text(
        premiumize_html(f"Reply sent to Support #{ticket_id}.\nTicket has been closed and removed from Pending Support."),
        reply_markup=support_management_keyboard()
    , parse_mode="HTML")


async def support_ticket_close(update, context, ticket_id):
    q = update.callback_query
    con = db()
    row = con.execute(
        "SELECT * FROM support_tickets WHERE id=? AND status='open'",
        (ticket_id,)
    ).fetchone()
    if not row:
        con.close()
        await q.edit_message_text(premiumize_html("Support ticket not found or already closed."), parse_mode="HTML")
        return

    con.execute(
        "UPDATE support_tickets SET status='closed', replied_at=? WHERE id=?",
        (now(), ticket_id)
    )
    con.commit()
    con.close()
    await q.edit_message_text(premiumize_html(f"Support #{ticket_id} closed."), parse_mode="HTML")


async def support_admin_callback(update, context):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    data=q.data or ""
    if data.startswith("support_ticket_reply:"):
        try:
            ticket_id = int(data.split(":", 1)[1])
        except ValueError:
            await q.edit_message_text(premiumize_html("Invalid support ticket ID."), parse_mode="HTML")
            return
        await support_ticket_reply_start(update, context, ticket_id)
        return

    if data.startswith("support_ticket_close:"):
        try:
            ticket_id = int(data.split(":", 1)[1])
        except ValueError:
            await q.edit_message_text(premiumize_html("Invalid support ticket ID."), parse_mode="HTML")
            return
        await support_ticket_close(update, context, ticket_id)
        return

    if data=="support_cancel":
        await q.edit_message_text(premiumize_html("❌ Cancelled."), parse_mode="HTML"); return
    if data.startswith("support_delete_confirm:"):
        sid=int(data.split(":")[1]); con=db()
        row=con.execute("SELECT * FROM support_options WHERE id=?",(sid,)).fetchone(); con.close()
        if not row: await q.edit_message_text(premiumize_html("❌ Support not found."), parse_mode="HTML"); return
        kb=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm Delete",callback_data=f"support_delete:{sid}"),
            InlineKeyboardButton("❌ Cancel",callback_data="support_cancel")
        ]])
        await q.edit_message_text(premiumize_html(f"⚠️ Delete <b>{row['name']}</b>?"),parse_mode="HTML",reply_markup=kb); return
    if data.startswith("support_delete:"):
        sid=int(data.split(":")[1]); con=db()
        con.execute("DELETE FROM support_options WHERE id=?",(sid,)); con.commit(); con.close()
        await q.edit_message_text(premiumize_html("✅ Support deleted."), parse_mode="HTML"); return
    if data.startswith("support_toggle:"):
        sid=int(data.split(":")[1]); con=db()
        row=con.execute("SELECT enabled,name FROM support_options WHERE id=?",(sid,)).fetchone()
        if not row: con.close(); await q.edit_message_text(premiumize_html("❌ Support not found."), parse_mode="HTML"); return
        new=0 if row["enabled"] else 1
        con.execute("UPDATE support_options SET enabled=? WHERE id=?",(new,sid)); con.commit(); con.close()
        await q.edit_message_text(premiumize_html(f"✅ <b>{row['name']}</b>\nStatus: {'🟢 Enabled' if new else '🔴 Disabled'}"),parse_mode="HTML")


async def user_support(update, context):
    await support_start(update, context)


async def payment_management(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        premiumize_html("💳 <b>PAYMENT MANAGEMENT</b>\n\n"
        "Payment method add, delete এবং enable/disable এখান থেকে করুন।"),
        parse_mode="HTML",
        reply_markup=payment_methods_keyboard()
    )


async def payment_methods_list(update, context):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    rows = con.execute(
        "SELECT * FROM payment_methods ORDER BY id ASC"
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            premiumize_html("📋 কোনো payment method নেই।"),
            reply_markup=payment_methods_keyboard()
        , parse_mode="HTML")
        return

    lines = ["📋 <b>PAYMENT METHODS</b>\n"]
    for r in rows:
        status = "🟢 Enabled" if r["enabled"] else "🔴 Disabled"
        lines.append(
            f"🆔 <b>{r['id']}</b> | <b>{r['name']}</b>\n"
            f"💳 Account: <code>{r['account']}</code>\n"
            f"📏 Deposit: {money(r['min_amount'])} ৳ - {money(r['max_amount'])} ৳\n"
            f"📌 Status: {status}\n"
            f"📝 {r['instructions'] or 'No extra instruction'}"
        )
    await update.message.reply_text(
        premiumize_html("\n\n".join(lines)),
        parse_mode="HTML",
        reply_markup=payment_methods_keyboard()
    )


async def add_payment_method_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["admin_state"] = "payment_method_name"
    await update.message.reply_text(
        premiumize_html("➕ <b>Add Payment Method</b>\n\n"
        "Payment method-এর নাম লিখুন।\nউদাহরণ: bKash"),
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )


async def add_payment_method_name(update, context):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text(premiumize_html("❌ নাম খালি রাখা যাবে না।"), parse_mode="HTML")
        return
    context.user_data["new_payment_name"] = name
    context.user_data["admin_state"] = "payment_method_account"
    await update.message.reply_text(
        premiumize_html("💳 Account number / payment address লিখুন:")
    , parse_mode="HTML")


async def add_payment_method_account(update, context):
    account = update.message.text.strip()
    if not account:
        await update.message.reply_text(premiumize_html("❌ Account information দিন।"), parse_mode="HTML")
        return
    context.user_data["new_payment_account"] = account
    context.user_data["admin_state"] = "payment_method_min"
    await update.message.reply_text(premiumize_html("📏 Minimum Deposit amount লিখুন:"), parse_mode="HTML")


async def add_payment_method_min(update, context):
    try:
        value = float(update.message.text.strip())
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক minimum amount দিন।"), parse_mode="HTML")
        return
    context.user_data["new_payment_min"] = value
    context.user_data["admin_state"] = "payment_method_max"
    await update.message.reply_text(premiumize_html("📏 Maximum Deposit amount লিখুন:"), parse_mode="HTML")


async def add_payment_method_max(update, context):
    try:
        value = float(update.message.text.strip())
        minimum = float(context.user_data.get("new_payment_min", 0))
        if value < minimum:
            raise ValueError
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ Maximum অবশ্যই Minimum-এর সমান বা বেশি হতে হবে।"), parse_mode="HTML")
        return
    context.user_data["new_payment_max"] = value
    context.user_data["admin_state"] = "payment_method_instructions"
    await update.message.reply_text(premiumize_html("📝 Payment instructions লিখুন।\nযেমন: Send Money করার পর Transaction ID দিন।"), parse_mode="HTML")


async def add_payment_method_instructions(update, context):
    name = context.user_data.get("new_payment_name")
    account = context.user_data.get("new_payment_account")
    instructions = update.message.text.strip()
    minimum = float(context.user_data.get("new_payment_min", 10))
    maximum = float(context.user_data.get("new_payment_max", 50000))

    con = db()
    con.execute("""
        INSERT INTO payment_methods(name, account, instructions, min_amount, max_amount, enabled, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
    """, (name, account, instructions, minimum, maximum, now()))
    con.commit()
    con.close()

    context.user_data.clear()
    await update.message.reply_text(
        premiumize_html(f"✅ <b>Payment Method Added</b>\n\n"
        f"💳 Method: <b>{name}</b>\n"
        f"📱 Account: <code>{account}</code>\n"
        f"📏 Min: {money(minimum)} ৳ | Max: {money(maximum)} ৳\n"
        f"🟢 Status: Enabled"),
        parse_mode="HTML",
        reply_markup=payment_methods_keyboard()
    )


async def delete_payment_method_start(update, context):
    if not is_admin(update.effective_user.id):
        return

    con = db()
    rows = con.execute(
        "SELECT id, name, account, enabled FROM payment_methods ORDER BY id ASC"
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(premiumize_html("❌ কোনো payment method নেই."), parse_mode="HTML")
        return

    buttons = []
    for r in rows:
        buttons.append([InlineKeyboardButton(
            f"🗑️ {r['id']} - {r['name']}",
            callback_data=f"payment_delete_confirm:{r['id']}"
        )])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="payment_cancel")])

    await update.message.reply_text(
        premiumize_html("🗑️ <b>Delete Payment Method</b>\n\n"
        "যে method delete করতে চান সেটি নির্বাচন করুন:"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def toggle_payment_method_start(update, context):
    if not is_admin(update.effective_user.id):
        return

    con = db()
    rows = con.execute(
        "SELECT id, name, enabled FROM payment_methods ORDER BY id ASC"
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(premiumize_html("❌ কোনো payment method নেই."), parse_mode="HTML")
        return

    buttons = []
    for r in rows:
        status = "🟢 Enabled" if r["enabled"] else "🔴 Disabled"
        buttons.append([InlineKeyboardButton(
            f"{status} {r['id']} - {r['name']}",
            callback_data=f"payment_toggle:{r['id']}"
        )])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="payment_cancel")])

    await update.message.reply_text(
        premiumize_html("🔘 <b>Enable / Disable Payment Method</b>\n\n"
        "একটি method নির্বাচন করুন:"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def admin_payment_callback(update, context):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    data = query.data or ""

    if data == "payment_cancel":
        await query.edit_message_text(premiumize_html("❌ Cancelled."), parse_mode="HTML")
        return

    if data.startswith("payment_delete_confirm:"):
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await query.edit_message_text(premiumize_html("❌ Invalid payment method."), parse_mode="HTML")
            return

        con = db()
        row = con.execute(
            "SELECT * FROM payment_methods WHERE id=?", (pid,)
        ).fetchone()
        con.close()

        if not row:
            await query.edit_message_text(premiumize_html("❌ Payment method not found."), parse_mode="HTML")
            return

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "✅ Confirm Delete",
                callback_data=f"payment_delete:{pid}"
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="payment_cancel"
            )
        ]])
        await query.edit_message_text(
            premiumize_html(f"⚠️ <b>Confirm Delete</b>\n\n"
            f"💳 {row['name']}\n"
            f"📱 <code>{row['account']}</code>\n\n"
            "এই payment method delete করবেন?"),
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    if data.startswith("payment_delete:"):
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await query.edit_message_text(premiumize_html("❌ Invalid payment method."), parse_mode="HTML")
            return

        con = db()
        cur = con.execute(
            "DELETE FROM payment_methods WHERE id=?", (pid,)
        )
        con.commit()
        con.close()

        await query.edit_message_text(
            premiumize_html("✅ Payment method deleted."
            if cur.rowcount else
            "❌ Payment method not found.")
        , parse_mode="HTML")
        return

    if data.startswith("payment_toggle:"):
        try:
            pid = int(data.split(":", 1)[1])
        except ValueError:
            await query.edit_message_text(premiumize_html("❌ Invalid payment method."), parse_mode="HTML")
            return

        con = db()
        row = con.execute(
            "SELECT * FROM payment_methods WHERE id=?", (pid,)
        ).fetchone()
        if not row:
            con.close()
            await query.edit_message_text(premiumize_html("❌ Payment method not found."), parse_mode="HTML")
            return

        new_status = 0 if row["enabled"] else 1
        con.execute(
            "UPDATE payment_methods SET enabled=? WHERE id=?",
            (new_status, pid)
        )
        con.commit()
        con.close()

        status_text = "🟢 Enabled" if new_status else "🔴 Disabled"
        await query.edit_message_text(
            premiumize_html(f"✅ <b>{row['name']}</b>\n\nStatus: {status_text}"),
            parse_mode="HTML"
        )
        return


async def deposit_limits_admin(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["admin_state"] = "deposit_min"
    await update.message.reply_text(
        premiumize_html(f"📏 <b>Deposit Limits</b>\n\n"
        f"Current Min: <b>{money(get_setting('deposit_min','10'))} ৳</b>\n"
        f"Current Max: <b>{money(get_setting('deposit_max','50000'))} ৳</b>\n\n"
        "নতুন Minimum amount লিখুন:"),
        parse_mode="HTML", reply_markup=back_keyboard()
    )


async def deposit_min_admin(update, context):
    try:
        value = float(update.message.text.strip())
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক minimum amount দিন।"), parse_mode="HTML")
        return
    context.user_data["new_deposit_min"] = value
    context.user_data["admin_state"] = "deposit_max"
    await update.message.reply_text(premiumize_html("Maximum amount লিখুন:"), parse_mode="HTML")


async def deposit_max_admin(update, context):
    try:
        value = float(update.message.text.strip())
        minimum = float(context.user_data.get("new_deposit_min", 0))
        if value < minimum:
            raise ValueError
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ Maximum amount অবশ্যই Minimum-এর সমান বা বেশি হতে হবে।"), parse_mode="HTML")
        return
    minimum = float(context.user_data["new_deposit_min"])
    set_setting("deposit_min", str(minimum))
    set_setting("deposit_max", str(value))
    context.user_data.clear()
    await update.message.reply_text(
        premiumize_html(f"✅ Deposit limit updated.\n\nMin: {money(minimum)} ৳\nMax: {money(value)} ৳"),
        reply_markup=admin_keyboard()
    , parse_mode="HTML")


async def settings_admin(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        premiumize_html("⚙️ <b>SETTINGS</b>\n\n"
        f"Payment: {'Configured' if get_setting('payment_info') else 'Not configured'}\n"
        f"Force Join: {len(get_force_join_channels())} active\n"
        f"Last Broadcast: {get_setting('broadcast_last', 'Never')}\n\n"
        "Commands:\n"
        "Add Channel বাটন ব্যবহার করে ধাপে ধাপে Channel যোগ করুন।\n"
        "Remove Channel বাটন ব্যবহার করে Channel মুছুন।"),
        parse_mode="HTML"
    )


async def user_management(update, context):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    rows = con.execute("""
        SELECT user_id, username, first_name, balance, created_at
        FROM users ORDER BY created_at DESC LIMIT 50
    """).fetchall()
    con.close()
    if not rows:
        await update.message.reply_text(premiumize_html("কোনো user নেই।"), parse_mode="HTML")
        return
    lines = ["👥 <b>USER MANAGEMENT</b>\n"]
    for r in rows:
        lines.append(
            f"ID: <code>{r['user_id']}</code>\n"
            f"Name: {r['first_name']} @{r['username']}\n"
            f"Balance: {money(r['balance'])} ৳"
        )
    await update.message.reply_text(premiumize_html("\n\n".join(lines)), parse_mode="HTML")



async def database_admin(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        premiumize_html("🗄️ <b>DATABASE MANAGEMENT</b>\n\n"
        "Database backup, restore এবং data management এখান থেকে করুন।\n\n"
        "⚠️ Clear All Data ব্যবহার করার আগে অবশ্যই database download করে backup রাখুন।"),
        parse_mode="HTML",
        reply_markup=database_keyboard()
    )


def database_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📥 Download Database"), KeyboardButton("📤 Upload Database")],
        [KeyboardButton("📥 Download user.txt"), KeyboardButton("📤 Upload user.txt")],
        [KeyboardButton("🗑️ Clear All Data")],
        [KeyboardButton("🔙 Return")],
    ], resize_keyboard=True)


async def database_download(update, context):
    if not is_admin(update.effective_user.id):
        return
    path = Path(DB_FILE)
    if not path.exists():
        await update.message.reply_text(premiumize_html("❌ Database file পাওয়া যায়নি।"), parse_mode="HTML")
        return
    try:
        await update.message.reply_document(
            document=path.open("rb"),
            caption=premiumize_html("📥 <b>Database Backup</b>\n\nCurrent database downloaded successfully."),
            parse_mode="HTML"
        )
    except Exception:
        logger.exception("Database download failed")
        await update.message.reply_text(premiumize_html("❌ Database download failed."), parse_mode="HTML")


async def database_upload_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["admin_state"] = "database_upload"
    await update.message.reply_text(
        premiumize_html("📤 <b>Upload Database</b>\n\n"
        "একটি valid SQLite <code>.db</code> file পাঠান।\n\n"
        "⚠️ Upload করলে বর্তমান database replace হবে।"),
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )


async def database_upload(update, context):
    if not is_admin(update.effective_user.id):
        return
    document = update.message.document
    if not document:
        await update.message.reply_text(premiumize_html("❌ Database file হিসেবে document পাঠান।"), parse_mode="HTML")
        return

    filename = (document.file_name or "").lower()
    if not filename.endswith((".db", ".sqlite", ".sqlite3")):
        await update.message.reply_text(premiumize_html("❌ শুধু .db / .sqlite / .sqlite3 file upload করুন।"), parse_mode="HTML")
        return

    tmp = Path(f"{DB_FILE}.upload_tmp")
    try:
        tg_file = await document.get_file()
        await tg_file.download_to_drive(custom_path=str(tmp))

        # Validate that the uploaded file is a readable SQLite database.
        test = sqlite3.connect(str(tmp))
        test.execute("PRAGMA schema_version").fetchone()
        test.close()

        # Keep a safety backup before replacement.
        current = Path(DB_FILE)
        if current.exists():
            backup = Path(f"{DB_FILE}.before_upload_backup")
            backup.write_bytes(current.read_bytes())

        tmp.replace(current)
        init_db()

        context.user_data.clear()
        await update.message.reply_text(
            premiumize_html("✅ <b>Database uploaded successfully.</b>\n\n"
            "নতুন database active হয়েছে।"),
            parse_mode="HTML",
            reply_markup=admin_keyboard()
        )
    except Exception:
        logger.exception("Database upload failed")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        await update.message.reply_text(
            premiumize_html("❌ Invalid database বা upload failed। Current database অপরিবর্তিত আছে।")
        , parse_mode="HTML")


async def download_user_txt(update, context):
    if not is_admin(update.effective_user.id): return
    con=db(); rows=con.execute("SELECT user_id,username,first_name,balance,blocked FROM users ORDER BY user_id").fetchall(); con.close()
    path=Path("user.txt")
    with path.open("w", encoding="utf-8") as f:
        f.write("user_id | username | name | balance | blocked\n")
        for r in rows:
            f.write(f"{r['user_id']} | {r['username'] or ''} | {r['first_name'] or ''} | {r['balance']} | {r['blocked']}\n")
    await update.message.reply_document(document=path.open("rb"), caption=premiumize_html("📥 user.txt"), parse_mode="HTML")

async def upload_user_txt_start(update, context):
    if not is_admin(update.effective_user.id): return
    context.user_data["admin_state"]="user_txt_upload"
    await update.message.reply_text(premiumize_html("📤 user.txt file পাঠান। এটি users table replace করবে না; matching User ID-এর তথ্য update করবে।"), reply_markup=back_keyboard(), parse_mode="HTML")

async def upload_user_txt(update, context):
    if not is_admin(update.effective_user.id) or not update.message.document: return
    doc=update.message.document
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text(premiumize_html("❌ শুধু user.txt file পাঠান।"), parse_mode="HTML"); return
    tmp=Path("user_upload.txt"); tg=await doc.get_file(); await tg.download_to_drive(custom_path=str(tmp))
    try:
        con=db(); updated=0
        for line in tmp.read_text(encoding="utf-8-sig").splitlines()[1:]:
            parts=[x.strip() for x in line.split("|",4)]
            if len(parts)<5: continue
            uid,username,name,balance,blocked=parts
            try: uid=int(uid); balance=float(balance); blocked=int(blocked)
            except ValueError: continue
            cur=con.execute("UPDATE users SET username=?,first_name=?,balance=?,blocked=? WHERE user_id=?",(username or None,name or "User",balance,blocked,uid))
            updated += cur.rowcount
        con.commit(); con.close(); context.user_data.clear()
        await update.message.reply_text(premiumize_html(f"✅ user.txt processed.\nUpdated users: {updated}"), reply_markup=database_keyboard(), parse_mode="HTML")
    except Exception:
        logger.exception("user.txt upload failed"); await update.message.reply_text(premiumize_html("❌ user.txt upload failed."), parse_mode="HTML")
    finally:
        tmp.unlink(missing_ok=True)

async def clear_all_data_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["admin_state"] = "clear_all_data_confirm"
    await update.message.reply_text(
        premiumize_html("⚠️ <b>DANGER ZONE</b>\n\n"
        "এটি Users, Products, Stock, Orders, Deposits, Messages, "
        "Support Tickets এবং Settings-এর data মুছে দেবে।\n\n"
        "এই কাজটি undo করা যাবে না।\n\n"
        "নিশ্চিত হলে নিচের <b>⚠️ CONFIRM CLEAR ALL DATA</b> চাপুন।"),
        parse_mode="HTML",
        reply_markup=clear_data_keyboard()
    )


def clear_data_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("⚠️ CONFIRM CLEAR ALL DATA")],
        [KeyboardButton("❌ Cancel")],
    ], resize_keyboard=True)


async def clear_all_data_confirm(update, context):
    if not is_admin(update.effective_user.id):
        return
    if update.message.text != "⚠️ CONFIRM CLEAR ALL DATA":
        return

    try:
        # Create a backup before destructive operation.
        current = Path(DB_FILE)
        if current.exists():
            backup = Path(f"{DB_FILE}.before_clear_backup")
            backup.write_bytes(current.read_bytes())

        con = db()
        tables = [
            "users", "products", "stock", "orders", "deposits",
            "messages", "settings", "force_join", "support_tickets",
            "payment_methods", "support_options"
        ]
        for table in tables:
            con.execute(f"DELETE FROM {table}")
        con.commit()
        con.close()

        init_db()
        context.user_data.clear()

        await update.message.reply_text(
            premiumize_html("✅ <b>All data cleared.</b>\n\n"
            "একটি safety backup রাখা হয়েছে: "
            "<code>store.db.before_clear_backup</code>"),
            parse_mode="HTML",
            reply_markup=admin_keyboard()
        )
    except Exception:
        logger.exception("Clear all data failed")
        await update.message.reply_text(
            premiumize_html("❌ Clear All Data failed। কোনো পরিবর্তন করার আগে transaction rollback করা হয়েছে।"),
            reply_markup=admin_keyboard()
        , parse_mode="HTML")



async def referral_callback(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "referral_close":
        try:
            await q.message.delete()
        except Exception:
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

async def user_support_callback(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "user_support_close":
        await q.edit_message_text(premiumize_html("❌ Support menu closed."), parse_mode="HTML")
        return
    if q.data == "user_support_ticket":
        context.user_data["state"] = "support_message"
        await q.edit_message_text(
            premiumize_html("📝 <b>Message Support</b>\n\nআপনার সমস্যা/প্রশ্ন লিখে পাঠান।"),
            parse_mode="HTML"
        )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid):
        return

    data = query.data or ""

    if data.startswith("deposit_approve:") or data.startswith("deposit_reject:"):
        action, did_text = data.split(":", 1)
        try:
            did = int(did_text)
        except ValueError:
            await query.edit_message_text(premiumize_html("❌ Invalid deposit ID."), parse_mode="HTML")
            return

        con = db()
        row = con.execute(
            "SELECT * FROM deposits WHERE id=? AND status='pending'",
            (did,)
        ).fetchone()

        if not row:
            con.close()
            await query.edit_message_text(premiumize_html("❌ Deposit not found or already processed."), parse_mode="HTML")
            return

        if action == "deposit_approve":
            con.execute(
                "UPDATE deposits SET status='approved' WHERE id=?",
                (did,)
            )
            con.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (row["amount"], row["user_id"])
            )
            referral_reward = apply_referral_reward(con, row)
            con.commit()
            con.close()
            try:
                await context.bot.send_message(
                    row["user_id"],
                    premiumize_html(f"✅ <b>Deposit Approved!</b>\n\n"
                    f"💰 Added: <b>{money(row['amount'])} ৳</b>\n"
                    f"💵 New Balance: <b>{money(get_balance(row['user_id']))} ৳</b>"),
                    parse_mode="HTML"
                )
            except Exception:
                pass

            if referral_reward > 0:
                referrer_id = get_referrer_id(row["user_id"])
                if referrer_id:
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            premiumize_html(f"🎉 <b>Referral Income Received!</b>\n\n"
                            f"👤 আপনার referred user Deposit করেছে: "
                            f"<b>{money(row['amount'])} ৳</b>\n"
                            f"💰 Referral Income (10%): "
                            f"<b>{money(referral_reward)} ৳</b>\n\n"
                            f"🛍️ এই Balance দিয়ে Product কেনা যাবে।"),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            await query.edit_message_text(
                premiumize_html(f"✅ Deposit #{did} approved.\nAmount: {money(row['amount'])} ৳")
            , parse_mode="HTML")
        else:
            con.execute(
                "UPDATE deposits SET status='rejected' WHERE id=?",
                (did,)
            )
            con.commit()
            con.close()
            try:
                await context.bot.send_message(
                    row["user_id"],
                    premiumize_html(f"❌ <b>Deposit Rejected</b>\n\n"
                    f"Amount: <b>{money(row['amount'])} ৳</b>"),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            await query.edit_message_text(
                premiumize_html(f"❌ Deposit #{did} rejected.\nAmount: {money(row['amount'])} ৳")
            , parse_mode="HTML")
        return

    if data == "admin_pending_deposits":
        con = db()
        rows = con.execute("""
            SELECT d.*, u.username, u.first_name
            FROM deposits d
            LEFT JOIN users u ON u.user_id=d.user_id
            WHERE d.status='pending'
            ORDER BY d.id DESC LIMIT 30
        """).fetchall()
        con.close()
        if not rows:
            await query.edit_message_text(premiumize_html("✅ কোনো pending deposit নেই।"), parse_mode="HTML")
            return
        for r in rows:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"deposit_approve:{r['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"deposit_reject:{r['id']}")
            ]])
            await context.bot.send_message(
                uid,
                premiumize_html(f"💳 <b>Deposit #{r['id']}</b>\n\n"
                f"👤 Name: <b>{escape(r['first_name'] or '—')}</b>\n"
                f"🔹 Username: @{escape(r['username']) if r['username'] else '—'}\n"
                f"🆔 User ID: <code>{r['user_id']}</code>\n"
                f"🏷️ Category: <b>{escape(r['payment_method'] or '—')}</b>\n"
                f"💰 Amount: <b>{money(r['amount'])} ৳</b>\n"
                f"🧾 TRX: <code>{r['trx_id']}</code>"),
                parse_mode="HTML",
                reply_markup=kb
            )
        await query.edit_message_text(premiumize_html("📋 Pending deposits দেখানো হয়েছে।"), parse_mode="HTML")
        return


def get_active_payment_text():
    con = db()
    rows = con.execute("""
        SELECT * FROM payment_methods
        WHERE enabled=1 ORDER BY id ASC
    """).fetchall()
    con.close()

    if not rows:
        return get_setting("payment_info", PAYMENT_INFO)

    lines = ["💳 <b>Payment Methods</b>"]
    for r in rows:
        lines.append(
            f"\n<b>{r['name']}</b>\n"
            f"📱 Account: <code>{r['account']}</code>\n"
            f"📝 {r['instructions'] or ''}"
        )
    return "\n".join(lines)

# ---------------- KEYBOARDS ----------------

def main_keyboard(user_id):
    rows = [
        [KeyboardButton("🛍️ Buy Product"), KeyboardButton("💳 Deposit Money")],
        [KeyboardButton("💰 My Balance"), KeyboardButton("💵 Price List")],
        [KeyboardButton("🔗 Referral"), KeyboardButton("📞 Support")],
    ]

    if user_id in ADMIN_IDS:
        rows.append([KeyboardButton("⚙️ Admin Panel")])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def back_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔙 Return")]],
        resize_keyboard=True
    )


def product_keyboard(products):
    rows = []
    pair = []

    for p in products:
        pair.append(KeyboardButton(f"🛒 {p['name']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []

    if pair:
        rows.append(pair)

    rows.append([KeyboardButton("🔙 Return")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def product_management_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ Add Product"), KeyboardButton("📦 Add Stock")],
        [KeyboardButton("🗑️ Delete Product"), KeyboardButton("📋 Products")],
        [KeyboardButton("🔙 Return")],
    ], resize_keyboard=True)


def force_join_management_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ Add Channel"), KeyboardButton("🗑️ Remove Channel")],
        [KeyboardButton("📋 View List")],
        [KeyboardButton("🔙 Return")],
    ], resize_keyboard=True)


def admin_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📦 Product Management"), KeyboardButton("💰 Pending Deposits")],
        [KeyboardButton("👥 User Management"), KeyboardButton("📢 Broadcast")],
        [KeyboardButton("📞 Support Management"), KeyboardButton("💳 Payment Management")],
        [KeyboardButton("📢 Force Join"), KeyboardButton("🗄️ Database")],
        [KeyboardButton("📊 Statistics")],
        [KeyboardButton("🔙 Return")],
    ], resize_keyboard=True)


# ---------------- HELPERS ----------------

def is_admin(user_id):
    return user_id in ADMIN_IDS


def get_products():
    con = db()
    rows = con.execute("""
        SELECT p.*,
               (SELECT COUNT(*) FROM stock s
                WHERE s.product_id=p.id AND s.sold=0) AS stock_count
        FROM products p
        WHERE p.active=1
        ORDER BY p.id DESC
    """).fetchall()
    con.close()
    return rows


def find_product_by_button(text):
    name = text.replace("🛒 ", "", 1).strip()
    con = db()
    row = con.execute(
        "SELECT * FROM products WHERE name=? AND active=1",
        (name,)
    ).fetchone()
    con.close()
    return row


# ============================================================
# CUSTOM WELCOME MESSAGE
# User supplied Telegram Premium Custom Emoji IDs are used here.
# {name} is replaced with the user's Telegram display name.
# ============================================================
WELCOME_EMOJI_1 = "5354972242629383937"
WELCOME_EMOJI_2 = "5312361253610475399"
WELCOME_EMOJI_3 = "5193202823411546657"

def welcome_message(user):
    name = escape(user.full_name or user.first_name or "User")
    return (
        f'<tg-emoji emoji-id="{WELCOME_EMOJI_1}">⭐</tg-emoji> <b>স্বাগতম, {name}</b>\n\n'
        f'<tg-emoji emoji-id="{WELCOME_EMOJI_2}">⭐</tg-emoji> যে কোনো প্রডাক্ট ক্রয় করতে নিচের অপশনগুলো ব্যবহার করুন '
        f'<tg-emoji emoji-id="{WELCOME_EMOJI_3}">⭐</tg-emoji>'
    )


async def send_home(update, context, text=None):
    if text:
        await update.message.reply_text(
            premiumize_html(text),
            reply_markup=main_keyboard(update.effective_user.id)
        , parse_mode="HTML")
    else:
        await update.message.reply_text(
            welcome_message(update.effective_user),
            parse_mode="HTML",
            reply_markup=main_keyboard(update.effective_user.id)
        )


# ---------------- START / MAIN MENU ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Capture referral only when the /start user is genuinely new.
    referrer_id = None
    if context.args:
        arg = str(context.args[0]).strip()
        if arg.startswith("ref_"):
            try:
                candidate = int(arg[4:])
                if candidate != update.effective_user.id:
                    referrer_id = candidate
            except ValueError:
                pass

    con = db()
    existing = con.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (update.effective_user.id,)
    ).fetchone()
    con.close()

    if existing:
        ensure_user(update.effective_user)
    else:
        # Only accept a referral from an existing user.
        if referrer_id:
            con = db()
            ok = con.execute(
                "SELECT user_id FROM users WHERE user_id=?",
                (referrer_id,)
            ).fetchone()
            con.close()
            if not ok:
                referrer_id = None
        ensure_user(update.effective_user, referrer_id)

    context.user_data.clear()

    # Force Join must be checked BEFORE showing the main menu.
    # New users should see the required channels first and only reach
    # the main menu after successfully joining all active channels.
    if not await check_force_join(update, context):
        return

    await send_home(update, context)


async def show_buy_menu(update, context):
    products = get_products()

    if not products:
        await update.message.reply_text(
            premiumize_html("❌ বর্তমানে কোনো Product available নেই।"),
            reply_markup=main_keyboard(update.effective_user.id)
        , parse_mode="HTML")
        return

    await update.message.reply_text(
        premiumize_html("🛍️ <b>What would you like to buy?</b>\n\n"
        "নিচের Product থেকে একটি নির্বাচন করুন।"),
        parse_mode="HTML",
        reply_markup=product_keyboard(products)
    )


async def show_price_list(update, context):
    products = get_products()

    if not products:
        await update.message.reply_text(
            premiumize_html("📋 Price List এখন খালি।"),
            reply_markup=main_keyboard(update.effective_user.id)
        , parse_mode="HTML")
        return

    lines = ["💵 <b>PRICE LIST</b>\n"]
    for p in products:
        lines.append(
            f"🛍️ <b>{p['name']}</b>\n"
            f"💰 Price: {money(p['price'])} ৳\n"
            f"📦 Stock: {p['stock_count']}\n"
        )

    await update.message.reply_text(
        premiumize_html("\n".join(lines)),
        parse_mode="HTML",
        reply_markup=main_keyboard(update.effective_user.id)
    )


async def show_balance(update, context):
    balance = get_balance(update.effective_user.id)
    await update.message.reply_text(
        premiumize_html(f"💰 <b>Your Balance</b>\n\n"
        f"💵 Balance: <b>{money(balance)} ৳</b>"),
        parse_mode="HTML",
        reply_markup=main_keyboard(update.effective_user.id)
    )


async def show_language(update, context):
    await update.message.reply_text(
        premiumize_html("🌐 Choose Language\n\n"
        "🇧🇩 বাংলা: বর্তমানে প্রধান ভাষা\n"
        "🇬🇧 English: UI can be extended later."),
        reply_markup=main_keyboard(update.effective_user.id)
    , parse_mode="HTML")


async def show_inbox(update, context):
    con = db()
    rows = con.execute("""
        SELECT text, created_at FROM messages
        WHERE user_id=? ORDER BY id DESC LIMIT 10
    """, (update.effective_user.id,)).fetchall()

    con.execute(
        "UPDATE messages SET is_read=1 WHERE user_id=?",
        (update.effective_user.id,)
    )
    con.commit()
    con.close()

    if not rows:
        text = "📩 <b>Mail Inbox</b>\n\nকোনো নতুন message নেই।"
    else:
        text = "📩 <b>Mail Inbox</b>\n\n" + "\n\n".join(
            f"• {r['text']}\n<i>{r['created_at']}</i>" for r in rows
        )

    await update.message.reply_text(
        premiumize_html(text), parse_mode="HTML",
        reply_markup=main_keyboard(update.effective_user.id)
    )


# ---------------- PRODUCT BUY FLOW ----------------

async def show_product(update, context, product):
    context.user_data["buy_product_id"] = product["id"]

    con = db()
    stock = con.execute(
        "SELECT COUNT(*) AS c FROM stock WHERE product_id=? AND sold=0",
        (product["id"],)
    ).fetchone()["c"]
    con.close()

    if stock <= 0:
        await update.message.reply_text(
            premiumize_html("❌ <b>Currently out of stock.</b>"),
            parse_mode="HTML",
            reply_markup=product_keyboard(get_products())
        )
        return

    description = product["description"] or "No description available."

    await update.message.reply_text(
        premiumize_html(f"🛒 <b>{product['name']}</b>\n\n"
        f"{description}\n\n"
        f"📦 Available Stock: <b>{stock}</b>\n"
        f"💵 Price per item: <b>{money(product['price'])} ৳</b>\n\n"
        f"<b>কতটি কিনতে চান?</b>\n"
        f"(Please type a number / শূন্য ছাড়া সংখ্যা লিখুন)"),
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )


def _extract_2fa_secret(text):
    """Detect a TOTP secret from a purchased stock row.

    Supports otpauth:// URIs and common KEY/SECRET/2FA formats.
    Returns a normalized Base32 secret or None.
    """
    import base64
    raw = str(text or "")
    candidates = []

    for m in re.finditer(r"otpauth://(?:totp|hotp)/[^\s]+", raw, re.I):
        uri = m.group(0).rstrip(').,;>')
        try:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(uri).query)
            if q.get("secret"):
                candidates.append(q["secret"][0])
        except Exception:
            pass

    patterns = [
        r"(?:2fa|2f|totp|otp|secret|key|authenticator)[\s_:-]*(?:key|secret)?\s*[=:]\s*([A-Z2-7][A-Z2-7\s-]{15,})",
        # A common stock row is: UID | Password | 2FA Secret Key |
        r"(?:^|\|)\s*([A-Z2-7](?:[A-Z2-7\s-]{15,}))[\s|,;]*$",
        r"\b([A-Z2-7]{16,64})\b",
    ]
    # Pipe-separated stock rows commonly put the 2FA secret in column 3.
    pipe_parts = [x.strip() for x in raw.split("|")]
    if len(pipe_parts) >= 3:
        candidates.append(pipe_parts[2])

    for pattern in patterns:
        for m in re.finditer(pattern, raw, re.I):
            candidates.append(m.group(1))

    for candidate in candidates:
        secret = re.sub(r"[^A-Z2-7]", "", str(candidate).upper())
        if len(secret) < 16:
            continue
        try:
            base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
            return secret
        except Exception:
            continue
    return None


def _generate_totp(secret, timestamp=None, digits=6, period=30):
    """Generate a standard RFC 6238 TOTP using only the Python stdlib."""
    import base64
    import hashlib
    import hmac
    import struct
    import time

    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    counter = int((time.time() if timestamp is None else timestamp) // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


async def _copy_code_button(code):
    """Create Telegram's native copy-to-clipboard button when supported."""
    if _TelegramCopyTextButton is not None:
        return InlineKeyboardButton(str(code), copy_text=_TelegramCopyTextButton(text=str(code)))
    # Compatibility with PTB versions that do not expose CopyTextButton yet.
    try:
        return InlineKeyboardButton(
            str(code),
            api_kwargs={"copy_text": {"text": str(code)}}
        )
    except TypeError:
        # Last-resort fallback: the visible code remains selectable/copyable by Telegram.
        return InlineKeyboardButton(str(code), callback_data=f"copy_code:{code}")


def _totp_keyboard(code, stock_id, product_id=None):
    next_btn = []
    if product_id is not None:
        next_btn = [InlineKeyboardButton("Next purchase", callback_data=f"next_purchase:{product_id}")]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(str(code), copy_text=_TelegramCopyTextButton(text=str(code)))
         if _TelegramCopyTextButton is not None else
         InlineKeyboardButton(str(code), api_kwargs={"copy_text": {"text": str(code)}})],
        [InlineKeyboardButton("Refresh", callback_data=f"refresh_2fa:{stock_id}")] + next_btn
    ])


def _account_card_text(content, code=None):
    parts = [x.strip() for x in str(content or "").split("|") if x.strip()]
    display_lines = []
    if len(parts) >= 3:
        uid, password, key = parts[0], parts[1], parts[2]
        try:
            if re.fullmatch(r"[+-]?\d+(?:\.\d+)?[Ee][+-]?\d+", uid):
                from decimal import Decimal
                uid = format(Decimal(uid), "f").rstrip("0").rstrip(".")
        except Exception:
            pass
        display_lines = [
            f"UID >> <code>{escape(uid)}</code>",
            f"PASS >> <code>{escape(password)}</code>",
            f"2F KEY >> <code>{escape(key)}</code>",
        ]
    else:
        for line in str(content or "").splitlines():
            line=line.strip()
            if not line: continue
            if re.match(r"^uid\s*:", line, re.I):
                display_lines.append(f"UID >> <code>{escape(line.split(':',1)[1].strip())}</code>")
            elif re.match(r"^(?:password|pass)\s*:", line, re.I):
                display_lines.append(f"PASS >> <code>{escape(line.split(':',1)[1].strip())}</code>")
            elif re.match(r"^2f\s*key\s*:", line, re.I):
                display_lines.append(f"2F KEY >> <code>{escape(line.split(':',1)[1].strip())}</code>")
            else:
                display_lines.append(f"<code>{escape(line)}</code>")
    text = (
        "🛒 <b>Account Details</b>\n"
        "━━━━━━━━━━━━━━\n"
        + "\n\n".join(display_lines) + "\n"
        "━━━━━━━━━━━━━━"
    )
    if code:
        text += f"\n\n🔐 <b>CODE</b>\n<code>{escape(str(code))}</code>"
    return premiumize_html(text)


def _purchase_details_keyboard(content, stock_id=None, product_id=None, secret=None):
    """Keyboard for purchased account details."""
    rows = []
    if secret and stock_id is not None:
        rows.append([InlineKeyboardButton(
            "🔐 Get Code", callback_data=f"get_2fa:{stock_id}")])
    if product_id is not None:
        rows.append([InlineKeyboardButton(
            "Next purchase", callback_data=f"next_purchase:{product_id}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def get_2fa_code_callback(update, context):
    query = update.callback_query
    data = query.data or ""
    try:
        stock_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid 2FA request.", show_alert=True)
        return

    con = db()
    row = con.execute(
        "SELECT * FROM stock WHERE id=? AND sold=1 AND sold_to=?",
        (stock_id, query.from_user.id)
    ).fetchone()
    con.close()
    if not row:
        await query.answer("This purchased item was not found.", show_alert=True)
        return

    secret = _extract_2fa_secret(row["content"] or "")
    if not secret:
        await query.answer("2FA key পাওয়া যায়নি।", show_alert=True)
        return
    try:
        code = _generate_totp(secret)
    except Exception:
        logger.exception("TOTP generation failed")
        await query.answer("2FA code generate করা যায়নি।", show_alert=True)
        return

    await query.answer()
    try:
        await query.edit_message_text(
            _account_card_text(row["content"], code),
            parse_mode="HTML",
            reply_markup=_totp_keyboard(code, stock_id, row["product_id"])
        )
    except Exception:
        logger.exception("Failed to replace Get Code button with TOTP code")
        await query.answer("Code generated, but message update failed.", show_alert=True)


async def refresh_2fa_code_callback(update, context):
    query = update.callback_query
    data = query.data or ""
    try:
        stock_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid refresh request.", show_alert=True)
        return

    con = db()
    row = con.execute(
        "SELECT * FROM stock WHERE id=? AND sold=1 AND sold_to=?",
        (stock_id, query.from_user.id)
    ).fetchone()
    con.close()
    if not row:
        await query.answer("This purchased item was not found.", show_alert=True)
        return

    secret = _extract_2fa_secret(row["content"] or "")
    if not secret:
        await query.answer("2FA key পাওয়া যায়নি।", show_alert=True)
        return
    try:
        code = _generate_totp(secret)
    except Exception:
        logger.exception("TOTP refresh failed")
        await query.answer("2FA code generate করা যায়নি।", show_alert=True)
        return

    await query.answer()
    try:
        await query.edit_message_text(
            _account_card_text(row["content"], code),
            parse_mode="HTML",
            reply_markup=_totp_keyboard(code, stock_id, row["product_id"])
        )
    except Exception:
        logger.exception("Failed to refresh TOTP code")
        await query.answer("Refresh failed.", show_alert=True)


async def next_purchase_callback(update, context):
    query = update.callback_query
    data = query.data or ""
    try:
        product_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid purchase request.", show_alert=True)
        return

    user_id = query.from_user.id
    con = db()
    product = con.execute(
        "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
    ).fetchone()
    if not product:
        con.close()
        await query.answer("Product পাওয়া যায়নি।", show_alert=True)
        return

    stock = con.execute(
        "SELECT * FROM stock WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT 1",
        (product_id,)
    ).fetchone()
    if not stock:
        con.close()
        await query.answer("Currently out of stock.", show_alert=True)
        return

    price = float(product["price"])
    try:
        con.execute("BEGIN IMMEDIATE")
        bal = con.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not bal or float(bal["balance"]) < price:
            con.rollback(); con.close()
            await query.answer("Insufficient balance!", show_alert=True)
            return
        locked = con.execute(
            "SELECT id, content, file_id FROM stock WHERE id=? AND product_id=? AND sold=0",
            (stock["id"], product_id)
        ).fetchone()
        if not locked:
            con.rollback(); con.close()
            await query.answer("Stock just sold. Try again.", show_alert=True)
            return
        con.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (price, user_id))
        con.execute(
            "UPDATE stock SET sold=1, sold_to=?, sold_at=? WHERE id=? AND sold=0",
            (user_id, now(), stock["id"])
        )
        con.execute(
            "INSERT INTO orders(user_id, product_id, quantity, total, status, created_at) VALUES (?, ?, 1, ?, 'completed', ?)",
            (user_id, product_id, price, now())
        )
        con.commit()
    except Exception:
        con.rollback(); con.close()
        logger.exception("Next purchase failed")
        await query.answer("Purchase failed.", show_alert=True)
        return
    con.close()

    content = str(stock["content"] or stock["file_id"] or "").strip()
    secret = _extract_2fa_secret(content)
    keyboard = _purchase_details_keyboard(
        content, stock_id=stock["id"], secret=secret
    )

    await query.answer("Purchase successful!", show_alert=False)
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=_account_card_text(content),
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception:
        logger.exception("Failed to deliver next purchase")


async def process_quantity(update, context, quantity):
    product_id = context.user_data.get("buy_product_id")
    if not product_id:
        return False

    if quantity <= 0:
        await update.message.reply_text(premiumize_html("❌ সঠিক quantity লিখুন।"), parse_mode="HTML")
        return True

    con = db()
    product = con.execute(
        "SELECT * FROM products WHERE id=? AND active=1",
        (product_id,)
    ).fetchone()

    if not product:
        con.close()
        await update.message.reply_text(premiumize_html("❌ Product পাওয়া যায়নি।"), parse_mode="HTML")
        context.user_data.pop("buy_product_id", None)
        return True

    stock_rows = con.execute("""
        SELECT * FROM stock
        WHERE product_id=? AND sold=0
        ORDER BY id ASC LIMIT ?
    """, (product_id, quantity)).fetchall()

    available = len(stock_rows)
    total = float(product["price"]) * quantity
    balance = get_balance(update.effective_user.id)

    if available < quantity:
        con.close()
        await update.message.reply_text(
            premiumize_html(f"❌ <b>Not enough stock.</b>\n\nAvailable Stock: {available}\nYou requested: {quantity}"),
            parse_mode="HTML", reply_markup=product_keyboard(get_products())
        )
        return True

    if balance < total:
        con.close()
        await update.message.reply_text(
            premiumize_html(f"❌ <b>Insufficient balance!</b>\n\nTotal Price: {money(total)} ৳\nYour Balance: {money(balance)} ৳"),
            parse_mode="HTML", reply_markup=product_keyboard(get_products())
        )
        return True

    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,)).fetchone()
        locked_balance = float(row["balance"])
        if locked_balance < total:
            con.rollback(); con.close()
            await update.message.reply_text(premiumize_html("❌ Balance changed. Please try again."), parse_mode="HTML")
            return True

        ids = [r["id"] for r in stock_rows]
        con.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (total, update.effective_user.id))
        for sid in ids:
            con.execute("""UPDATE stock SET sold=1, sold_to=?, sold_at=? WHERE id=? AND sold=0""",
                         (update.effective_user.id, now(), sid))
        con.execute("""INSERT INTO orders(user_id, product_id, quantity, total, status, created_at)
                       VALUES (?, ?, ?, ?, 'completed', ?)""",
                     (update.effective_user.id, product_id, quantity, total, now()))
        con.commit()
    except Exception:
        con.rollback(); con.close()
        logger.exception("Purchase failed")
        await update.message.reply_text(premiumize_html("❌ Purchase failed. Please try again."), parse_mode="HTML")
        return True
    con.close()

    context.user_data.pop("buy_product_id", None)
    new_balance = get_balance(update.effective_user.id)
    await update.message.reply_text(
        premiumize_html(f"✅ <b>Purchase successful!</b>\n\n🛍️ Product: <b>{escape(product['name'])}</b>\n"
                        f"🔢 Quantity: <b>{quantity}</b>\n💵 Total Price: <b>{money(total)} ৳</b>\n"
                        f"💰 New Balance: <b>{money(new_balance)} ৳</b>\n\n📦 Purchased item নিচে পাঠানো হচ্ছে..."),
        parse_mode="HTML", reply_markup=main_keyboard(update.effective_user.id)
    )

    # Deliver every purchased row as a normal Telegram message. If a row
    # contains a TOTP secret, attach a per-item Get Code inline button.
    for number, stock in enumerate(stock_rows, start=1):
        content = str(stock["content"] or stock["file_id"] or "").strip()
        secret = _extract_2fa_secret(content)
        keyboard = _purchase_details_keyboard(
            content, stock_id=stock["id"], secret=secret
        )
        # Format the purchased account card.
        formatted_content = _account_card_text(content)
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=formatted_content,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    return True


# ---------------- AUTO PAYMENT / SMS WEBHOOK ----------------

def normalize_payment_method(value):
    value = (value or "").strip().lower()
    if "bkash" in value or "বিকাশ" in value:
        return "bkash"
    if "nagad" in value or "নগদ" in value:
        return "nagad"
    return value


def parse_payment_sms(message, provider=""):
    """
    Parse common bKash/Nagad incoming-payment SMS formats.

    SMSGate's sms:received payload puts the actual SMS text in
    payload.message and the originating SMS sender in payload.sender.
    Provider detection is intentionally conservative but supports common
    sender IDs and English/Bengali keywords.
    """
    import re

    raw = str(message or "").strip()
    low = raw.lower()
    sender = str(provider or "").strip().lower()

    p = normalize_payment_method(provider)

    if p not in ("bkash", "nagad"):
        if any(x in low for x in ("bkash", "b-kash", "বিকাশ")):
            p = "bkash"
        elif any(x in low for x in ("nagad", "নগদ")):
            p = "nagad"
        elif sender in {
            "bkash", "bkash-payment", "bkashpay", "bkash payment",
            "16247", "09609616247"
        }:
            p = "bkash"
        elif sender in {
            "nagad", "nagad-payment", "nagadpay", "16167", "09609616167"
        }:
            p = "nagad"

    # Common forms:
    #   You have received Tk 100.00 ...
    #   Received Tk 100 ...
    #   Amount: Tk 100
    #   Tk. 100 / BDT 100 / ৳100
    amount_patterns = [
        r"(?:you\s+have\s+)?received(?:\s+tk)?\s*[:=\-]?\s*(?:tk|bdt|৳)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"(?:cash[\s-]*in|money[\s-]*received|payment|amount)\s*[:=\-]?\s*(?:tk|bdt|৳)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"(?:tk|bdt|৳)\s*\.?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    ]

    amount = None
    for pattern in amount_patterns:
        m = re.search(pattern, raw, re.I)
        if m:
            try:
                amount = float(m.group(1).replace(",", ""))
                break
            except ValueError:
                continue

    # TrxID / Transaction ID / Txn ID.
    trx_patterns = [
        r"\btrx\s*id?\s*[:#=\-]?\s*([A-Z0-9]{5,40})\b",
        r"\btransaction\s*id\s*[:#=\-]?\s*([A-Z0-9]{5,40})\b",
        r"\btxn\s*id?\s*[:#=\-]?\s*([A-Z0-9]{5,40})\b",
    ]

    trx_id = None
    for pattern in trx_patterns:
        m = re.search(pattern, raw, re.I)
        if m:
            trx_id = m.group(1).strip().upper()
            break

    return p, amount, trx_id


def _telegram_send_message(chat_id, text):
    """Send a Telegram message directly from the webhook thread."""
    if not BOT_TOKEN:
        return False
    try:
        payload = json.dumps({
            "chat_id": int(chat_id),
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=15) as response:
            return 200 <= response.status < 300
    except Exception:
        logger.exception("Telegram notification from payment webhook failed")
        return False


def auto_approve_payment(provider, amount, trx_id, raw_sms):
    """
    Match an incoming SMS against an existing pending deposit.

    Exact match requirements:
      - provider must match bKash/Nagad payment method
      - amount must match exactly to 2 decimal places
      - transaction ID must match exactly (case-insensitive)
      - the same transaction can never be processed twice
    """
    provider = normalize_payment_method(provider)
    if amount is None or not trx_id:
        return {"ok": False, "reason": "amount or trx_id missing"}

    amount = round(float(amount), 2)
    trx_norm = trx_id.strip().upper()

    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")

        # First reject duplicate/previously processed transaction IDs.
        duplicate = con.execute(
            "SELECT id, status FROM deposits WHERE UPPER(TRIM(trx_id))=? LIMIT 1",
            (trx_norm,)
        ).fetchone()
        if duplicate and duplicate["status"] == "approved":
            con.rollback()
            return {"ok": True, "status": "duplicate", "deposit_id": duplicate["id"]}

        if provider in ("bkash", "nagad"):
            rows = con.execute("""
                SELECT d.*, u.username, u.first_name
                FROM deposits d
                LEFT JOIN users u ON u.user_id=d.user_id
                WHERE d.status='pending'
                  AND LOWER(TRIM(d.payment_method)) LIKE ?
                  AND ABS(d.amount - ?) < 0.005
                  AND UPPER(TRIM(d.trx_id))=?
                ORDER BY d.id ASC
                LIMIT 1
            """, (f"%{provider}%", amount, trx_norm)).fetchall()
        else:
            # SMSGate may provide a numeric sender ID. If the SMS text itself
            # doesn't identify the provider, match only against an exact
            # pending amount + transaction ID belonging to bKash/Nagad.
            rows = con.execute("""
                SELECT d.*, u.username, u.first_name
                FROM deposits d
                LEFT JOIN users u ON u.user_id=d.user_id
                WHERE d.status='pending'
                  AND (
                    LOWER(TRIM(d.payment_method)) LIKE '%bkash%'
                    OR LOWER(TRIM(d.payment_method)) LIKE '%nagad%'
                  )
                  AND ABS(d.amount - ?) < 0.005
                  AND UPPER(TRIM(d.trx_id))=?
                ORDER BY d.id ASC
                LIMIT 1
            """, (amount, trx_norm)).fetchall()

        if not rows:
            con.rollback()
            return {"ok": False, "reason": "no_exact_pending_match"}

        row = rows[0]
        provider = normalize_payment_method(row["payment_method"]) if provider not in ("bkash", "nagad") else provider

        con.execute("""
            UPDATE deposits
            SET status='approved', auto_processed=1, auto_processed_at=?
            WHERE id=? AND status='pending'
        """, (now(), row["id"]))

        con.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (row["amount"], row["user_id"])
        )

        referral_reward = apply_referral_reward(con, row)
        con.commit()

        new_balance_row = con.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (row["user_id"],)
        ).fetchone()
        new_balance = float(new_balance_row["balance"]) if new_balance_row else 0.0

    except Exception:
        con.rollback()
        logger.exception("Auto payment approval failed")
        return {"ok": False, "reason": "database_error"}
    finally:
        con.close()

    # User notification
    _telegram_send_message(
        row["user_id"],
        f"✅ <b>Deposit Approved Automatically</b>\n\n"
        f"💳 Method: <b>{escape(row['payment_method'] or provider)}</b>\n"
        f"💰 Added: <b>{money(row['amount'])} ৳</b>\n"
        f"🧾 TRX: <code>{escape(row['trx_id'])}</code>\n"
        f"💵 New Balance: <b>{money(new_balance)} ৳</b>"
    )

    # Referral notification, same logic as manual approval.
    if referral_reward > 0:
        referrer_id = get_referrer_id(row["user_id"])
        if referrer_id:
            _telegram_send_message(
                referrer_id,
                f"🎉 <b>Referral Income Received!</b>\n\n"
                f"👤 আপনার referred user Deposit করেছে: "
                f"<b>{money(row['amount'])} ৳</b>\n"
                f"💰 Referral Income (10%): "
                f"<b>{money(referral_reward)} ৳</b>"
            )

    return {
        "ok": True,
        "status": "approved",
        "deposit_id": row["id"],
        "user_id": row["user_id"],
        "amount": row["amount"],
        "provider": provider,
        "trx_id": row["trx_id"],
    }


class PaymentWebhookHandler(BaseHTTPRequestHandler):
    def _json_response(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._json_response(200, {
                "ok": True,
                "service": "payment_sms_webhook",
                "auto_payment_enabled": AUTO_PAYMENT_ENABLED,
            })
            return
        self._json_response(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/payment_sms":
            self._json_response(404, {"ok": False, "error": "not_found"})
            return

        if not AUTO_PAYMENT_ENABLED:
            self._json_response(503, {"ok": False, "error": "auto_payment_disabled"})
            return

        # SMSGate Dashboard webhooks do not send our custom X-Webhook-Secret
        # header. Authentication is therefore not required here; the endpoint
        # is intentionally limited to SMSGate's webhook path and validates the
        # payment SMS before approving any pending deposit.

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            content_type = (self.headers.get("Content-Type") or "").lower()

            if "application/json" in content_type:
                data = json.loads(raw.decode("utf-8"))
            else:
                parsed = parse_qs(raw.decode("utf-8"))
                data = {k: v[0] for k, v in parsed.items()}

            # Accept both our simple payload and common Android SMS relay
            # webhook payloads, e.g. {event: "sms:received", payload: {...}}.
            payload = data.get("payload")
            if isinstance(payload, dict):
                merged = dict(data)
                merged.update(payload)
                data = merged

            sms = str(
                data.get("message")
                or data.get("sms")
                or data.get("body")
                or data.get("text")
                or ""
            ).strip()
            provider = str(
                data.get("provider")
                or data.get("sender")
                or data.get("source")
                or data.get("from")
                or data.get("address")
                or ""
            ).strip()

            if not sms:
                self._json_response(400, {"ok": False, "error": "message_required"})
                return

            parsed_provider, amount, trx_id = parse_payment_sms(sms, provider)
            logger.info(
                "Payment SMS parsed: provider=%s amount=%s trx_id=%s sender=%s",
                parsed_provider, amount, trx_id, provider
            )
            result = auto_approve_payment(parsed_provider, amount, trx_id, sms)
            self._json_response(200 if result.get("ok") else 422, result)

        except Exception:
            logger.exception("Payment webhook request failed")
            self._json_response(400, {"ok": False, "error": "invalid_request"})

    def log_message(self, format, *args):
        logger.info("PaymentWebhook: " + format, *args)


def start_payment_webhook():
    server = ThreadingHTTPServer(
        (PAYMENT_WEBHOOK_HOST, PAYMENT_WEBHOOK_PORT),
        PaymentWebhookHandler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(
        "Payment SMS webhook started on %s:%s",
        PAYMENT_WEBHOOK_HOST,
        PAYMENT_WEBHOOK_PORT
    )
    return server


# ---------------- DEPOSIT ----------------

async def deposit_start(update, context):
    con = db()
    rows = con.execute("SELECT * FROM payment_methods WHERE enabled=1 ORDER BY id ASC").fetchall()
    con.close()
    if not rows:
        await update.message.reply_text(
            premiumize_html("❌ বর্তমানে কোনো payment method চালু নেই। Admin-কে জানান।"),
            reply_markup=main_keyboard(update.effective_user.id)
        , parse_mode="HTML")
        return
    buttons = [[InlineKeyboardButton(f"💳 {r['name']}", callback_data=f"user_payment:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="user_payment_cancel")])
    await update.message.reply_text(
        premiumize_html("💳 <b>Deposit Money</b>\n\nপ্রথমে Payment Method নির্বাচন করুন:"),
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def user_payment_callback(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "user_payment_cancel":
        await q.edit_message_text(premiumize_html("❌ Deposit cancelled."), parse_mode="HTML")
        return
    try:
        pid = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await q.edit_message_text(premiumize_html("❌ Invalid payment method."), parse_mode="HTML")
        return
    con = db()
    row = con.execute("SELECT * FROM payment_methods WHERE id=? AND enabled=1", (pid,)).fetchone()
    con.close()
    if not row:
        await q.edit_message_text(premiumize_html("❌ Payment method পাওয়া যায়নি বা disabled।"), parse_mode="HTML")
        return
    context.user_data["deposit_payment_method_id"] = pid
    context.user_data["deposit_payment_method"] = row["name"]
    context.user_data["state"] = "deposit_amount"
    await q.edit_message_text(
        premiumize_html(f"💳 <b>{escape(row['name'])}</b>\n\n"
        f"📱 Account: <code>{escape(row['account'])}</code>\n"
        f"📝 {escape(row['instructions'] or '')}\n\n"
        f"💵 কত টাকা Deposit করতে চান?\n"
        f"Minimum: <b>{money(row['min_amount'])} ৳</b>\n"
        f"Maximum: <b>{money(row['max_amount'])} ৳</b>\n\n"
        "উদাহরণ: 100"),
        parse_mode="HTML"
    )


async def deposit_amount(update, context, text):
    try:
        amount = float(text)
        pid = context.user_data.get("deposit_payment_method_id")
        con = db(); pm = con.execute("SELECT min_amount,max_amount FROM payment_methods WHERE id=?", (pid,)).fetchone(); con.close()
        min_amount = float(pm["min_amount"] if pm else 10)
        max_amount = float(pm["max_amount"] if pm else 50000)
        if amount <= 0 or amount < min_amount or amount > max_amount:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            premiumize_html(f"❌ Amount অবশ্যই {money(get_setting('deposit_min','10'))} ৳ থেকে "
            f"{money(get_setting('deposit_max','50000'))} ৳ এর মধ্যে হতে হবে।")
        , parse_mode="HTML")
        return
    context.user_data["deposit_amount"] = amount
    context.user_data["state"] = "deposit_trx"
    await update.message.reply_text(
        premiumize_html(f"💳 Method: <b>{escape(context.user_data.get('deposit_payment_method',''))}</b>\n"
        f"💵 Amount: <b>{money(amount)} ৳</b>\n\n"
        "Payment করার পর Transaction ID পাঠান।"),
        parse_mode="HTML", reply_markup=back_keyboard()
    )


async def deposit_trx(update, context, trx):
    amount = context.user_data.get("deposit_amount")
    method = context.user_data.get("deposit_payment_method", "")
    if not amount or not method:
        context.user_data.clear()
        await update.message.reply_text(premiumize_html("❌ Deposit session expired."), parse_mode="HTML")
        return
    trx = trx.strip()
    if len(trx) < 3:
        await update.message.reply_text(premiumize_html("❌ সঠিক Transaction ID দিন।"), parse_mode="HTML")
        return
    con = db()
    cur = con.execute("""
        INSERT INTO deposits(user_id, amount, payment_method, trx_id, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
    """, (update.effective_user.id, amount, method, trx, now()))
    deposit_id = cur.lastrowid
    con.commit(); con.close()

    user = update.effective_user
    username = f"@{user.username}" if user.username else "—"
    # Keep the admin notification for safety/manual fallback, but tell the user
    # that enabled auto-payment will verify the SMS automatically.
    for admin_id in ADMIN_IDS:
        try:
            admin_text = (
                f"💳 <b>NEW DEPOSIT</b>\n\n"
                f"🆔 Deposit ID: <code>{deposit_id}</code>\n"
                f"👤 Name: <b>{escape(user.first_name or '—')}</b>\n"
                f"🔹 Username: {escape(username)}\n"
                f"🆔 User ID: <code>{user.id}</code>\n"
                f"🏷️ Category: <b>{escape(method)}</b>\n"
                f"💰 Amount: <b>{money(amount)} ৳</b>\n"
                f"🧾 TRX: <code>{escape(trx)}</code>"
            )
            if AUTO_PAYMENT_ENABLED:
                admin_text += "\n\n🤖 Auto Payment: <b>ON</b> — SMS match হলে automatically approve হবে."
            await context.bot.send_message(
                admin_id,
                premiumize_html(admin_text),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"deposit_approve:{deposit_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"deposit_reject:{deposit_id}")
                ]])
            )
        except Exception:
            pass

    # The webhook can approve the deposit almost immediately after the request
    # is created. Check the current status so the user never receives a stale
    # "Admin verification" message when auto-payment has already approved it.
    con = db()
    status_row = con.execute("SELECT status FROM deposits WHERE id=?", (deposit_id,)).fetchone()
    con.close()
    current_status = status_row["status"] if status_row else "pending"

    context.user_data.clear()
    if current_status == "approved":
        response_text = (
            "✅ <b>Deposit Approved Automatically!</b>\n\n"
            f"💰 Added: <b>{money(amount)} ৳</b>\n"
            "🤖 Payment SMS verified successfully.\n"
            "💵 আপনার balance update করা হয়েছে।"
        )
    elif AUTO_PAYMENT_ENABLED:
        response_text = (
            "✅ <b>Deposit request received!</b>\n\n"
            f"💰 Amount: <b>{money(amount)} ৳</b>\n"
            "🤖 SMS verification automatically হবে।\n"
            "⏳ Verification complete হলে আপনার balance-এ টাকা automatically যোগ হবে।\n"
            "ℹ️ Manual admin verification প্রয়োজন নেই।"
        )
    else:
        response_text = (
            "✅ <b>Deposit request submitted!</b>\n\n"
            "Admin verification-এর পর আপনার balance-এ টাকা যোগ হবে."
        )

    await update.message.reply_text(
        premiumize_html(response_text),
        parse_mode="HTML", reply_markup=main_keyboard(user.id)
    )


# ---------------- ADMIN STATISTICS ----------------

async def admin_statistics(update, context):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    users = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    blocked = con.execute("SELECT COUNT(*) AS c FROM users WHERE blocked=1").fetchone()["c"]
    balance = con.execute("SELECT COALESCE(SUM(balance),0) AS s FROM users").fetchone()["s"]
    products = con.execute("SELECT COUNT(*) AS c FROM products WHERE active=1").fetchone()["c"]
    stock_total = con.execute("SELECT COUNT(*) AS c FROM stock").fetchone()["c"]
    stock_available = con.execute("SELECT COUNT(*) AS c FROM stock WHERE sold=0").fetchone()["c"]
    stock_sold = con.execute("SELECT COUNT(*) AS c FROM stock WHERE sold=1").fetchone()["c"]
    orders = con.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    orders_done = con.execute("SELECT COUNT(*) AS c FROM orders WHERE status='completed'").fetchone()["c"]
    sales = con.execute("SELECT COALESCE(SUM(total),0) AS s FROM orders WHERE status='completed'").fetchone()["s"]
    deposits_total = con.execute("SELECT COUNT(*) AS c FROM deposits").fetchone()["c"]
    deposits_approved = con.execute("SELECT COUNT(*) AS c FROM deposits WHERE status='approved'").fetchone()["c"]
    deposits_pending = con.execute("SELECT COUNT(*) AS c FROM deposits WHERE status='pending'").fetchone()["c"]
    deposits_amount = con.execute("SELECT COALESCE(SUM(amount),0) AS s FROM deposits WHERE status='approved'").fetchone()["s"]
    referred = con.execute("SELECT COUNT(*) AS c FROM users WHERE referrer_id IS NOT NULL").fetchone()["c"]
    referral_income = con.execute("SELECT COALESCE(SUM(reward_amount),0) AS s FROM referral_earnings").fetchone()["s"]
    con.close()

    text = (
        "📊 <b>BOT STATISTICS</b>\n\n"
        f"👥 Total Users: <b>{users}</b>\n"
        f"🚫 Blocked Users: <b>{blocked}</b>\n"
        f"💰 Users Balance: <b>{money(balance)} ৳</b>\n\n"
        f"📦 Active Products: <b>{products}</b>\n"
        f"🗃️ Total Stock: <b>{stock_total}</b>\n"
        f"✅ Available Stock: <b>{stock_available}</b>\n"
        f"🔴 Sold Stock: <b>{stock_sold}</b>\n\n"
        f"🛒 Total Orders: <b>{orders}</b>\n"
        f"✅ Completed Orders: <b>{orders_done}</b>\n"
        f"💵 Product Sales: <b>{money(sales)} ৳</b>\n\n"
        f"💳 Total Deposits: <b>{deposits_total}</b>\n"
        f"✅ Approved Deposits: <b>{deposits_approved}</b>\n"
        f"⏳ Pending Deposits: <b>{deposits_pending}</b>\n"
        f"💰 Approved Deposit Amount: <b>{money(deposits_amount)} ৳</b>\n\n"
        f"🔗 Total Referred Users: <b>{referred}</b>\n"
        f"🎁 Referral Income Paid: <b>{money(referral_income)} ৳</b>"
    )
    await update.message.reply_text(
        premiumize_html(text),
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


# ---------------- ADMIN ----------------

async def admin_panel(update, context):
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        premiumize_html("⚙️ <b>ADMIN PANEL</b>\n\n"
        "Product, Stock, Deposit এবং User management এখান থেকে করুন।"),
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


async def add_product_start(update, context):
    context.user_data["admin_state"] = "product_name"
    await update.message.reply_text(
        premiumize_html("➕ Product Name লিখুন:"),
        reply_markup=back_keyboard()
    , parse_mode="HTML")


async def add_product_name(update, context):
    context.user_data["new_product_name"] = update.message.text.strip()
    context.user_data["admin_state"] = "product_price"
    await update.message.reply_text(premiumize_html("💰 Product Price লিখুন:"), parse_mode="HTML")


async def add_product_price(update, context):
    try:
        price = float(update.message.text.strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক price লিখুন।"), parse_mode="HTML")
        return

    context.user_data["new_product_price"] = price
    context.user_data["admin_state"] = "product_description"
    await update.message.reply_text(premiumize_html("📝 Product Description লিখুন:"), parse_mode="HTML")


async def add_product_description(update, context):
    name = context.user_data["new_product_name"]
    price = context.user_data["new_product_price"]
    description = update.message.text.strip()

    con = db()
    cur = con.execute("""
        INSERT INTO products(name, price, description, active, created_at)
        VALUES (?, ?, ?, 1, ?)
    """, (name, price, description, now()))
    product_id = cur.lastrowid
    con.commit()
    con.close()

    context.user_data.clear()

    await update.message.reply_text(
        premiumize_html(f"✅ <b>Product created successfully!</b>\n\n"
        f"🛍️ Name: {name}\n"
        f"💰 Price: {money(price)} ৳\n"
        f"🆔 Product ID: {product_id}\n\n"
        "এখন 📦 Add Stock থেকে file/account যোগ করুন।"),
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


async def add_stock_start(update, context):
    products = get_products()
    if not products:
        await update.message.reply_text(premiumize_html("❌ আগে Product তৈরি করুন।"), parse_mode="HTML")
        return

    context.user_data["admin_state"] = "stock_product"
    lines = ["📦 কোন Product-এ Stock যোগ করবেন?\n"]
    for p in products:
        lines.append(f"{p['id']} = {p['name']}")

    await update.message.reply_text(
        premiumize_html("\n".join(lines) + "\n\nProduct ID লিখুন:")
    , parse_mode="HTML")


async def add_stock_product(update, context):
    try:
        product_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক Product ID লিখুন।"), parse_mode="HTML")
        return

    con = db()
    product = con.execute(
        "SELECT * FROM products WHERE id=? AND active=1",
        (product_id,)
    ).fetchone()
    con.close()

    if not product:
        await update.message.reply_text(premiumize_html("❌ Product পাওয়া যায়নি।"), parse_mode="HTML")
        return

    context.user_data["stock_product_id"] = product_id
    context.user_data["admin_state"] = "stock_file"

    await update.message.reply_text(
        premiumize_html(f"📦 <b>{escape(product['name'])}</b> selected.\n\n"
        "এখন একটি account/profile file পাঠান।\n\n"
        "⚠️ <b>একটি file = একটি stock নয়।</b>\n"
        "File-এর ভিতরের প্রতিটি non-empty row = ১টি stock.\n\n"
        "যেমন file-এ 100টি row থাকলে Stock = 100 হবে।"),
        parse_mode="HTML"
    )


def create_purchase_xlsx(rows, product_name, user_id):
    """Create a minimal valid XLSX file using only the Python standard library."""
    import html
    import re
    from datetime import datetime

    safe_product = re.sub(r"[^A-Za-z0-9_-]+", "_", str(product_name)).strip("_") or "product"
    path = UPLOAD_DIR / f"purchase_{user_id}_{safe_product}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    def esc(value):
        return html.escape(str(value), quote=True)

    sheet_rows = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"',
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        '<sheetData>'
    ]
    for idx, value in enumerate(rows, start=1):
        sheet_rows.append(f'<row r="{idx}"><c r="A{idx}" t="inlineStr"><is><t>{esc(value)}</t></is></c></row>')
    sheet_rows += ['</sheetData>', '</worksheet>']

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Purchased" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        z.writestr("xl/worksheets/sheet1.xml", "\n".join(sheet_rows))
    return path


def _xlsx_cell_value(cell, shared_strings):
    """Read one XLSX cell without requiring openpyxl."""
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cell_type = cell.attrib.get("t", "")
    value_node = cell.find("x:v", ns)

    if cell_type == "inlineStr":
        parts = []
        for node in cell.findall(".//x:t", ns):
            parts.append(node.text or "")
        return "".join(parts).strip()

    if value_node is None:
        # Formula cells may have a cached inline string.
        parts = [node.text or "" for node in cell.findall(".//x:t", ns)]
        return "".join(parts).strip()

    value = value_node.text or ""

    if cell_type == "s":
        try:
            idx = int(value)
            return shared_strings[idx].strip() if 0 <= idx < len(shared_strings) else value
        except (ValueError, IndexError):
            return value.strip()

    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"

    return value.strip()


def _parse_xlsx_rows(raw_bytes):
    """
    Parse XLSX/XLSM using only Python's standard library.

    If the first populated row is a header row containing UID, Password and
    2FA/Secret Key columns, the header is treated as metadata (not stock) and
    each following row is normalized into a clean account record.
    """
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as z:
            names = set(z.namelist())

            shared_strings = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in root.findall("x:si", ns):
                    parts = [node.text or "" for node in si.findall(".//x:t", ns)]
                    shared_strings.append("".join(parts))

            # Find worksheet files. sheet1 is normally the first sheet.
            sheet_names = sorted(
                n for n in names
                if n.startswith("xl/worksheets/") and n.endswith(".xml")
            )
            if not sheet_names:
                raise RuntimeError("XLSX file-এ কোনো worksheet পাওয়া যায়নি।")

            for sheet_name in sheet_names:
                root = ET.fromstring(z.read(sheet_name))
                result = []

                sheet_data = root.find("x:sheetData", ns)
                if sheet_data is None:
                    continue

                raw_rows = []
                for row in sheet_data.findall("x:row", ns):
                    cells = []
                    for cell in row.findall("x:c", ns):
                        value = _xlsx_cell_value(cell, shared_strings)
                        cells.append(value)
                    while cells and not str(cells[-1]).strip():
                        cells.pop()
                    if any(str(v).strip() for v in cells):
                        raw_rows.append([str(v).strip() for v in cells])

                if raw_rows:
                    # New stock format: first row contains field names such as
                    # UID | Password | 2F Secret Key. Do not sell the header.
                    headers = [re.sub(r"[^a-z0-9]+", "", v.lower()) for v in raw_rows[0]]
                    def header_index(*names):
                        wanted = {re.sub(r"[^a-z0-9]+", "", n.lower()) for n in names}
                        for i, h in enumerate(headers):
                            if h in wanted:
                                return i
                        return None

                    uid_idx = header_index("uid", "user id", "userid", "username id")
                    password_idx = header_index("password", "pass", "pwd")
                    key_idx = header_index(
                        "2f key", "2fa key", "2f secret key", "2fa secret key",
                        "secret key", "2factor key", "two factor key", "totp key"
                    )

                    if uid_idx is not None and password_idx is not None:
                        normalized = []
                        for values in raw_rows[1:]:
                            def value_at(idx):
                                return values[idx].strip() if idx is not None and idx < len(values) else ""

                            uid = value_at(uid_idx)
                            password = value_at(password_idx)
                            key = value_at(key_idx)
                            if not uid and not password and not key:
                                continue

                            lines = [f"Uid: {uid}", f"Password: {password}"]
                            if key:
                                lines.append(f"2f key: {key}")
                            normalized.append("\n".join(lines))

                        if normalized:
                            return normalized

                    result = [" | ".join(v for v in row if v) for row in raw_rows]
                    if result:
                        return result

            return []

    except zipfile.BadZipFile as e:
        raise RuntimeError(
            "XLSX fileটি valid Excel ZIP format নয় বা fileটি corrupt।"
        ) from e
    except ET.ParseError as e:
        raise RuntimeError(
            "XLSX file-এর XML data পড়া যায়নি; fileটি corrupt হতে পারে।"
        ) from e


def parse_stock_rows(raw_bytes, file_name):
    """Parse an uploaded stock source into individual sellable profile rows."""
    suffix = Path(file_name or "").suffix.lower()

    # ZIP upload: read supported files inside the archive and combine their rows.
    if suffix == ".zip":
        rows = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    inner_name = info.filename
                    inner_suffix = Path(inner_name).suffix.lower()
                    if inner_suffix not in (".txt", ".csv", ".tsv", ".xlsx", ".xlsm"):
                        continue
                    rows.extend(parse_stock_rows(z.read(info), inner_name))
        except zipfile.BadZipFile as e:
            raise RuntimeError("ZIP fileটি valid নয় বা corrupt।") from e
        return rows

    # XLSX/XLSM is parsed with Python's standard library.
    # This removes the previous openpyxl dependency completely.
    if suffix in (".xlsx", ".xlsm"):
        return _parse_xlsx_rows(raw_bytes)

    # Plain text / CSV / TSV. Every non-empty record becomes one stock item.
    try:
        decoded = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = None
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                decoded = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise RuntimeError("Text file-এর encoding পড়া যায়নি।")

    rows = []
    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        for values in csv.reader(io.StringIO(decoded), delimiter=delimiter):
            cells = [str(v).strip() for v in values if str(v).strip()]
            if cells:
                rows.append(" | ".join(cells))
    else:
        rows = [line.strip() for line in decoded.splitlines() if line.strip()]

    return rows


async def add_stock_file(update, context):
    product_id = context.user_data.get("stock_product_id")
    if not product_id:
        await update.message.reply_text(premiumize_html("❌ Stock session পাওয়া যায়নি। আবার Add Stock শুরু করুন।"), parse_mode="HTML")
        return

    document = update.message.document
    if not document:
        await update.message.reply_text(premiumize_html("❌ Document হিসেবে file পাঠান।"), parse_mode="HTML")
        return

    file_name = document.file_name or "uploaded_stock.txt"
    temp_path = UPLOAD_DIR / f"_stock_upload_{update.effective_user.id}_{document.file_unique_id}{Path(file_name).suffix}"

    try:
        # download_as_bytearray is more compatible across python-telegram-bot versions.
        tg_file = await document.get_file()
        downloaded = await tg_file.download_as_bytearray()
        raw = bytes(downloaded)

        if not raw:
            raise RuntimeError("Uploaded file is empty.")

        rows = parse_stock_rows(raw, file_name)
        if not rows:
            raise RuntimeError("File-এ কোনো non-empty profile/account row পাওয়া যায়নি।")

        con = db()
        try:
            con.execute("BEGIN IMMEDIATE")
            for row_number, content in enumerate(rows, start=1):
                con.execute("""
                    INSERT INTO stock(product_id, file_id, file_name, content, row_number)
                    VALUES (?, ?, ?, ?, ?)
                """, (product_id, document.file_id, file_name, content, row_number))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

        con = db()
        stock_now = con.execute(
            "SELECT COUNT(*) AS c FROM stock WHERE product_id=? AND sold=0",
            (product_id,)
        ).fetchone()["c"]
        product = con.execute(
            "SELECT name FROM products WHERE id=?", (product_id,)
        ).fetchone()
        con.close()

        await update.message.reply_text(
            premiumize_html(f"✅ <b>Stock uploaded successfully!</b>\n\n"
            f"🛍️ Product: <b>{escape(product['name'])}</b>\n"
            f"➕ এই file থেকে Stock যোগ হয়েছে: <b>{len(rows)}</b>\n"
            f"📦 Current Available Stock: <b>{stock_now}</b>\n\n"
            "প্রতিটি row আলাদা Stock হিসেবে সংরক্ষণ হয়েছে।\n"
            "User ১টি কিনলে ১টি row sold হবে এবং ১টি item-এর price balance থেকে কাটা হবে।"),
            parse_mode="HTML",
            reply_markup=admin_keyboard()
        )

    except Exception as e:
        logger.exception("Row-based stock upload failed")
        # Show the real reason to the admin instead of hiding it behind a generic error.
        await update.message.reply_text(
            premiumize_html(f"❌ <b>Stock upload failed</b>\n\n"
            f"কারণ: <code>{escape(str(e))}</code>\n\n"
            "Supported: TXT, CSV, TSV, XLSX/XLSM এবং ZIP। XLSX-এর জন্য openpyxl লাগবে না।"),
            parse_mode="HTML"
        )
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


async def list_products_admin(update, context):
    products = get_products()
    if not products:
        await update.message.reply_text(premiumize_html("📦 কোনো Product নেই।"), parse_mode="HTML")
        return

    lines = ["📋 <b>PRODUCTS</b>\n"]
    for p in products:
        lines.append(
            f"🆔 {p['id']} | 🛍️ {p['name']}\n"
            f"💰 {money(p['price'])} ৳ | 📦 Stock: {p['stock_count']}"
        )

    await update.message.reply_text(premiumize_html("\n\n".join(lines)), parse_mode="HTML")


async def delete_product_start(update, context):
    context.user_data["admin_state"] = "delete_product"
    await update.message.reply_text(premiumize_html("🗑️ যে Product delete করবেন তার ID লিখুন:"), parse_mode="HTML")


async def delete_product(update, context):
    try:
        pid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক Product ID লিখুন।"), parse_mode="HTML")
        return

    con = db()
    con.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
    con.commit()
    con.close()

    context.user_data.clear()
    await update.message.reply_text(
        premiumize_html("✅ Product deleted."),
        reply_markup=admin_keyboard()
    , parse_mode="HTML")


async def pending_deposits(update, context):
    if not is_admin(update.effective_user.id):
        return

    con = db()
    rows = con.execute("""
        SELECT d.*, u.username, u.first_name
        FROM deposits d
        LEFT JOIN users u ON u.user_id=d.user_id
        WHERE d.status='pending'
        ORDER BY d.id DESC
        LIMIT 30
    """).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(premiumize_html("✅ কোনো pending deposit নেই।"), parse_mode="HTML")
        return

    for r in rows:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "✅ Approve", callback_data=f"deposit_approve:{r['id']}"
            ),
            InlineKeyboardButton(
                "❌ Reject", callback_data=f"deposit_reject:{r['id']}"
            )
        ]])
        await update.message.reply_text(
            premiumize_html(f"💳 <b>Deposit #{r['id']}</b>\n\n"
            f"👤 Name: <b>{escape(r['first_name'] or '—')}</b>\n"
            f"🔹 Username: @{escape(r['username']) if r['username'] else '—'}\n"
            f"🆔 User ID: <code>{r['user_id']}</code>\n"
            f"🏷️ Category: <b>{escape(r['payment_method'] or '—')}</b>\n"
            f"💰 Amount: <b>{money(r['amount'])} ৳</b>\n"
            f"🧾 TRX: <code>{r['trx_id']}</code>"),
            parse_mode="HTML",
            reply_markup=kb
        )


async def approve_deposit(update, context):
    if not is_admin(update.effective_user.id):
        return

    try:
        did = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text(premiumize_html("Usage: /approve_deposit ID"), parse_mode="HTML")
        return

    con = db()
    row = con.execute(
        "SELECT * FROM deposits WHERE id=? AND status='pending'",
        (did,)
    ).fetchone()

    if not row:
        con.close()
        await update.message.reply_text(premiumize_html("❌ Deposit not found or already processed."), parse_mode="HTML")
        return

    con.execute(
        "UPDATE deposits SET status='approved' WHERE id=?",
        (did,)
    )
    con.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (row["amount"], row["user_id"])
    )
    referral_reward = apply_referral_reward(con, row)
    con.commit()
    con.close()

    try:
        await context.bot.send_message(
            row["user_id"],
            premiumize_html(f"✅ <b>Deposit Approved!</b>\n\n"
            f"💰 Added: <b>{money(row['amount'])} ৳</b>\n"
            f"💵 New Balance: <b>{money(get_balance(row['user_id']))} ৳</b>"),
            parse_mode="HTML"
        )
    except Exception:
        pass

    if referral_reward > 0:
        referrer_id = get_referrer_id(row["user_id"])
        if referrer_id:
            try:
                await context.bot.send_message(
                    referrer_id,
                    premiumize_html(f"🎉 <b>Referral Income Received!</b>\n\n"
                    f"👤 আপনার referred user Deposit করেছে: "
                    f"<b>{money(row['amount'])} ৳</b>\n"
                    f"💰 Referral Income (10%): "
                    f"<b>{money(referral_reward)} ৳</b>\n\n"
                    f"🛍️ এই Balance দিয়ে Product কেনা যাবে।"),
                    parse_mode="HTML"
                )
            except Exception:
                pass

    await update.message.reply_text(premiumize_html("✅ Deposit approved."), parse_mode="HTML")


async def reject_deposit(update, context):
    if not is_admin(update.effective_user.id):
        return

    try:
        did = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text(premiumize_html("Usage: /reject_deposit ID"), parse_mode="HTML")
        return

    con = db()
    cur = con.execute("""
        UPDATE deposits SET status='rejected'
        WHERE id=? AND status='pending'
    """, (did,))
    con.commit()
    con.close()

    await update.message.reply_text(
        premiumize_html("✅ Deposit rejected." if cur.rowcount else "❌ Deposit not found.")
    , parse_mode="HTML")


def user_management_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("👥 All Users")],
        [KeyboardButton("🚫 Block User"), KeyboardButton("🔓 Blocked User")],
        [KeyboardButton("➕ Add Balance"), KeyboardButton("➖ Remove Balance")],
        [KeyboardButton("🔙 Return")]
    ], resize_keyboard=True)


async def user_management(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        premiumize_html("👥 <b>USER MANAGEMENT</b>\n\nSelect an option:"),
        parse_mode="HTML",
        reply_markup=user_management_keyboard()
    )


async def all_users_admin(update, context):
    if not is_admin(update.effective_user.id):
        return
    con = db()
    rows = con.execute("SELECT user_id, username, first_name, balance, blocked FROM users ORDER BY user_id DESC LIMIT 100").fetchall()
    con.close()
    if not rows:
        await update.message.reply_text(premiumize_html("❌ কোনো user নেই।"), reply_markup=user_management_keyboard(), parse_mode="HTML")
        return
    lines = ["👥 <b>ALL USERS</b>\n"]
    for r in rows:
        username = f"@{r['username']}" if r['username'] else "—"
        status = "🚫 Blocked" if r['blocked'] else "🟢 Active"
        lines.append(
            f"👤 <b>{escape(r['first_name'] or 'User')}</b>\n"
            f"Username: <code>{escape(username)}</code>\n"
            f"User ID: <code>{r['user_id']}</code>\n"
            f"Balance: <b>{money(r['balance'])} ৳</b>\n"
            f"Status: {status}"
        )
    await update.message.reply_text(premiumize_html("\n\n".join(lines)), parse_mode="HTML", reply_markup=user_management_keyboard())


async def block_user_start(update, context):
    if not is_admin(update.effective_user.id): return
    context.user_data["admin_state"] = "block_user"
    await update.message.reply_text(premiumize_html("🚫 যে User ID block করতে চান সেটি লিখুন:"), reply_markup=back_keyboard(), parse_mode="HTML")


async def block_user(update, context):
    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক User ID দিন।"), parse_mode="HTML")
        return
    con = db()
    cur = con.execute("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))
    con.commit(); con.close()
    context.user_data.clear()
    await update.message.reply_text(
        premiumize_html("✅ User blocked." if cur.rowcount else "❌ User পাওয়া যায়নি."),
        reply_markup=user_management_keyboard()
    , parse_mode="HTML")


async def blocked_users_admin(update, context):
    if not is_admin(update.effective_user.id): return
    con = db()
    rows = con.execute("SELECT user_id, username, first_name, balance FROM users WHERE blocked=1 ORDER BY user_id DESC").fetchall()
    con.close()
    if not rows:
        await update.message.reply_text(premiumize_html("✅ কোনো blocked user নেই।"), reply_markup=user_management_keyboard(), parse_mode="HTML")
        return
    lines=["🚫 <b>BLOCKED USERS</b>\n"]
    for r in rows:
        username=f"@{r['username']}" if r['username'] else "—"
        lines.append(f"👤 <b>{escape(r['first_name'] or 'User')}</b>\nUsername: <code>{escape(username)}</code>\nUser ID: <code>{r['user_id']}</code>\nBalance: <b>{money(r['balance'])} ৳</b>")
    await update.message.reply_text(premiumize_html("\n\n".join(lines)), parse_mode="HTML", reply_markup=user_management_keyboard())


async def balance_change_start(update, context, mode):
    if not is_admin(update.effective_user.id): return
    context.user_data["balance_mode"] = mode
    context.user_data["admin_state"] = "balance_user_id"
    label = "Add" if mode == "add" else "Remove"
    await update.message.reply_text(premiumize_html(f"{ '➕' if mode == 'add' else '➖' } {label} Balance\n\nUser ID লিখুন:"), reply_markup=back_keyboard(), parse_mode="HTML")


async def balance_user_id(update, context):
    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক User ID দিন।"), parse_mode="HTML")
        return
    con=db(); row=con.execute("SELECT user_id,balance FROM users WHERE user_id=?",(user_id,)).fetchone(); con.close()
    if not row:
        await update.message.reply_text(premiumize_html("❌ User পাওয়া যায়নি। আবার User ID দিন:"), parse_mode="HTML")
        return
    context.user_data["balance_user_id"] = user_id
    context.user_data["admin_state"] = "balance_amount"
    context.user_data["balance_current"] = float(row["balance"])
    await update.message.reply_text(premiumize_html(f"Current Balance: {money(row['balance'])} ৳\n\nAmount লিখুন:"), parse_mode="HTML")


async def balance_amount(update, context):
    try:
        amount=float(update.message.text.strip())
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text(premiumize_html("❌ সঠিক amount দিন।"), parse_mode="HTML")
        return
    user_id=context.user_data["balance_user_id"]
    mode=context.user_data["balance_mode"]
    con=db()
    if mode == "add":
        con.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(amount,user_id))
    else:
        con.execute("UPDATE users SET balance=MAX(0,balance-?) WHERE user_id=?",(amount,user_id))
    con.commit()
    row=con.execute("SELECT balance FROM users WHERE user_id=?",(user_id,)).fetchone()
    con.close()
    new_balance=float(row["balance"])
    context.user_data.clear()
    action="added to" if mode=="add" else "removed from"
    await update.message.reply_text(premiumize_html(f"✅ {money(amount)} ৳ {action} User <code>{user_id}</code>.\n\nNew Balance: <b>{money(new_balance)} ৳</b>"), parse_mode="HTML", reply_markup=user_management_keyboard())


# ---------------- MESSAGE ROUTER ----------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    ensure_user(update.effective_user)
    if not is_admin(update.effective_user.id):
        con = db(); blocked = con.execute("SELECT blocked FROM users WHERE user_id=?", (update.effective_user.id,)).fetchone(); con.close()
        if blocked and blocked["blocked"]:
            await update.message.reply_text(premiumize_html("🚫 আপনার account blocked করা হয়েছে।"), parse_mode="HTML")
            return
    text = (update.message.text or "").strip()
    text = _BUTTON_ALIASES.get(text, text)
    uid = update.effective_user.id

    if text == "🔗 Referral":
        try:
            await show_referral(update, context)
        except Exception:
            logger.exception("Referral menu failed")
            await update.message.reply_text(
                premiumize_html("❌ Referral menu load করতে সমস্যা হয়েছে।"),
                reply_markup=main_keyboard(uid)
            , parse_mode="HTML")
        return

    # -------- Support: highest-priority menu route --------
    # Handle this before force-join and every state handler so the
    # Support button can never be swallowed by another flow.
    if text == "📞 Support":
        try:
            await user_support(update, context)
        except Exception:
            logger.exception("Support button failed")
            await update.message.reply_text(
                premiumize_html("📞 <b>Support</b>\n\n"
                "⚠️ Support menu load করতে সমস্যা হয়েছে। "
                "Admin-এর সাথে যোগাযোগ করুন।"),
                parse_mode="HTML",
                reply_markup=main_keyboard(uid)
            )
        return

    if text not in ("🔙 Return", "BACK", "/back", "✅ I Joined / Joined Check"):
        if not await check_force_join(update, context):
            return

    # Admin document upload
    if (
        update.message.document
        and is_admin(uid)
        and context.user_data.get("admin_state") == "database_upload"
    ):
        await database_upload(update, context)
        return

    if (
        update.message.document
        and is_admin(uid)
        and context.user_data.get("admin_state") == "stock_file"
    ):
        await add_stock_file(update, context)
        return

    if text == "✅ I Joined / Joined Check":
        if await check_force_join(update, context):
            await send_home(update, context, "✅ Join verified successfully.")
        return

    # Global return
    if text in ("🔙 Return", "BACK", "/back"):
        context.user_data.clear()
        await send_home(update, context)
        return

    # -------- admin states --------
    astate = context.user_data.get("admin_state")
    if is_admin(uid) and astate == "product_name":
        await add_product_name(update, context)
        return
    if is_admin(uid) and astate == "product_price":
        await add_product_price(update, context)
        return
    if is_admin(uid) and astate == "product_description":
        await add_product_description(update, context)
        return
    if is_admin(uid) and astate == "stock_product":
        await add_stock_product(update, context)
        return
    if is_admin(uid) and astate == "delete_product":
        await delete_product(update, context)
        return
    if is_admin(uid) and astate == "broadcast":
        await do_broadcast(update, context)
        return
    if is_admin(uid) and astate == "deposit_min":
        await deposit_min_admin(update, context)
        return
    if is_admin(uid) and astate == "deposit_max":
        await deposit_max_admin(update, context)
        return
    if is_admin(uid) and astate == "user_txt_upload":
        await upload_user_txt(update, context)
        return
    if is_admin(uid) and astate == "force_join_add_chat_id":
        await force_join_add_chat_id(update, context)
        return
    if is_admin(uid) and astate == "force_join_add_title":
        await force_join_add_title(update, context)
        return
    if is_admin(uid) and astate == "force_join_add_invite":
        await force_join_add_invite(update, context)
        return
    if is_admin(uid) and astate == "force_join_remove":
        await force_join_remove_save(update, context)
        return
    if is_admin(uid) and astate == "payment_method_name":
        await add_payment_method_name(update, context)
        return
    if is_admin(uid) and astate == "payment_method_account":
        await add_payment_method_account(update, context)
        return
    if is_admin(uid) and astate == "payment_method_min":
        await add_payment_method_min(update, context)
        return
    if is_admin(uid) and astate == "payment_method_max":
        await add_payment_method_max(update, context)
        return
    if is_admin(uid) and astate == "payment_method_instructions":
        await add_payment_method_instructions(update, context)
        return
    if is_admin(uid) and astate == "support_name":
        await add_support_name(update, context)
        return
    if is_admin(uid) and astate == "support_contact":
        await add_support_contact(update, context)
        return
    if is_admin(uid) and astate == "support_description":
        await add_support_description(update, context)
        return
    if is_admin(uid) and astate == "support_reply":
        await support_ticket_reply_save(update, context)
        return
    if is_admin(uid) and astate == "block_user":
        await block_user(update, context)
        return
    if is_admin(uid) and astate == "balance_user_id":
        await balance_user_id(update, context)
        return
    if is_admin(uid) and astate == "balance_amount":
        await balance_amount(update, context)
        return
    if is_admin(uid) and astate == "clear_all_data_confirm":
        if text == "⚠️ CONFIRM CLEAR ALL DATA":
            await clear_all_data_confirm(update, context)
        elif text == "❌ Cancel":
            context.user_data.clear()
            await update.message.reply_text(
                premiumize_html("✅ Cancelled."),
                reply_markup=admin_keyboard()
            , parse_mode="HTML")
        return

    # -------- deposit states --------
    state = context.user_data.get("state")
    if state == "deposit_amount":
        await deposit_amount(update, context, text)
        return
    if state == "deposit_trx":
        await deposit_trx(update, context, text)
        return
    if state == "support_message":
        await support_message(update, context, text)
        return

    # -------- buy quantity --------
    if context.user_data.get("buy_product_id"):
        try:
            qty = int(text)
            if await process_quantity(update, context, qty):
                return
        except ValueError:
            pass

    # -------- main menu --------
    if text == "🛍️ Buy Product":
        await show_buy_menu(update, context)
        return

    if text == "💳 Deposit Money":
        await deposit_start(update, context)
        return

    if text == "💰 My Balance":
        await show_balance(update, context)
        return

    if text == "💵 Price List":
        await show_price_list(update, context)
        return

    if text == "⚙️ Admin Panel" and is_admin(uid):
        await admin_panel(update, context)
        return

    # -------- database menu --------
    if is_admin(uid):
        if text == "🗄️ Database":
            await database_admin(update, context)
            return
        if text == "📥 Download Database":
            await database_download(update, context)
            return
        if text == "📤 Upload Database":
            await database_upload_start(update, context)
            return
        if text == "📥 Download user.txt":
            await download_user_txt(update, context)
            return
        if text == "📤 Upload user.txt":
            await upload_user_txt_start(update, context)
            return
        if text == "🗑️ Clear All Data":
            await clear_all_data_start(update, context)
            return
        if text == "❌ Cancel":
            context.user_data.clear()
            await update.message.reply_text(
                premiumize_html("✅ Cancelled."),
                reply_markup=admin_keyboard()
            , parse_mode="HTML")
            return

    # -------- admin menu --------
    if is_admin(uid):
        if text == "📦 Product Management":
            await product_management(update, context)
            return
        if text == "➕ Add Product":
            await add_product_start(update, context)
            return
        if text == "📦 Add Stock":
            await add_stock_start(update, context)
            return
        if text == "🗑️ Delete Product":
            await delete_product_start(update, context)
            return
        if text == "📋 Products":
            await list_products_admin(update, context)
            return
        if text == "💰 Pending Deposits":
            await pending_deposits(update, context)
            return
        if text == "👥 User Management":
            await user_management(update, context)
            return
        if text == "👥 All Users":
            await all_users_admin(update, context)
            return
        if text == "🚫 Block User":
            await block_user_start(update, context)
            return
        if text == "🔓 Blocked User":
            await blocked_users_admin(update, context)
            return
        if text == "➕ Add Balance":
            await balance_change_start(update, context, "add")
            return
        if text == "➖ Remove Balance":
            await balance_change_start(update, context, "remove")
            return
        if text == "📢 Broadcast":
            await broadcast_start(update, context)
            return
        if text == "📊 Statistics":
            await admin_statistics(update, context)
            return
        if text == "📞 Support Management":
            await support_management(update, context)
            return
        if text == "➕ Add Support":
            await add_support_start(update, context)
            return
        if text == "🗑️ Delete Support":
            await delete_support_start(update, context)
            return
        if text == "📋 Support List":
            await support_list(update, context)
            return
        if text == "Pending Support":
            await pending_support(update, context)
            return
        if text == "💳 Payment Management":
            await payment_management(update, context)
            return
        if text == "➕ Add Payment Method":
            await add_payment_method_start(update, context)
            return
        if text == "🗑️ Delete Payment Method":
            await delete_payment_method_start(update, context)
            return
        if text == "🔘 Enable / Disable Payment Method":
            await toggle_payment_method_start(update, context)
            return
        if text == "📋 Payment Methods":
            await payment_methods_list(update, context)
            return
        if text == "📢 Force Join":
            await force_join_management(update, context)
            return
        if text == "➕ Add Channel":
            await force_join_add_start(update, context)
            return
        if text == "🗑️ Remove Channel":
            await force_join_remove_start(update, context)
            return
        if text == "📋 View List":
            await force_join_view_list(update, context)
            return

    # -------- dynamic product button --------
    if text.startswith("🛒 "):
        product = find_product_by_button(text)
        if product:
            await show_product(update, context, product)
            return

    await update.message.reply_text(
        premiumize_html("❓ Menu থেকে একটি option নির্বাচন করুন।"),
        reply_markup=main_keyboard(uid)
    , parse_mode="HTML")



# ---------------- COMMANDS ----------------

async def help_command(update, context):
    await update.message.reply_text(
        premiumize_html("/start - Main Menu\n"
        "/help - Help\n\n"
        "Admin Panel থেকে সব management button দিয়েই করা যাবে।")    , parse_mode="HTML")


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN সেট করুন। চাইলে ফাইলের BOT_TOKEN default-এ আপনার token রাখতে পারেন।"
        )

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    if AUTO_PAYMENT_ENABLED:
        start_payment_webhook()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("approve_deposit", approve_deposit))
    app.add_handler(CommandHandler("reject_deposit", reject_deposit))
    app.add_handler(CommandHandler("reply_ticket", reply_ticket))
    app.add_handler(CallbackQueryHandler(user_payment_callback, pattern=r"^user_payment:"))
    app.add_handler(CallbackQueryHandler(force_join_callback, pattern=r"^force_join_check$"))
    app.add_handler(CallbackQueryHandler(admin_payment_callback, pattern=r"^payment_"))
    app.add_handler(CallbackQueryHandler(support_admin_callback, pattern=r"^support_"))
    app.add_handler(CallbackQueryHandler(user_support_callback, pattern=r"^user_support_"))
    app.add_handler(CallbackQueryHandler(referral_callback, pattern=r"^referral_"))
    # Specific 2FA callbacks MUST be registered before the catch-all admin callback.
    app.add_handler(CallbackQueryHandler(get_2fa_code_callback, pattern=r"^get_2fa:"))
    app.add_handler(CallbackQueryHandler(refresh_2fa_code_callback, pattern=r"^refresh_2fa:"))
    app.add_handler(CallbackQueryHandler(next_purchase_callback, pattern=r"^next_purchase:"))
    app.add_handler(CallbackQueryHandler(admin_callback))

    # Document handler is routed through the same function so stock uploads
    # can be handled without adding a separate state machine.
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_message)
    )

    logger.info("Product Selling Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
