"""
Voucher Store Telegram Bot
Python 3.10+
Features: Buy Vouchers, Wallet, Refer & Earn, My Orders, Support.
Payments are manual UPI verification. Never auto-approve from a screenshot/UTR alone.

Environment variables:
BOT_TOKEN, ADMIN_ID, UPI_ID, UPI_NAME
Optional:
SUPPORT_USERNAME (without @), REFERRAL_REWARD (default 5),
DATA_DIR (default current directory; on Render persistent disk use /var/data)
"""

import logging
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("8824898321:AAHoMV1mh7q7bfdkXELcmn4eGfpypjj_5l0", "").strip()
ADMIN_ID_TEXT = os.getenv("8904470199", "").strip()
UPI_ID = os.getenv("UPI_ID", "").strip()
UPI_NAME = os.getenv("UPI_NAME", "Voucher Store").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
REFERRAL_REWARD = float(os.getenv("REFERRAL_REWARD", "5"))
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR = DATA_DIR / "digital_files"
FILES_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "shop.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment.")
if not ADMIN_ID_TEXT.isdigit():
    raise RuntimeError("ADMIN_ID must be your numeric Telegram user ID.")
ADMIN_ID = int(ADMIN_ID_TEXT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("voucher-store")

MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🛒 Buy Vouchers")],
        [KeyboardButton("👛 Wallet"), KeyboardButton("🎁 Refer & Earn")],
        [KeyboardButton("🔥 My Orders"), KeyboardButton("❤️ Support")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL NOT NULL DEFAULT 0,
            referred_by INTEGER,
            referral_paid INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            code TEXT NOT NULL,
            reserved_order_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS digital_products (
            product_id INTEGER PRIMARY KEY REFERENCES products(id),
            file_path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            amount REAL NOT NULL,
            payment_method TEXT,
            status TEXT NOT NULL DEFAULT 'created',
            utr TEXT,
            coupon_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS wallet_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            utr TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # Safe schema additions for databases created by an earlier version.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(orders)")}
        if "payment_method" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT")
        if "utr" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN utr TEXT")
        if "coupon_id" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN coupon_id INTEGER")


def ensure_user(user):
    with db() as con:
        con.execute(
            "INSERT INTO users(user_id, username) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user.id, user.username or ""),
        )


def admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def fmt(amount):
    return f"₹{amount:.2f}".rstrip("0").rstrip(".")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    # Optional deep-link referral: /start ref123456
    if context.args and context.args[0].startswith("ref"):
        try:
            referrer_id = int(context.args[0][3:])
            if referrer_id != user.id:
                with db() as con:
                    row = con.execute(
                        "SELECT referred_by FROM users WHERE user_id=?", (user.id,)
                    ).fetchone()
                    referrer = con.execute(
                        "SELECT user_id FROM users WHERE user_id=?", (referrer_id,)
                    ).fetchone()
                    if row and row["referred_by"] is None and referrer:
                        con.execute(
                            "UPDATE users SET referred_by=? WHERE user_id=?",
                            (referrer_id, user.id),
                        )
        except ValueError:
            pass
    await update.message.reply_text(
        f"👋 Welcome, {user.first_name}!\n\n"
        "Choose an option from the menu below.",
        reply_markup=MENU,
    )


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        products = con.execute(
            "SELECT * FROM products WHERE active=1 ORDER BY id DESC"
        ).fetchall()
        stock_rows = con.execute(
            "SELECT product_id, COUNT(*) AS n FROM coupons WHERE reserved_order_id IS NULL "
            "GROUP BY product_id"
        ).fetchall()
        stock = {r["product_id"]: r["n"] for r in stock_rows}
        digital = {
            r["product_id"] for r in con.execute("SELECT product_id FROM digital_products")
        }
    if not products:
        await update.message.reply_text("No vouchers are available right now.", reply_markup=MENU)
        return
    for p in products:
        available = stock.get(p["id"], 0) > 0 or p["id"] in digital
        caption = (
            f"🎟️ {p['name']}\n"
            f"💰 Price: {fmt(p['price'])}\n"
            f"ℹ️ {p['description'] or 'Digital voucher'}\n"
            f"📦 {'Available' if available else 'Out of stock'}"
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Buy now", callback_data=f"buy:{p['id']}")
        ]]) if available else None
        await update.message.reply_text(caption, reply_markup=markup)


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        pid = int(q.data.split(":")[1])
    except Exception:
        await q.edit_message_text("Invalid product.")
        return
    with db() as con:
        p = con.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (pid,)
        ).fetchone()
        if not p:
            await q.edit_message_text("Product is unavailable.")
            return
        coupon = con.execute(
            "SELECT id FROM coupons WHERE product_id=? AND reserved_order_id IS NULL ORDER BY id LIMIT 1",
            (pid,),
        ).fetchone()
        digital = con.execute(
            "SELECT file_path FROM digital_products WHERE product_id=?", (pid,)
        ).fetchone()
        if not coupon and not (digital and Path(digital["file_path"]).is_file()):
            await q.edit_message_text("Sorry, this product is out of stock.")
            return
        cur = con.execute(
            "INSERT INTO orders(user_id, product_id, amount, status) VALUES(?,?,?,'awaiting_payment')",
            (q.from_user.id, pid, p["price"]),
        )
        oid = cur.lastrowid
    buttons = [
        [InlineKeyboardButton("Pay using Wallet", callback_data=f"walletpay:{oid}")],
        [InlineKeyboardButton("Pay using UPI", callback_data=f"upipay:{oid}")],
    ]
    await q.edit_message_text(
        f"🧾 Order #{oid}\n{p['name']}\nAmount: {fmt(p['price'])}\n\nChoose payment method.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, oid_text = q.data.split(":")
    oid = int(oid_text)
    with db() as con:
        order = con.execute(
            "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id "
            "WHERE o.id=? AND o.user_id=?", (oid, q.from_user.id)
        ).fetchone()
        if not order or order["status"] != "awaiting_payment":
            await q.edit_message_text("This order is no longer awaiting payment.")
            return
        if action == "walletpay":
            user = con.execute("SELECT balance FROM users WHERE user_id=?", (q.from_user.id,)).fetchone()
            if not user or user["balance"] < order["amount"]:
                await q.edit_message_text(
                    f"Insufficient wallet balance.\nRequired: {fmt(order['amount'])}\n"
                    f"Use Wallet to request a top-up, or choose UPI."
                )
                return
            con.execute("UPDATE users SET balance=balance-? WHERE user_id=?",
                        (order["amount"], q.from_user.id))
            con.execute("UPDATE orders SET status='paid', payment_method='wallet' WHERE id=?", (oid,))
            paid_by_wallet = True
        else:
            con.execute("UPDATE orders SET payment_method='upi' WHERE id=?", (oid,))
            paid_by_wallet = False

    if paid_by_wallet:
        await q.edit_message_text(f"Wallet payment received for order #{oid}. Delivery will follow.")
        await deliver_order(context, oid)
    else:
        if not UPI_ID:
            await q.edit_message_text("UPI is not configured. Please contact support.")
            return
        await q.edit_message_text(
            f"💳 UPI payment for order #{oid}\n"
            f"UPI ID: {UPI_ID}\nPayee: {UPI_NAME}\nAmount: {fmt(order['amount'])}\n\n"
            f"After payment, send:\n/paid {oid} YOUR_UTR\n\n"
            "Admin verifies payment before delivery."
        )
        await context.bot.send_message(
            ADMIN_ID, f"UPI order #{oid} created by user {q.from_user.id} for {fmt(order['amount'])}."
        )


async def paid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /paid ORDER_ID UTR")
        return
    try:
        oid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Order ID must be a number.")
        return
    utr = " ".join(context.args[1:]).strip()
    with db() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id=? AND user_id=?", (oid, update.effective_user.id)
        ).fetchone()
        if not order or order["status"] != "awaiting_payment" or order["payment_method"] != "upi":
            await update.message.reply_text("Order not found or not awaiting UPI payment.")
            return
        con.execute("UPDATE orders SET utr=?, status='payment_submitted' WHERE id=?", (utr, oid))
    await update.message.reply_text("✅ UTR submitted. Delivery happens after admin verifies the actual payment.")
    await context.bot.send_message(
        ADMIN_ID, f"Payment submitted for order #{oid}\nUser: {update.effective_user.id}\nUTR: {utr}\n"
                  f"Verify in your bank/UPI app, then use /approve {oid}."
    )


