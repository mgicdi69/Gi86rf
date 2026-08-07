import os
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# PRODUCT SELLING BOT - UPDATED
# ============================================================
# IMPORTANT:
# 1) Put your Bot Token in BOT_TOKEN.
# 2) Put your Telegram numeric ID in ADMIN_IDS.
# 3) Stock file: EACH NON-EMPTY LINE = ONE SELLABLE ACCOUNT/ITEM.
# 4) Telegram ReplyKeyboard buttons cannot have custom colors.
#    The requested layout is kept as 2 + 2 + 2 + Support.
# ============================================================

BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"
ADMIN_IDS = {123456789}

DB_FILE = os.getenv("DB_FILE", "store.db")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "product_files"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAIL_INBOX_URL = "https://dongvanfb.net/get_code_mail/"

PAYMENT_INFO = os.getenv(
    "PAYMENT_INFO",
    "💳 Payment Methods\n\n"
    "bKash: YOUR_BKASH_NUMBER\n"
    "Nagad: YOUR_NAGAD_NUMBER\n\n"
    "টাকা পাঠানোর পর আপনার Transaction ID পাঠান।",
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------- DATABASE ----------------

def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            language TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)

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

    # One row = one sellable stock item/account.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            file_id TEXT DEFAULT '',
            file_name TEXT DEFAULT '',
            item_text TEXT DEFAULT '',
            sold INTEGER DEFAULT 0,
            sold_to INTEGER,
            sold_at TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    """)

    # Migration for older databases created by the previous version.
    cols = {r["name"] for r in cur.execute("PRAGMA table_info(stock)").fetchall()}
    if "item_text" not in cols:
        cur.execute("ALTER TABLE stock ADD COLUMN item_text TEXT DEFAULT ''")

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

    # Defaults. Admin can change min/max and support from Admin Panel.
    defaults = {
        "deposit_min": "10",
        "deposit_max": "100000",
        "support_username": "",
    }
    for key, value in defaults.items():
        cur.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )

    con.commit()
    con.close()


def setting(key, default=""):
    con = db()
    row = con.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    con.close()
    return row["value"] if row else default


def set_setting(key, value):
    con = db()
    con.execute("""
        INSERT INTO settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))
    con.commit()
    con.close()


def ensure_user(user):
    con = db()
    con.execute("""
        INSERT INTO users(user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        now(),
    ))
    con.commit()
    con.close()


def get_language(user_id):
    con = db()
    row = con.execute(
        "SELECT language FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    con.close()
    return (row["language"] or "") if row else ""


def set_language(user_id, lang):
    if lang not in ("bn", "en"):
        return
    con = db()
    con.execute(
        "UPDATE users SET language=? WHERE user_id=?", (lang, user_id)
    )
    con.commit()
    con.close()


def get_balance(user_id):
    con = db()
    row = con.execute(
        "SELECT balance FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    con.close()
    return float(row["balance"]) if row else 0.0


def add_balance(user_id, amount):
    con = db()
    con.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (amount, user_id),
    )
    con.commit()
    con.close()


def money(value):
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ---------------- TRANSLATIONS ----------------

T = {
    "bn": {
        "welcome": (
            "🛍️ <b>Welcome to Product Store!</b>\n\n"
            "নিচের মেনু থেকে Product কিনুন, Balance দেখুন এবং Deposit করুন।"
        ),
        "buy": "🛍️ Buy Product",
        "deposit": "💳 Deposit Money",
        "balance": "💰 My Balance",
        "prices": "💵 Price List",
        "language": "🌐 Choose Language",
        "mail": "📩 Mail Inbox",
        "support": "📞 Support",
        "admin": "⚙️ Admin Panel",
        "return": "🔙 Return",
        "choose_product": "🛍️ <b>Product নির্বাচন করুন</b>\n\nনিচের Product থেকে একটি নির্বাচন করুন।",
        "no_products": "❌ বর্তমানে কোনো Product available নেই।",
        "price_empty": "📋 Price List এখন খালি।",
        "balance_text": "💰 <b>Your Balance</b>\n\n💵 Balance: <b>{balance} ৳</b>",
        "language_choose": "🌐 <b>Language নির্বাচন করুন</b>",
        "language_saved": "✅ ভাষা পরিবর্তন করা হয়েছে।",
        "mail_text": "📩 <b>Mail Inbox</b>\n\nনিচের বাটনে Mail Inbox খুলুন।",
        "open_mail": "📩 Open Mail Inbox",
        "support_unset": "❌ Admin এখনো Support username সেট করেনি।",
        "open_support": "📞 Contact Support",
        "out_stock": "❌ <b>Currently out of stock.</b>",
        "product_info": (
            "🛒 <b>{name}</b>\n\n{description}\n\n"
            "📦 Available Stock: <b>{stock}</b>\n"
            "💵 Price per item: <b>{price} ৳</b>\n\n"
            "কতটি কিনতে চান? একটি সংখ্যা লিখুন।"
        ),
        "quantity_error": "❌ সঠিক quantity লিখুন।",
        "not_enough_stock": "❌ <b>Not enough stock.</b>\n\nAvailable: {available}\nRequested: {requested}",
        "insufficient": "❌ <b>Insufficient balance!</b>\n\nTotal Price: {total} ৳\nYour Balance: {balance} ৳",
        "purchase_success": (
            "✅ <b>Purchase successful!</b>\n\n"
            "🛍️ Product: <b>{name}</b>\n"
            "🔢 Quantity: <b>{quantity}</b>\n"
            "💵 Total Price: <b>{total} ৳</b>\n"
            "💰 New Balance: <b>{balance} ৳</b>\n\n"
            "📦 আপনার purchased item/account নিচে পাঠানো হচ্ছে..."
        ),
        "item": "📦 <b>{name}</b>\n\n<code>{item}</code>",
        "deposit_start": (
            "💳 <b>Deposit Money</b>\n\n"
            "কত টাকা Deposit করতে চান?\n"
            "Minimum: {min} ৳\nMaximum: {max} ৳\n\n"
            "উদাহরণ: 100"
        ),
        "invalid_amount": "❌ সঠিক amount লিখুন।",
        "range_error": "❌ Deposit amount অবশ্যই {min} থেকে {max} ৳ এর মধ্যে হতে হবে।",
        "deposit_trx": "{payment}\n\n💵 Deposit Amount: <b>{amount} ৳</b>\n\nএখন আপনার Transaction ID পাঠান।",
        "deposit_submitted": (
            "✅ <b>Deposit request submitted!</b>\n\n"
            "Admin verification-এর পর আপনার balance-এ টাকা যোগ হবে।"
        ),
        "deposit_expired": "❌ Deposit session expired.",
        "menu_error": "❓ Menu থেকে একটি option নির্বাচন করুন।",
        "no_support": "❌ Support username সেট করা নেই।",
    },
    "en": {
        "welcome": (
            "🛍️ <b>Welcome to Product Store!</b>\n\n"
            "Use the menu below to buy products, check balance and deposit money."
        ),
        "buy": "🛍️ Buy Product",
        "deposit": "💳 Deposit Money",
        "balance": "💰 My Balance",
        "prices": "💵 Price List",
        "language": "🌐 Choose Language",
        "mail": "📩 Mail Inbox",
        "support": "📞 Support",
        "admin": "⚙️ Admin Panel",
        "return": "🔙 Return",
        "choose_product": "🛍️ <b>Select a Product</b>\n\nChoose one product below.",
        "no_products": "❌ No products are currently available.",
        "price_empty": "📋 Price List is empty.",
        "balance_text": "💰 <b>Your Balance</b>\n\n💵 Balance: <b>{balance} ৳</b>",
        "language_choose": "🌐 <b>Choose your language</b>",
        "language_saved": "✅ Language changed successfully.",
        "mail_text": "📩 <b>Mail Inbox</b>\n\nOpen the Mail Inbox using the button below.",
        "open_mail": "📩 Open Mail Inbox",
        "support_unset": "❌ Support username has not been set by Admin yet.",
        "open_support": "📞 Contact Support",
        "out_stock": "❌ <b>Currently out of stock.</b>",
        "product_info": (
            "🛒 <b>{name}</b>\n\n{description}\n\n"
            "📦 Available Stock: <b>{stock}</b>\n"
            "💵 Price per item: <b>{price} ৳</b>\n\n"
            "How many do you want to buy? Enter a number."
        ),
        "quantity_error": "❌ Enter a valid quantity.",
        "not_enough_stock": "❌ <b>Not enough stock.</b>\n\nAvailable: {available}\nRequested: {requested}",
        "insufficient": "❌ <b>Insufficient balance!</b>\n\nTotal Price: {total} ৳\nYour Balance: {balance} ৳",
        "purchase_success": (
            "✅ <b>Purchase successful!</b>\n\n"
            "🛍️ Product: <b>{name}</b>\n"
            "🔢 Quantity: <b>{quantity}</b>\n"
            "💵 Total Price: <b>{total} ৳</b>\n"
            "💰 New Balance: <b>{balance} ৳</b>\n\n"
            "📦 Your purchased item/account is being sent below..."
        ),
        "item": "📦 <b>{name}</b>\n\n<code>{item}</code>",
        "deposit_start": (
            "💳 <b>Deposit Money</b>\n\n"
            "How much do you want to deposit?\n"
            "Minimum: {min} ৳\nMaximum: {max} ৳\n\n"
            "Example: 100"
        ),
        "invalid_amount": "❌ Enter a valid amount.",
        "range_error": "❌ Deposit amount must be between {min} and {max} ৳.",
        "deposit_trx": "{payment}\n\n💵 Deposit Amount: <b>{amount} ৳</b>\n\nNow send your Transaction ID.",
        "deposit_submitted": (
            "✅ <b>Deposit request submitted!</b>\n\n"
            "Your balance will be updated after Admin verification."
        ),
        "deposit_expired": "❌ Deposit session expired.",
        "menu_error": "❓ Please choose an option from the menu.",
        "no_support": "❌ Support username is not set.",
    },
}


def lang_of(user_id):
    return get_language(user_id) or "bn"


def tr(user_id, key, **kwargs):
    lang = lang_of(user_id)
    return T[lang][key].format(**kwargs)


# ---------------- KEYBOARDS ----------------

def main_keyboard(user_id):
    # Telegram ReplyKeyboardMarkup does NOT support per-button colors.
    # Layout requested by user: first 2, middle 2, next 2, then Support.
    lang = lang_of(user_id)
    t = T[lang]

    rows = [
        [KeyboardButton(t["buy"]), KeyboardButton(t["deposit"])],
        [KeyboardButton(t["balance"]), KeyboardButton(t["prices"])],
        [KeyboardButton(t["language"]), KeyboardButton(t["mail"])],
        [KeyboardButton(t["support"])],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(t["admin"])])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def language_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🇧🇩 বাংলা"), KeyboardButton("🇬🇧 English")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def back_keyboard(user_id):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(T[lang_of(user_id)]["return"])]],
        resize_keyboard=True,
    )


def product_keyboard(products, user_id):
    rows = []
    pair = []
    for p in products:
        pair.append(KeyboardButton(f"🛒 {p['name']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([KeyboardButton(T[lang_of(user_id)]["return"])])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def admin_keyboard(user_id):
    lang = lang_of(user_id)
    if lang == "en":
        rows = [
            [KeyboardButton("➕ Add Product"), KeyboardButton("📦 Add Stock")],
            [KeyboardButton("🗑️ Delete Product"), KeyboardButton("📋 Products")],
            [KeyboardButton("💰 Pending Deposits"), KeyboardButton("👥 Users")],
            [KeyboardButton("📢 Broadcast"), KeyboardButton("💰 Deposit Limits")],
            [KeyboardButton("📞 Support Settings"), KeyboardButton("🔙 Return")],
        ]
    else:
        rows = [
            [KeyboardButton("➕ Add Product"), KeyboardButton("📦 Add Stock")],
            [KeyboardButton("🗑️ Delete Product"), KeyboardButton("📋 Products")],
            [KeyboardButton("💰 Pending Deposits"), KeyboardButton("👥 Users")],
            [KeyboardButton("📢 Broadcast"), KeyboardButton("💰 Deposit Limits")],
            [KeyboardButton("📞 Support Settings"), KeyboardButton("🔙 Return")],
        ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ---------------- HELPERS ----------------

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
        "SELECT * FROM products WHERE name=? AND active=1", (name,)
    ).fetchone()
    con.close()
    return row


async def send_home(update, context, text=None):
    uid = update.effective_user.id
    if text is None:
        text = tr(uid, "welcome")
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(uid),
    )


def support_url():
    username = setting("support_username", "").strip()
    if not username:
        return ""
    if username.startswith("https://t.me/"):
        return username
    username = username.lstrip("@").strip()
    return f"https://t.me/{username}"


# ---------------- START / LANGUAGE ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    context.user_data.clear()

    if not get_language(update.effective_user.id):
        await update.message.reply_text(
            "🌐 <b>Choose Language / ভাষা নির্বাচন করুন</b>",
            parse_mode="HTML",
            reply_markup=language_keyboard(),
        )
        return

    await send_home(update, context)


async def choose_language(update, context, lang):
    set_language(update.effective_user.id, lang)
    context.user_data.clear()
    await update.message.reply_text(
        tr(update.effective_user.id, "language_saved"),
        reply_markup=main_keyboard(update.effective_user.id),
    )


# ---------------- USER MENU ----------------

async def show_buy_menu(update, context):
    uid = update.effective_user.id
    products = get_products()
    if not products:
        await update.message.reply_text(
            tr(uid, "no_products"),
            reply_markup=main_keyboard(uid),
        )
        return
    await update.message.reply_text(
        tr(uid, "choose_product"),
        parse_mode="HTML",
        reply_markup=product_keyboard(products, uid),
    )


async def show_price_list(update, context):
    uid = update.effective_user.id
    products = get_products()
    if not products:
        await update.message.reply_text(
            tr(uid, "price_empty"),
            reply_markup=main_keyboard(uid),
        )
        return

    lines = ["💵 <b>PRICE LIST</b>\n"]
    for p in products:
        lines.append(
            f"🛍️ <b>{p['name']}</b>\n"
            f"💰 Price: {money(p['price'])} ৳\n"
            f"📦 Stock: {p['stock_count']}"
        )
    await update.message.reply_text(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_keyboard(uid),
    )


async def show_balance(update, context):
    uid = update.effective_user.id
    await update.message.reply_text(
        tr(uid, "balance_text", balance=money(get_balance(uid))),
        parse_mode="HTML",
        reply_markup=main_keyboard(uid),
    )


async def show_language(update, context):
    await update.message.reply_text(
        tr(update.effective_user.id, "language_choose"),
        parse_mode="HTML",
        reply_markup=language_keyboard(),
    )


async def show_mail(update, context):
    uid = update.effective_user.id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(uid, "open_mail"), url=MAIL_INBOX_URL)]
    ])
    await update.message.reply_text(
        tr(uid, "mail_text"),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def show_support(update, context):
    uid = update.effective_user.id
    url = support_url()
    if not url:
        await update.message.reply_text(
            tr(uid, "support_unset"),
            reply_markup=main_keyboard(uid),
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(uid, "open_support"), url=url)]
    ])
    await update.message.reply_text(
        f"📞 <b>Support</b>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ---------------- PRODUCT BUY FLOW ----------------

async def show_product(update, context, product):
    uid = update.effective_user.id
    context.user_data["buy_product_id"] = product["id"]

    con = db()
    stock = con.execute(
        "SELECT COUNT(*) AS c FROM stock WHERE product_id=? AND sold=0",
        (product["id"],),
    ).fetchone()["c"]
    con.close()

    if stock <= 0:
        await update.message.reply_text(
            tr(uid, "out_stock"),
            parse_mode="HTML",
            reply_markup=product_keyboard(get_products(), uid),
        )
        return

    description = product["description"] or "No description available."
    await update.message.reply_text(
        tr(
            uid,
            "product_info",
            name=product["name"],
            description=description,
            stock=stock,
            price=money(product["price"]),
        ),
        parse_mode="HTML",
        reply_markup=back_keyboard(uid),
    )


async def process_quantity(update, context, quantity):
    uid = update.effective_user.id
    product_id = context.user_data.get("buy_product_id")
    if not product_id:
        return False

    if quantity <= 0:
        await update.message.reply_text(tr(uid, "quantity_error"))
        return True

    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")

        product = con.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
        ).fetchone()
        if not product:
            con.rollback()
            await update.message.reply_text("❌ Product not found.")
            return True

        # Select and lock by transaction; these rows are the actual items.
        stock_rows = con.execute("""
            SELECT * FROM stock
            WHERE product_id=? AND sold=0
            ORDER BY id ASC LIMIT ?
        """, (product_id, quantity)).fetchall()

        available = len(stock_rows)
        total = float(product["price"]) * quantity

        if available < quantity:
            con.rollback()
            await update.message.reply_text(
                tr(uid, "not_enough_stock", available=available, requested=quantity),
                parse_mode="HTML",
                reply_markup=product_keyboard(get_products(), uid),
            )
            return True

        row = con.execute(
            "SELECT balance FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        balance = float(row["balance"]) if row else 0.0

        if balance < total:
            con.rollback()
            await update.message.reply_text(
                tr(uid, "insufficient", total=money(total), balance=money(balance)),
                parse_mode="HTML",
                reply_markup=product_keyboard(get_products(), uid),
            )
            return True

        con.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=?",
            (total, uid),
        )

        for stock_row in stock_rows:
            con.execute("""
                UPDATE stock
                SET sold=1, sold_to=?, sold_at=?
                WHERE id=? AND sold=0
            """, (uid, now(), stock_row["id"]))

        con.execute("""
            INSERT INTO orders(user_id, product_id, quantity, total, status, created_at)
            VALUES (?, ?, ?, ?, 'completed', ?)
        """, (uid, product_id, quantity, total, now()))

        con.commit()

    except Exception:
        con.rollback()
        logger.exception("Purchase failed")
        await update.message.reply_text("❌ Purchase failed. Please try again.")
        return True
    finally:
        con.close()

    context.user_data.pop("buy_product_id", None)
    new_balance = get_balance(uid)

    await update.message.reply_text(
        tr(
            uid,
            "purchase_success",
            name=product["name"],
            quantity=quantity,
            total=money(total),
            balance=money(new_balance),
        ),
        parse_mode="HTML",
        reply_markup=main_keyboard(uid),
    )

    # Each selected stock row is delivered exactly once.
    for row in stock_rows:
        item = (row["item_text"] or "").strip()
        if item:
            try:
                await update.message.reply_text(
                    tr(uid, "item", name=product["name"], item=item),
                    parse_mode="HTML",
                )
            except Exception:
                await update.message.reply_text(
                    f"📦 {product['name']}\n\n{item}"
                )
        elif row["file_id"]:
            try:
                await context.bot.send_document(
                    chat_id=uid,
                    document=row["file_id"],
                    caption=f"📦 {product['name']}\n✅ Purchased successfully",
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"📦 {product['name']}\n\n{row['file_id']}",
                )

    return True


# ---------------- DEPOSIT ----------------

async def deposit_start(update, context):
    uid = update.effective_user.id
    min_amt = float(setting("deposit_min", "10"))
    max_amt = float(setting("deposit_max", "100000"))

    context.user_data["state"] = "deposit_amount"
    await update.message.reply_text(
        tr(uid, "deposit_start", min=money(min_amt), max=money(max_amt)),
        parse_mode="HTML",
        reply_markup=back_keyboard(uid),
    )


async def deposit_amount(update, context, text):
    uid = update.effective_user.id
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(tr(uid, "invalid_amount"))
        return

    min_amt = float(setting("deposit_min", "10"))
    max_amt = float(setting("deposit_max", "100000"))

    if amount < min_amt or amount > max_amt:
        await update.message.reply_text(
            tr(uid, "range_error", min=money(min_amt), max=money(max_amt))
        )
        return

    context.user_data["deposit_amount"] = amount
    context.user_data["state"] = "deposit_trx"

    await update.message.reply_text(
        tr(
            uid,
            "deposit_trx",
            payment=PAYMENT_INFO,
            amount=money(amount),
        ),
        parse_mode="HTML",
        reply_markup=back_keyboard(uid),
    )


async def deposit_trx(update, context, trx):
    uid = update.effective_user.id
    amount = context.user_data.get("deposit_amount")

    if not amount:
        context.user_data.clear()
        await update.message.reply_text(
            tr(uid, "deposit_expired"),
            reply_markup=main_keyboard(uid),
        )
        return

    con = db()
    cur = con.execute("""
        INSERT INTO deposits(user_id, amount, trx_id, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
    """, (uid, amount, trx, now()))
    deposit_id = cur.lastrowid
    con.commit()
    con.close()

    user = update.effective_user
    username = f"@{user.username}" if user.username else "N/A"

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"💳 <b>NEW DEPOSIT</b>\n\n"
                f"User ID: <code>{uid}</code>\n"
                f"Username: <b>{username}</b>\n"
                f"Name: <b>{user.first_name or ''}</b>\n"
                f"Amount: <b>{money(amount)} ৳</b>\n"
                f"TRX ID: <code>{trx}</code>\n"
                f"Deposit ID: <code>{deposit_id}</code>\n\n"
                f"Approve: /approve_deposit {deposit_id}\n"
                f"Reject: /reject_deposit {deposit_id}",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Could not notify admin about deposit")

    context.user_data.clear()
    await update.message.reply_text(
        tr(uid, "deposit_submitted"),
        parse_mode="HTML",
        reply_markup=main_keyboard(uid),
    )


# ---------------- ADMIN ----------------

async def admin_panel(update, context):
    if not is_admin(update.effective_user.id):
        return
    uid = update.effective_user.id
    await update.message.reply_text(
        "⚙️ <b>ADMIN PANEL</b>\n\n"
        "Product, Stock, Deposit, Users, Broadcast, Deposit Limits এবং Support settings এখান থেকে control করুন.",
        parse_mode="HTML",
        reply_markup=admin_keyboard(uid),
    )


async def add_product_start(update, context):
    context.user_data["admin_state"] = "product_name"
    await update.message.reply_text(
        "➕ Product Name লিখুন:",
        reply_markup=back_keyboard(update.effective_user.id),
    )


async def add_product_name(update, context):
    context.user_data["new_product_name"] = update.message.text.strip()
    context.user_data["admin_state"] = "product_price"
    await update.message.reply_text("💰 Product Price লিখুন:")


async def add_product_price(update, context):
    try:
        price = float(update.message.text.strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ সঠিক price লিখুন।")
        return

    context.user_data["new_product_price"] = price
    context.user_data["admin_state"] = "product_description"
    await update.message.reply_text("📝 Product Description লিখুন:")


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
        f"✅ <b>Product created successfully!</b>\n\n"
        f"🛍️ Name: {name}\n"
        f"💰 Price: {money(price)} ৳\n"
        f"🆔 Product ID: {product_id}\n\n"
        f"এখন 📦 Add Stock থেকে account file যোগ করুন।",
        parse_mode="HTML",
        reply_markup=admin_keyboard(update.effective_user.id),
    )


async def add_stock_start(update, context):
    products = get_products()
    if not products:
        await update.message.reply_text("❌ আগে Product তৈরি করুন।")
        return

    context.user_data["admin_state"] = "stock_product"
    lines = ["📦 কোন Product-এ Stock যোগ করবেন?\n"]
    for p in products:
        lines.append(f"{p['id']} = {p['name']}")

    await update.message.reply_text(
        "\n".join(lines) + "\n\nProduct ID লিখুন:"
    )


async def add_stock_product(update, context):
    try:
        product_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ সঠিক Product ID লিখুন।")
        return

    con = db()
    product = con.execute(
        "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
    ).fetchone()
    con.close()

    if not product:
        await update.message.reply_text("❌ Product পাওয়া যায়নি।")
        return

    context.user_data["stock_product_id"] = product_id
    context.user_data["admin_state"] = "stock_file"

    await update.message.reply_text(
        f"📦 <b>{product['name']}</b> selected.\n\n"
        "এখন একটি .txt / text Document পাঠান।\n\n"
        "⚠️ <b>প্রতিটি non-empty row/line = ১টি Stock.</b>\n"
        "উদাহরণ:\n"
        "email1:pass1\n"
        "email2:pass2\n"
        "email3:pass3\n\n"
        "এখানে Stock হবে 3।\n"
        "Purchase হলে ১টি row User-কে দেওয়া হবে এবং Stock ১ কমে যাবে।",
        parse_mode="HTML",
    )


async def add_stock_file(update, context):
    product_id = context.user_data.get("stock_product_id")
    if not product_id:
        return

    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Document হিসেবে file পাঠান।")
        return

    try:
        tg_file = await document.get_file()
        raw = await tg_file.download_as_bytearray()

        # Try UTF-8 first, then common fallback encodings.
        try:
            content = bytes(raw).decode("utf-8-sig")
        except UnicodeDecodeError:
            content = bytes(raw).decode("utf-8", errors="replace")

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            await update.message.reply_text("❌ File-এ কোনো valid row পাওয়া যায়নি।")
            return

        con = db()
        for line in lines:
            con.execute("""
                INSERT INTO stock(product_id, file_id, file_name, item_text)
                VALUES (?, '', ?, ?)
            """, (product_id, document.file_name or "", line))
        con.commit()

        product = con.execute(
            "SELECT name FROM products WHERE id=?", (product_id,)
        ).fetchone()
        stock_count = con.execute(
            "SELECT COUNT(*) AS c FROM stock WHERE product_id=? AND sold=0",
            (product_id,),
        ).fetchone()["c"]
        con.close()

        await update.message.reply_text(
            f"✅ <b>Stock uploaded successfully!</b>\n\n"
            f"🛍️ Product: <b>{product['name']}</b>\n"
            f"➕ Added: <b>{len(lines)}</b> accounts/items\n"
            f"📦 Current Stock: <b>{stock_count}</b>\n\n"
            f"প্রতিটি row আলাদা Stock হিসেবে রাখা হয়েছে।",
            parse_mode="HTML",
            reply_markup=admin_keyboard(update.effective_user.id),
        )

    except Exception:
        logger.exception("Stock upload failed")
        await update.message.reply_text(
            "❌ Stock file process করা যায়নি। .txt file দিয়ে আবার চেষ্টা করুন।"
        )


async def list_products_admin(update, context):
    products = get_products()
    if not products:
        await update.message.reply_text("📦 কোনো Product নেই।")
        return

    lines = ["📋 <b>PRODUCTS</b>\n"]
    for p in products:
        lines.append(
            f"🆔 {p['id']} | 🛍️ {p['name']}\n"
            f"💰 {money(p['price'])} ৳ | 📦 Stock: {p['stock_count']}"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


async def delete_product_start(update, context):
    context.user_data["admin_state"] = "delete_product"
    await update.message.reply_text("🗑️ যে Product delete করবেন তার ID লিখুন:")


async def delete_product(update, context):
    try:
        pid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ সঠিক Product ID লিখুন।")
        return

    con = db()
    con.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
    con.commit()
    con.close()

    context.user_data.clear()
    await update.message.reply_text(
        "✅ Product deleted.",
        reply_markup=admin_keyboard(update.effective_user.id),
    )


async def pending_deposits(update, context):
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
        await update.message.reply_text("✅ কোনো pending deposit নেই।")
        return

    lines = ["💰 <b>PENDING DEPOSITS</b>\n"]
    for r in rows:
        username = f"@{r['username']}" if r["username"] else "N/A"
        lines.append(
            f"ID: <code>{r['id']}</code>\n"
            f"User: <code>{r['user_id']}</code>\n"
            f"Username: <b>{username}</b>\n"
            f"Name: <b>{r['first_name'] or ''}</b>\n"
            f"Amount: <b>{money(r['amount'])} ৳</b>\n"
            f"TRX: <code>{r['trx_id']}</code>\n"
            f"/approve_deposit {r['id']}\n"
            f"/reject_deposit {r['id']}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


async def approve_deposit(update, context):
    if not is_admin(update.effective_user.id):
        return

    try:
        did = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /approve_deposit ID")
        return

    con = db()
    row = con.execute(
        "SELECT * FROM deposits WHERE id=? AND status='pending'", (did,)
    ).fetchone()

    if not row:
        con.close()
        await update.message.reply_text(
            "❌ Deposit not found or already processed."
        )
        return

    con.execute(
        "UPDATE deposits SET status='approved' WHERE id=?", (did,)
    )
    con.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (row["amount"], row["user_id"]),
    )
    con.commit()
    con.close()

    try:
        await context.bot.send_message(
            row["user_id"],
            f"✅ <b>Deposit Approved!</b>\n\n"
            f"💰 Added: <b>{money(row['amount'])} ৳</b>\n"
            f"💵 New Balance: <b>{money(get_balance(row['user_id']))} ৳</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await update.message.reply_text("✅ Deposit approved.")


async def reject_deposit(update, context):
    if not is_admin(update.effective_user.id):
        return

    try:
        did = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /reject_deposit ID")
        return

    con = db()
    cur = con.execute("""
        UPDATE deposits SET status='rejected'
        WHERE id=? AND status='pending'
    """, (did,))
    con.commit()
    con.close()

    await update.message.reply_text(
        "✅ Deposit rejected." if cur.rowcount else "❌ Deposit not found."
    )


# ---------------- ADMIN: USERS ----------------

async def users_admin(update, context):
    if not is_admin(update.effective_user.id):
        return

    con = db()
    rows = con.execute("""
        SELECT user_id, username, first_name, balance, language, created_at
        FROM users
        ORDER BY user_id DESC
        LIMIT 100
    """).fetchall()
    count = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    total_balance = con.execute(
        "SELECT COALESCE(SUM(balance),0) AS s FROM users"
    ).fetchone()["s"]
    con.close()

    lines = [
        "👥 <b>USERS</b>",
        f"Total Users: <b>{count}</b>",
        f"Total User Balance: <b>{money(total_balance)} ৳</b>",
        "",
    ]

    if not rows:
        lines.append("No users found.")
    else:
        for r in rows:
            username = f"@{r['username']}" if r["username"] else "N/A"
            lines.append(
                f"🆔 <code>{r['user_id']}</code>\n"
                f"👤 Username: <b>{username}</b>\n"
                f"📝 Name: <b>{r['first_name'] or 'N/A'}</b>\n"
                f"💰 Balance: <b>{money(r['balance'])} ৳</b>\n"
                f"🌐 Language: <b>{r['language'] or 'not selected'}</b>\n"
                f"📅 Joined: <b>{r['created_at']}</b>"
            )

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


# ---------------- ADMIN: BROADCAST ----------------

async def broadcast_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["admin_state"] = "broadcast"
    await update.message.reply_text(
        "📢 <b>Broadcast</b>\n\n"
        "এখন যে message সব user-কে পাঠাতে চান সেটি লিখুন।\n"
        "URL দিলে Telegram-এর clickable link হিসেবে পাঠানো হবে।\n\n"
        "Cancel করতে 🔙 Return চাপুন।",
        parse_mode="HTML",
        reply_markup=back_keyboard(update.effective_user.id),
    )


async def do_broadcast(update, context):
    if not is_admin(update.effective_user.id):
        return

    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Empty message পাঠানো যাবে না।")
        return

    con = db()
    rows = con.execute("SELECT user_id FROM users").fetchall()
    con.close()

    sent = 0
    failed = 0

    for r in rows:
        try:
            await context.bot.send_message(
                chat_id=r["user_id"],
                text=text,
                disable_web_page_preview=False,
            )
            con2 = db()
            con2.execute("""
                INSERT INTO messages(user_id, text, is_read, created_at)
                VALUES (?, ?, 0, ?)
            """, (r["user_id"], text, now()))
            con2.commit()
            con2.close()
            sent += 1
        except Exception:
            failed += 1

    context.user_data.clear()
    await update.message.reply_text(
        f"✅ <b>Broadcast completed!</b>\n\n"
        f"📤 Sent: <b>{sent}</b>\n"
        f"❌ Failed: <b>{failed}</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(update.effective_user.id),
    )


# ---------------- ADMIN: DEPOSIT LIMITS ----------------

async def deposit_limits_start(update, context):
    if not is_admin(update.effective_user.id):
        return

    context.user_data["admin_state"] = "deposit_min"
    await update.message.reply_text(
        f"💰 <b>Deposit Limits</b>\n\n"
        f"Current Minimum: <b>{setting('deposit_min', '10')} ৳</b>\n"
        f"Current Maximum: <b>{setting('deposit_max', '100000')} ৳</b>\n\n"
        f"নতুন Minimum Deposit amount লিখুন:",
        parse_mode="HTML",
        reply_markup=back_keyboard(update.effective_user.id),
    )


async def deposit_min_set(update, context):
    try:
        value = float(update.message.text.strip())
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ সঠিক minimum amount লিখুন।")
        return

    context.user_data["new_deposit_min"] = value
    context.user_data["admin_state"] = "deposit_max"
    await update.message.reply_text(
        f"✅ Minimum set: {money(value)} ৳\n\n"
        f"এখন নতুন Maximum Deposit amount লিখুন:"
    )


async def deposit_max_set(update, context):
    try:
        value = float(update.message.text.strip())
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ সঠিক maximum amount লিখুন।")
        return

    minimum = float(context.user_data.get("new_deposit_min", 0))
    if value < minimum:
        await update.message.reply_text(
            "❌ Maximum amount Minimum-এর চেয়ে কম হতে পারবে না।"
        )
        return

    set_setting("deposit_min", minimum)
    set_setting("deposit_max", value)
    context.user_data.clear()

    await update.message.reply_text(
        f"✅ <b>Deposit Limits Updated</b>\n\n"
        f"Minimum: <b>{money(minimum)} ৳</b>\n"
        f"Maximum: <b>{money(value)} ৳</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(update.effective_user.id),
    )


# ---------------- ADMIN: SUPPORT ----------------

async def support_settings_start(update, context):
    if not is_admin(update.effective_user.id):
        return

    context.user_data["admin_state"] = "support_username"
    current = setting("support_username", "")
    await update.message.reply_text(
        f"📞 <b>Support Settings</b>\n\n"
        f"Current: <b>{current or 'Not set'}</b>\n\n"
        f"আপনার Telegram username লিখুন।\n"
        f"উদাহরণ: <code>@yourusername</code>\n\n"
        f"অথবা পুরো link দিতে পারেন: <code>https://t.me/yourusername</code>",
        parse_mode="HTML",
        reply_markup=back_keyboard(update.effective_user.id),
    )


async def support_username_set(update, context):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("❌ Username খালি রাখা যাবে না।")
        return

    if value.startswith("https://t.me/"):
        saved = value
    else:
        saved = value.lstrip("@").strip()

    set_setting("support_username", saved)
    context.user_data.clear()

    await update.message.reply_text(
        f"✅ <b>Support saved successfully!</b>\n\n"
        f"Support: <code>{saved}</code>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(update.effective_user.id),
    )


# ---------------- MESSAGE ROUTER ----------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    ensure_user(update.effective_user)
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # Admin document upload for row-based stock.
    if (
        update.message.document
        and is_admin(uid)
        and context.user_data.get("admin_state") == "stock_file"
    ):
        await add_stock_file(update, context)
        return

    # Language selection works even before a language is chosen.
    if text == "🇧🇩 বাংলা":
        await choose_language(update, context, "bn")
        return

    if text == "🇬🇧 English":
        await choose_language(update, context, "en")
        return

    # Return.
    if text in ("🔙 Return", "BACK", "/back"):
        context.user_data.clear()
        await send_home(update, context)
        return

    # Admin states.
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
        await deposit_min_set(update, context)
        return
    if is_admin(uid) and astate == "deposit_max":
        await deposit_max_set(update, context)
        return
    if is_admin(uid) and astate == "support_username":
        await support_username_set(update, context)
        return

    # Deposit states.
    state = context.user_data.get("state")
    if state == "deposit_amount":
        await deposit_amount(update, context, text)
        return
    if state == "deposit_trx":
        await deposit_trx(update, context, text)
        return

    # Buy quantity.
    if context.user_data.get("buy_product_id"):
        try:
            qty = int(text)
            if await process_quantity(update, context, qty):
                return
        except ValueError:
            pass

    # Main menu, language-aware.
    lang = lang_of(uid)
    t = T[lang]

    if text == t["buy"]:
        await show_buy_menu(update, context)
        return
    if text == t["deposit"]:
        await deposit_start(update, context)
        return
    if text == t["balance"]:
        await show_balance(update, context)
        return
    if text == t["prices"]:
        await show_price_list(update, context)
        return
    if text == t["language"]:
        await show_language(update, context)
        return
    if text == t["mail"]:
        await show_mail(update, context)
        return
    if text == t["support"]:
        await show_support(update, context)
        return
    if text == t["admin"] and is_admin(uid):
        await admin_panel(update, context)
        return

    # Admin menu.
    if is_admin(uid):
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
        if text == "👥 Users":
            await users_admin(update, context)
            return
        if text == "📢 Broadcast":
            await broadcast_start(update, context)
            return
        if text == "💰 Deposit Limits":
            await deposit_limits_start(update, context)
            return
        if text == "📞 Support Settings":
            await support_settings_start(update, context)
            return

    # Dynamic product button.
    if text.startswith("🛒 "):
        product = find_product_by_button(text)
        if product:
            await show_product(update, context, product)
            return

    await update.message.reply_text(
        tr(uid, "menu_error"),
        reply_markup=main_keyboard(uid),
    )


# ---------------- COMMANDS ----------------

async def help_command(update, context):
    uid = update.effective_user.id
    await update.message.reply_text(
        "/start - Main Menu\n"
        "/help - Help\n\n"
        "Admin commands:\n"
        "/approve_deposit ID\n"
        "/reject_deposit ID",
        reply_markup=main_keyboard(uid),
    )


def main():
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "bot.py ফাইলে BOT_TOKEN-এর জায়গায় আপনার Bot Token বসান।"
        )

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("approve_deposit", approve_deposit))
    app.add_handler(CommandHandler("reject_deposit", reject_deposit))

    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_message)
    )

    logger.info("Updated Product Selling Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