async def deliver_order(context: ContextTypes.DEFAULT_TYPE, oid: int):
    """Deliver an already-paid order. Coupon is removed only after send succeeds."""
    with db() as con:
        order = con.execute(
            "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id WHERE o.id=?",
            (oid,),
        ).fetchone()
        if not order or order["status"] not in ("paid", "payment_submitted"):
            return False
        coupon = con.execute(
            "SELECT id, code FROM coupons WHERE product_id=? AND reserved_order_id IS NULL ORDER BY id LIMIT 1",
            (order["product_id"],),
        ).fetchone()
        digital = con.execute(
            "SELECT file_path FROM digital_products WHERE product_id=?", (order["product_id"],)
        ).fetchone()
        if not coupon and not (digital and Path(digital["file_path"]).is_file()):
            await context.bot.send_message(ADMIN_ID, f"Order #{oid} paid but stock/file is missing.")
            return False
        # Reserve coupon before sending to avoid two simultaneous deliveries.
        if coupon:
            con.execute("UPDATE coupons SET reserved_order_id=? WHERE id=? AND reserved_order_id IS NULL",
                        (oid, coupon["id"]))
    try:
        if coupon:
            await context.bot.send_message(
                order["user_id"],
                f"🎉 Payment confirmed!\nOrder #{oid}\n{order['name']}\n"
                f"Your voucher code:\n`{coupon['code']}`",
                parse_mode="Markdown",
            )
        else:
            with open(digital["file_path"], "rb") as f:
                await context.bot.send_document(
                    order["user_id"], document=f, caption=f"Order #{oid}: {order['name']}"
                )
        referrer_id = None
        with db() as con:
            if coupon:
                con.execute("DELETE FROM coupons WHERE id=? AND reserved_order_id=?", (coupon["id"], oid))
            con.execute("UPDATE orders SET status='delivered', coupon_id=? WHERE id=?",
                        (coupon["id"] if coupon else None, oid))
            # Pay a referral reward once, only after the referred user's first successful delivery.
            buyer = con.execute(
                "SELECT referred_by, referral_paid FROM users WHERE user_id=?",
                (order["user_id"],),
            ).fetchone()
            if buyer and buyer["referred_by"] and not buyer["referral_paid"]:
                referrer_id = buyer["referred_by"]
                con.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=?",
                    (REFERRAL_REWARD, referrer_id),
                )
                con.execute(
                    "UPDATE users SET referral_paid=1 WHERE user_id=?",
                    (order["user_id"],),
                )
        if referrer_id:
            await context.bot.send_message(
                referrer_id,
                f"🎁 Referral reward credited: {fmt(REFERRAL_REWARD)} to your wallet!",
            )
        await context.bot.send_message(ADMIN_ID, f"✅ Order #{oid} delivered.")
        return True
    except Exception:
        log.exception("Delivery failed for order %s", oid)
        # Keep reservation so an admin can inspect before retrying; do not risk double-selling.
        await context.bot.send_message(
            ADMIN_ID, f"⚠️ Delivery for order #{oid} failed. Check customer chat and stock before retrying."
        )
        return False


async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        row = con.execute("SELECT balance FROM users WHERE user_id=?",
                          (update.effective_user.id,)).fetchone()
    balance = row["balance"] if row else 0
    await update.message.reply_text(
        f"👛 Your wallet balance: {fmt(balance)}\n\n"
        "To request a wallet top-up, send:\n/topup AMOUNT UTR\n"
        "Pay to the store UPI ID shown by Support/admin. Top-ups are credited only after verification.",
        reply_markup=MENU,
    )


async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /topup AMOUNT UTR")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid amount.")
        return
    utr = " ".join(context.args[1:]).strip()
    with db() as con:
        cur = con.execute(
            "INSERT INTO wallet_requests(user_id, amount, utr) VALUES(?,?,?)",
            (update.effective_user.id, amount, utr),
        )
        rid = cur.lastrowid
    await update.message.reply_text(f"Top-up request #{rid} submitted for admin verification.")
    await context.bot.send_message(
        ADMIN_ID, f"Wallet top-up #{rid}\nUser: {update.effective_user.id}\n"
                  f"Amount: {fmt(amount)}\nUTR: {utr}\nApprove with /credit {rid} after verifying payment."
    )


async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref{update.effective_user.id}"
    with db() as con:
        count = con.execute("SELECT COUNT(*) FROM users WHERE referred_by=? AND referral_paid=1",
                            (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(
        f"🎁 Refer & Earn\nShare your personal link:\n{link}\n\n"
        f"Successful referrals: {count}\nReward: {fmt(REFERRAL_REWARD)} per eligible referral.\n"
        "Reward is credited once the referred customer completes their first delivered order."
    )


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        rows = con.execute(
            "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id "
            "WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10",
            (update.effective_user.id,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("You have no orders yet.", reply_markup=MENU)
        return
    text = "🔥 Your recent orders\n\n"
    for r in rows:
        text += f"#{r['id']} — {r['name']} — {fmt(r['amount'])} — {r['status']}\n"
    await update.message.reply_text(text, reply_markup=MENU)


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if SUPPORT_USERNAME:
        msg = f"❤️ Support\nContact: @{SUPPORT_USERNAME}"
    else:
        msg = "❤️ Support\nSend your question here and the store admin will receive it."
    await update.message.reply_text(msg, reply_markup=MENU)


async def support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        return
    await context.bot.send_message(
        ADMIN_ID,
        f"❤️ Support message from {update.effective_user.full_name} "
        f"(ID {update.effective_user.id}):\n{update.message.text}"
    )
    await update.message.reply_text("Your message has been sent to support.")


async def addproduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    parts = update.message.text.partition(" ")[2].split("|", 2)
    if len(parts) != 3:
        await update.message.reply_text("Usage: /addproduct NAME|PRICE|DESCRIPTION")
        return
    name, price_text, description = [x.strip() for x in parts]
    try:
        price = float(price_text)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Price must be greater than zero.")
        return
    with db() as con:
        cur = con.execute("INSERT INTO products(name,price,description) VALUES(?,?,?)",
                          (name, price, description))
    await update.message.reply_text(f"Product added. ID: {cur.lastrowid}")


async def addcoupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    parts = update.message.text.partition(" ")[2].split("|", 1)
    if len(parts) != 2:
        await update.message.reply_text("Usage: /addcoupon PRODUCT_ID|CODE")
        return
    try:
        pid = int(parts[0].strip())
    except ValueError:
        await update.message.reply_text("Product ID must be a number.")
        return
    with db() as con:
        p = con.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            await update.message.reply_text("Product not found.")
            return
        con.execute("INSERT INTO coupons(product_id,code) VALUES(?,?)", (pid, parts[1].strip()))
    await update.message.reply_text("Coupon code added to stock.")


async def addfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    try:
        pid = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /addfile PRODUCT_ID, then send the file.")
        return
    with db() as con:
        p = con.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        await update.message.reply_text("Product not found.")
        return
    context.user_data["file_product_id"] = pid
    await update.message.reply_text(f"Now send the digital file for product #{pid}.")


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update) or "file_product_id" not in context.user_data:
        return
    doc = update.message.document
    if not doc:
        return
    pid = context.user_data["file_product_id"]
    safe_name = Path(doc.file_name or f"product_{pid}.bin").name
    dest = FILES_DIR / f"{pid}_{safe_name}"
    tgfile = await context.bot.get_file(doc.file_id)
    await tgfile.download_to_drive(custom_path=dest)
    with db() as con:
        con.execute(
            "INSERT INTO digital_products(product_id,file_path) VALUES(?,?) "
            "ON CONFLICT(product_id) DO UPDATE SET file_path=excluded.file_path",
            (pid, str(dest)),
        )
    context.user_data.pop("file_product_id", None)
    await update.message.reply_text("Digital file uploaded and linked to product.")


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    with db() as con:
        orders = con.execute(
            "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id "
            "WHERE o.status IN ('awaiting_payment','payment_submitted') ORDER BY o.id DESC"
        ).fetchall()
        requests = con.execute(
            "SELECT * FROM wallet_requests WHERE status='pending' ORDER BY id DESC"
        ).fetchall()
    if not orders and not requests:
        await update.message.reply_text("No pending orders or wallet top-ups.")
        return
    for o in orders:
        await update.message.reply_text(
            f"Order #{o['id']} | {o['name']} | {fmt(o['amount'])}\n"
            f"User: {o['user_id']} | Status: {o['status']} | UTR: {o['utr'] or '—'}"
        )
    for r in requests:
        await update.message.reply_text(
            f"Wallet top-up #{r['id']} | User: {r['user_id']} | {fmt(r['amount'])} | UTR: {r['utr']}"
        )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update) or not context.args:
        return
    try:
        oid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /approve ORDER_ID")
        return
    with db() as con:
        o = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] != "payment_submitted":
            await update.message.reply_text("Order not found or no UPI payment submitted.")
            return
        con.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
    await update.message.reply_text("Payment marked verified. Attempting delivery.")
    await deliver_order(context, oid)


async def credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update) or not context.args:
        return
    try:
        rid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /credit TOPUP_REQUEST_ID")
        return
    with db() as con:
        r = con.execute("SELECT * FROM wallet_requests WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] != "pending":
            await update.message.reply_text("Top-up request not found or already processed.")
            return
        con.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (r["amount"], r["user_id"]))
        con.execute("UPDATE wallet_requests SET status='credited' WHERE id=?", (rid,))
    await update.message.reply_text(f"Wallet top-up #{rid} credited. Verify bank payment first.")
    await context.bot.send_message(r["user_id"], f"✅ Wallet credited with {fmt(r['amount'])}.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        delivered = con.execute("SELECT COUNT(*) FROM orders WHERE status='delivered'").fetchone()[0]
        revenue = con.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='delivered'").fetchone()[0]
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    await update.message.reply_text(
        f"📊 Store stats\nUsers: {users}\nOrders: {total}\n"
        f"Delivered: {delivered}\nDelivered sales: {fmt(revenue)}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ensure_user(update.effective_user)
    if text == "🛒 Buy Vouchers":
        await show_products(update, context)
    elif text == "👛 Wallet":
        await wallet(update, context)
    elif text == "🎁 Refer & Earn":
        await referral(update, context)
    elif text == "🔥 My Orders":
        await my_orders(update, context)
    elif text == "❤️ Support":
        await support(update, context)
    else:
        await support_message(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("paid", paid_command))
    app.add_handler(CommandHandler("topup", topup_command))
    app.add_handler(CommandHandler("addproduct", addproduct))
    app.add_handler(CommandHandler("addcoupon", addcoupon))
    app.add_handler(CommandHandler("addfile", addfile))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("credit", credit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(payment_callback, pattern=r"^(walletpay|upipay):\d+$"))
    app.add_handler(MessageHandler(filters.Document.ALL, receive_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    log.info("Voucher store bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
