"""
Voucher Store Telegram Bot
Python 3.10+
Features: Buy vouchers, quantity selection, UPI QR, manual payment verification,
Approve/Reject buttons for admins, My Orders, Support, Admin Panel, broadcast,
personal coupons, product/coupon/file management.

Environment:
BOT_TOKEN, ADMIN_ID, UPI_ID, UPI_NAME
Optional: DATA_DIR (default "."; use "/var/data" for a Render persistent disk)
"""

import asyncio
import io
import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import qrcode
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_TEXT = os.getenv("ADMIN_ID", "").strip()
UPI_ID = os.getenv("UPI_ID", "").strip()
UPI_NAME = os.getenv("UPI_NAME", "Voucher Store").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "Adityajat55").strip().lstrip("@")
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR = DATA_DIR / "digital_files"
FILES_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "shop.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in your environment.")
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
        [KeyboardButton("🔥 My Orders"), KeyboardButton("❤️ Support")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
ADMIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🛠 Admin Panel")],
        [KeyboardButton("🛒 Buy Vouchers")],
        [KeyboardButton("🔥 My Orders"), KeyboardButton("❤️ Support")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
ADMIN_PANEL = InlineKeyboardMarkup([
    [InlineKeyboardButton("📢 Broadcast Message", callback_data="admin:broadcast")],
    [
        InlineKeyboardButton("➕ Add Product", callback_data="admin:addproduct"),
        InlineKeyboardButton("🗑 Delete Product", callback_data="admin:deleteproduct"),
    ],
    [
        InlineKeyboardButton("🎟 Add Coupon", callback_data="admin:addcoupon"),
        InlineKeyboardButton("🎁 Send Personal Coupon", callback_data="admin:personal_coupon"),
    ],
    [
        InlineKeyboardButton("🧾 Pending Payments", callback_data="admin:pending"),
        InlineKeyboardButton("📊 Statistics", callback_data="admin:stats"),
    ],
])


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
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            quantity INTEGER NOT NULL DEFAULT 1,
            payment_ref TEXT,
            expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS delivery_archive (
            order_id INTEGER PRIMARY KEY REFERENCES orders(id),
            delivery_type TEXT NOT NULL,
            payload TEXT,
            file_path TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(orders)")}
        additions = {
            "payment_method": "TEXT",
            "utr": "TEXT",
            "coupon_id": "INTEGER",
            "quantity": "INTEGER NOT NULL DEFAULT 1",
            "payment_ref": "TEXT",
            "expires_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in cols:
                con.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_ref "
            "ON orders(payment_ref) WHERE payment_ref IS NOT NULL"
        )


def ensure_user(user):
    if not user:
        return
    with db() as con:
        con.execute(
            "INSERT INTO users(user_id, username) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user.id, user.username or ""),
        )


def admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def fmt(amount):
    return f"₹{float(amount):.2f}".rstrip("0").rstrip(".")


def payment_review_keyboard(oid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ APPROVE",
            callback_data=f"payreview:approve:{oid}",
        ),
        InlineKeyboardButton(
            "❌ REJECT",
            callback_data=f"payreview:reject:{oid}",
        ),
    ]])


async def notify_admin_payment(context, order, user_id, oid):
    await context.bot.send_message(
        ADMIN_ID,
        "💳 NEW PAYMENT SUBMITTED\n\n"
        f"🧾 Order: {order['payment_ref'] or f'ORDER-{oid}'} (#{oid})\n"
        f"👤 User ID: {user_id}\n"
        f"🔢 UTR: {order['utr'] or '—'}\n"
        f"💰 Amount: {fmt(order['amount'])}\n"
        f"📦 Quantity: {order['quantity']}\n\n"
        "⚠️ Check the actual credit in your UPI/bank account before deciding.",
        reply_markup=payment_review_keyboard(oid),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
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
        f"👋 Welcome, {user.first_name}!\n\nChoose an option below.",
        reply_markup=ADMIN_MENU if admin(update) else MENU,
    )


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    with db() as con:
        products = con.execute(
            "SELECT * FROM products WHERE active=1 ORDER BY id DESC"
        ).fetchall()
        stock_rows = con.execute(
            "SELECT product_id, COUNT(*) AS n FROM coupons "
            "WHERE reserved_order_id IS NULL GROUP BY product_id"
        ).fetchall()
        stock = {r["product_id"]: r["n"] for r in stock_rows}
        digital = {
            r["product_id"]
            for r in con.execute("SELECT product_id FROM digital_products")
        }

    buttons = []
    for p in products:
        if stock.get(p["id"], 0) > 0 or p["id"] in digital:
            buttons.append([InlineKeyboardButton(
                f"🛒 {p['name']} · {fmt(p['price'])}",
                callback_data=f"buy:{p['id']}",
            )])
    if not buttons:
        await update.message.reply_text(
            "No vouchers are available right now.", reply_markup=MENU
        )
        return
    await update.message.reply_text(
        "👇 Select a service to continue:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":")[1])
    with db() as con:
        p = con.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (pid,)
        ).fetchone()
        stock = con.execute(
            "SELECT COUNT(*) FROM coupons WHERE product_id=? "
            "AND reserved_order_id IS NULL", (pid,)
        ).fetchone()[0]
        digital = con.execute(
            "SELECT file_path FROM digital_products WHERE product_id=?", (pid,)
        ).fetchone()
    if not p:
        await q.edit_message_text("Product is unavailable.")
        return
    digital_available = bool(
        digital and Path(digital["file_path"]).is_file() and stock == 0
    )
    if stock < 1 and not digital_available:
        await q.edit_message_text("Sorry, this product is out of stock.")
        return
    context.user_data["awaiting_quantity"] = {
        "product_id": pid,
        "product_name": p["name"],
        "price": float(p["price"]),
        "max_qty": 1 if digital_available else min(stock, 50),
        "digital_only": digital_available,
    }
    max_qty = 1 if digital_available else min(stock, 50)
    await q.edit_message_text(
        f"🛒 {p['name']}\nPrice per unit: {fmt(p['price'])}\n"
        f"Available: {'1 digital file' if digital_available else stock}\n\n"
        f"How many would you like? Enter a number from 1 to {max_qty}."
    )


async def quantity_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("awaiting_quantity")
    if not pending:
        return False
    raw = (update.message.text or "").strip()
    if not raw.isdigit():
        await update.message.reply_text("Please type a whole number, for example: 2")
        return True
    quantity = int(raw)
    if not 1 <= quantity <= pending["max_qty"]:
        await update.message.reply_text(
            f"Quantity must be between 1 and {pending['max_qty']}."
        )
        return True

    pid = pending["product_id"]
    with db() as con:
        p = con.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (pid,)
        ).fetchone()
        if not p:
            context.user_data.pop("awaiting_quantity", None)
            await update.message.reply_text("This product is no longer available.")
            return True
        stock = con.execute(
            "SELECT COUNT(*) FROM coupons WHERE product_id=? "
            "AND reserved_order_id IS NULL", (pid,)
        ).fetchone()[0]
        digital = con.execute(
            "SELECT file_path FROM digital_products WHERE product_id=?", (pid,)
        ).fetchone()
        digital_available = bool(
            digital and Path(digital["file_path"]).is_file() and stock == 0
        )
        if digital_available and quantity != 1:
            await update.message.reply_text("This digital file can only be ordered once.")
            return True
        if not digital_available and stock < quantity:
            await update.message.reply_text(
                f"Only {stock} coupon(s) are available. Enter a smaller quantity."
            )
            return True

        total = round(float(p["price"]) * quantity, 2)
        payment_ref = (
            f"PH-{datetime.now(timezone.utc).strftime('%Y%m%d')}-"
            f"{secrets.token_hex(3).upper()}"
        )
        cur = con.execute(
            "INSERT INTO orders(user_id, product_id, amount, quantity, status, payment_ref) "
            "VALUES(?,?,?,?, 'awaiting_payment', ?)",
            (update.effective_user.id, pid, total, quantity, payment_ref),
        )
        oid = cur.lastrowid

    context.user_data.pop("awaiting_quantity", None)
    await update.message.reply_text(
        f"✅ Order created successfully!\n"
        f"🧾 Your unique Order ID: {payment_ref}\n"
        f"Keep this ID. After delivery, use /recover {payment_ref} "
        "if you need your voucher again."
    )
    await send_payment_qr(
        context, update.effective_chat.id, update.effective_user.id, oid
    )
    return True


async def send_payment_qr(context, chat_id, user_id, oid):
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    with db() as con:
        order = con.execute(
            "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id "
            "WHERE o.id=? AND o.user_id=?", (oid, user_id)
        ).fetchone()
        if not order or order["status"] != "awaiting_payment":
            await context.bot.send_message(chat_id, "This order is no longer awaiting payment.")
            return
        con.execute(
            "UPDATE orders SET payment_method='upi', expires_at=? WHERE id=?",
            (expires_at.isoformat(), oid),
        )

    if not UPI_ID:
        await context.bot.send_message(
            chat_id, "⚠️ UPI is not configured. Please contact support."
        )
        return

    params = {
        "pa": UPI_ID,
        "pn": UPI_NAME,
        "am": f"{float(order['amount']):.2f}",
        "cu": "INR",
        "tn": order["payment_ref"] or f"Order {oid}",
    }
    upi_uri = "upi://pay?" + urlencode(params)
    qr = qrcode.QRCode(box_size=8, border=3)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    photo = io.BytesIO()
    photo.name = f"payment_{oid}.png"
    qr.make_image(fill_color="black", back_color="white").save(photo, format="PNG")
    photo.seek(0)

    caption = (
        "💳 Paytm Business Payment\n\n"
        f"🧾 Order ID: {order['payment_ref'] or f'ORDER-{oid}'}\n"
        f"⭐ Service: {order['name']}\n"
        "⚡ Instant Use\n"
        f"📦 Qty: {order['quantity']}\n"
        f"💰 Amount: {fmt(order['amount'])}\n"
        "📲 Scan the QR with any UPI app\n"
        "⏰ QR valid for 10 minutes\n\n"
        "After payment, tap Verify Payment and send your UTR. "
        "Voucher delivery happens only after admin verification.\n"
        f"Keep Order ID {order['payment_ref']}. Recover later with /recover {order['payment_ref']}."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ Verify Payment", callback_data=f"verify:{oid}")
    ]])
    await context.bot.send_photo(
        chat_id=chat_id, photo=photo, caption=caption, reply_markup=keyboard
    )
    await context.bot.send_message(
        ADMIN_ID,
        f"New payment QR created\nOrder: {order['payment_ref']}\n"
        f"User: {user_id}\nAmount: {fmt(order['amount'])}\nQR valid for 10 minutes."
    )


async def verify_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    oid = int(q.data.split(":")[1])
    with db() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id=? AND user_id=?",
            (oid, q.from_user.id),
        ).fetchone()
    if not order or order["status"] != "awaiting_payment":
        await q.message.reply_text("This order is not awaiting payment.")
        return
    if (
        not order["expires_at"]
        or datetime.now(timezone.utc) > datetime.fromisoformat(order["expires_at"])
    ):
        with db() as con:
            con.execute(
                "UPDATE orders SET status='expired' "
                "WHERE id=? AND status='awaiting_payment'", (oid,)
            )
        await q.message.reply_text("⏰ QR expired. Please start a new order.")
        return
    context.user_data["awaiting_utr"] = oid
    await q.message.reply_text(
        f"🧾 Order {order['payment_ref']}\n"
        "Please send your UPI transaction UTR/reference number."
    )


async def submit_utr(update: Update, context: ContextTypes.DEFAULT_TYPE, oid, utr):
    with db() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id=? AND user_id=?",
            (oid, update.effective_user.id),
        ).fetchone()
        if (
            not order
            or order["status"] != "awaiting_payment"
            or order["payment_method"] != "upi"
        ):
            await update.message.reply_text("This order is no longer awaiting payment.")
            return
        if (
            not order["expires_at"]
            or datetime.now(timezone.utc) > datetime.fromisoformat(order["expires_at"])
        ):
            con.execute(
                "UPDATE orders SET status='expired' WHERE id=? AND status='awaiting_payment'",
                (oid,),
            )
            await update.message.reply_text("⏰ QR expired. Please create a new order.")
            return
        con.execute(
            "UPDATE orders SET utr=?, status='payment_submitted' "
            "WHERE id=? AND status='awaiting_payment'",
            (utr, oid),
        )
        updated_order = con.execute(
            "SELECT * FROM orders WHERE id=?", (oid,)
        ).fetchone()

    await update.message.reply_text(
        "✅ UTR submitted. Your payment is pending admin verification."
    )
    await notify_admin_payment(
        context, updated_order, update.effective_user.id, oid
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
    if len(utr) < 6:
        await update.message.reply_text("Please provide a valid UTR (at least 6 characters).")
        return
    await submit_utr(update, context, oid, utr)


async def deliver_order(context: ContextTypes.DEFAULT_TYPE, oid: int):
    """Deliver a paid order and archive its contents for customer recovery."""
    with db() as con:
        order = con.execute(
            "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id "
            "WHERE o.id=?", (oid,)
        ).fetchone()
        if not order or order["status"] != "paid":
            return False

        # If delivery was already archived (e.g. after a previous interrupted attempt),
        # reuse the archive rather than allocating a second set of coupons.
        archived = con.execute(
            "SELECT * FROM delivery_archive WHERE order_id=?", (oid,)
        ).fetchone()

        if archived:
            delivery_type = archived["delivery_type"]
            archived_payload = archived["payload"]
            archived_file = archived["file_path"]
            coupons = []
            digital_path = archived_file
        else:
            quantity = int(order["quantity"] or 1)
            coupons = con.execute(
                "SELECT id, code FROM coupons WHERE product_id=? "
                "AND reserved_order_id IS NULL ORDER BY id LIMIT ?",
                (order["product_id"], quantity),
            ).fetchall()
            digital = con.execute(
                "SELECT file_path FROM digital_products WHERE product_id=?",
                (order["product_id"],),
            ).fetchone()
            digital_ok = bool(
                not coupons and quantity == 1 and digital
                and Path(digital["file_path"]).is_file()
            )
            if len(coupons) < quantity and not digital_ok:
                await context.bot.send_message(
                    ADMIN_ID, f"⚠️ Order #{oid} paid but stock is insufficient."
                )
                return False

            if coupons:
                codes = [item["code"] for item in coupons]
                archived_payload = "\n".join(
                    f"{i}. {code}" for i, code in enumerate(codes, 1)
                )
                delivery_type = "codes"
                digital_path = None
                for item in coupons:
                    con.execute(
                        "UPDATE coupons SET reserved_order_id=? "
                        "WHERE id=? AND reserved_order_id IS NULL",
                        (oid, item["id"]),
                    )
            else:
                delivery_type = "file"
                archived_payload = None
                digital_path = digital["file_path"]

            con.execute(
                "INSERT INTO delivery_archive(order_id, delivery_type, payload, file_path) "
                "VALUES(?,?,?,?)",
                (oid, delivery_type, archived_payload, digital_path),
            )

    try:
        if delivery_type == "codes":
            await context.bot.send_message(
                order["user_id"],
                f"🎉 Payment confirmed!\n"
                f"🧾 Order ID: {order['payment_ref'] or f'ORDER-{oid}'}\n"
                f"{order['name']}\nQuantity: {order['quantity']}\n"
                f"Your voucher codes:\n{archived_payload}\n\n"
                f"To recover these codes later, send /recover {order['payment_ref'] or oid}"
            )
        else:
            if not digital_path or not Path(digital_path).is_file():
                raise FileNotFoundError("Archived digital product file is missing.")
            with open(digital_path, "rb") as f:
                await context.bot.send_document(
                    order["user_id"], document=f,
                    caption=(
                        f"🎉 Payment confirmed! Order ID: "
                        f"{order['payment_ref'] or f'ORDER-{oid}'}\n"
                        f"To recover this file later, send /recover {order['payment_ref'] or oid}"
                    ),
                )

        with db() as con:
            if delivery_type == "codes":
                con.execute("DELETE FROM coupons WHERE reserved_order_id=?", (oid,))
            con.execute(
                "UPDATE orders SET status='delivered' WHERE id=? AND status='paid'",
                (oid,),
            )
        await context.bot.send_message(ADMIN_ID, f"✅ Order #{oid} delivered and archived for recovery.")
        return True
    except Exception:
        log.exception("Delivery failed for order %s", oid)
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ Delivery for order #{oid} failed. The archived delivery is retained; "
            "check the customer chat before retrying."
        )
        return False


async def payment_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not admin(update):
        await q.answer("Access denied!", show_alert=True)
        return
    await q.answer()
    try:
        _, action, oid_text = q.data.split(":")
        oid = int(oid_text)
    except (ValueError, IndexError):
        await q.message.reply_text("Invalid payment request.")
        return

    if action == "approve":
        with db() as con:
            order = con.execute(
                "SELECT * FROM orders WHERE id=?", (oid,)
            ).fetchone()
            if not order or order["status"] != "payment_submitted":
                await q.message.reply_text("This payment has already been processed or is invalid.")
                return
            con.execute(
                "UPDATE orders SET status='paid' WHERE id=? AND status='payment_submitted'",
                (oid,),
            )
        await q.edit_message_text(
            f"✅ PAYMENT APPROVED\nOrder #{oid}\nPayment marked verified."
        )
        await deliver_order(context, oid)

    elif action == "reject":
        with db() as con:
            order = con.execute(
                "SELECT * FROM orders WHERE id=?", (oid,)
            ).fetchone()
            if not order or order["status"] != "payment_submitted":
                await q.message.reply_text("This payment has already been processed or is invalid.")
                return
            con.execute(
                "UPDATE orders SET status='rejected' "
                "WHERE id=? AND status='payment_submitted'",
                (oid,),
            )
        await q.edit_message_text(f"❌ PAYMENT REJECTED\nOrder #{oid}")
        await context.bot.send_message(
            order["user_id"],
            f"❌ Your payment for Order #{oid} was rejected.\n"
            f"Please contact support: @{SUPPORT_USERNAME}",
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

    for r in rows:
        order_ref = r["payment_ref"] or f"ORDER-{r['id']}"
        text = (
            f"🧾 Order ID: {order_ref}\n"
            f"Product: {r['name']}\n"
            f"Amount: {fmt(r['amount'])}\n"
            f"Quantity: {r['quantity']}\n"
            f"Status: {r['status']}"
        )
        markup = None
        if r["status"] == "delivered":
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "♻️ Recover Voucher", callback_data=f"recover:{r['id']}"
                )
            ]])
        await update.message.reply_text(text, reply_markup=markup)
    await update.message.reply_text(
        "To recover a delivered order, send /recover YOUR_ORDER_ID.",
        reply_markup=MENU,
    )


async def send_recovered_order(message, user_id: int, identifier: str):
    """Find a customer's own delivered order and resend its archived delivery."""
    with db() as con:
        if identifier.isdigit():
            order = con.execute(
                "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id "
                "WHERE o.id=? AND o.user_id=?",
                (int(identifier), user_id),
            ).fetchone()
        else:
            order = con.execute(
                "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id "
                "WHERE o.payment_ref=? AND o.user_id=?",
                (identifier, user_id),
            ).fetchone()
        if not order:
            await message.reply_text("❌ Order not found. Check your Order ID.")
            return
        if order["status"] != "delivered":
            await message.reply_text(
                f"This order is not delivered yet. Current status: {order['status']}"
            )
            return
        archive = con.execute(
            "SELECT * FROM delivery_archive WHERE order_id=?", (order["id"],)
        ).fetchone()

    if not archive:
        await message.reply_text(
            "⚠️ This order was delivered before recovery storage was enabled, "
            "so its old voucher cannot be retrieved automatically. Please contact support."
        )
        return
    order_ref = order["payment_ref"] or f"ORDER-{order['id']}"
    if archive["delivery_type"] == "codes" and archive["payload"]:
        await message.reply_text(
            f"♻️ Voucher recovery successful!\n"
            f"🧾 Order ID: {order_ref}\n"
            f"Product: {order['name']}\n"
            f"Your original voucher codes:\n{archive['payload']}"
        )
    elif archive["delivery_type"] == "file":
        file_path = archive["file_path"]
        if not file_path or not Path(file_path).is_file():
            await message.reply_text(
                "⚠️ The original digital file is no longer available. Contact support."
            )
            return
        with open(file_path, "rb") as f:
            await message.reply_document(
                document=f,
                caption=f"♻️ Recovered file for Order {order_ref}"
            )
    else:
        await message.reply_text("No recoverable delivery was saved for this order.")


async def recover_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /recover ORDER_ID\nExample: /recover PH-20261110-A1B2C3"
        )
        return
    identifier = " ".join(context.args).strip()
    await send_recovered_order(
        update.effective_message, update.effective_user.id, identifier
    )


async def recover_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        oid = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await q.message.reply_text("Invalid order ID.")
        return
    await send_recovered_order(q.message, q.from_user.id, str(oid))


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"❤️ Contact Support @{SUPPORT_USERNAME}",
            url=f"https://t.me/{SUPPORT_USERNAME}",
        )
    ]])
    await update.message.reply_text(
        "❤️ Support\n"
        f"Contact support: @{SUPPORT_USERNAME}\n"
        "Tap below to open Telegram support.",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if admin(update):
        return
    await context.bot.send_message(
        ADMIN_ID,
        f"❤️ Support message from {update.effective_user.full_name} "
        f"(ID {update.effective_user.id}):\n{update.message.text}",
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
        if not name or price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a product name and a price greater than zero.")
        return
    with db() as con:
        cur = con.execute(
            "INSERT INTO products(name,price,description) VALUES(?,?,?)",
            (name, price, description),
        )
    await update.message.reply_text(f"✅ Product added. ID: {cur.lastrowid}")


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
    code = parts[1].strip()
    if not code:
        await update.message.reply_text("Coupon code cannot be empty.")
        return
    with db() as con:
        product = con.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
        if not product:
            await update.message.reply_text("Product not found.")
            return
        con.execute(
            "INSERT INTO coupons(product_id,code) VALUES(?,?)", (pid, code)
        )
    await update.message.reply_text("✅ Coupon code added to stock.")


async def addfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    try:
        pid = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /addfile PRODUCT_ID, then send the file.")
        return
    with db() as con:
        product = con.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
    if not product:
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
    await update.message.reply_text("✅ Digital file uploaded and linked to product.")


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    with db() as con:
        orders = con.execute(
            "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id "
            "WHERE o.status IN ('awaiting_payment','payment_submitted') "
            "ORDER BY o.id DESC"
        ).fetchall()
    if not orders:
        await update.message.reply_text("No pending orders.")
        return
    for o in orders:
        text = (
            f"Order #{o['id']} | {o['name']} | {fmt(o['amount'])}\n"
            f"User: {o['user_id']} | Status: {o['status']} | UTR: {o['utr'] or '—'}"
        )
        markup = (
            payment_review_keyboard(o["id"])
            if o["status"] == "payment_submitted" else None
        )
        await update.message.reply_text(text, reply_markup=markup)


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update) or not context.args:
        return
    try:
        oid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /approve ORDER_ID")
        return
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not order or order["status"] != "payment_submitted":
            await update.message.reply_text("Order not found or no UPI payment submitted.")
            return
        con.execute(
            "UPDATE orders SET status='paid' WHERE id=? AND status='payment_submitted'",
            (oid,),
        )
    await update.message.reply_text("✅ Payment marked verified. Attempting delivery.")
    await deliver_order(context, oid)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        delivered = con.execute(
            "SELECT COUNT(*) FROM orders WHERE status='delivered'"
        ).fetchone()[0]
        revenue = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='delivered'"
        ).fetchone()[0]
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    await update.message.reply_text(
        f"📊 Store stats\nUsers: {users}\nOrders: {total}\n"
        f"Delivered: {delivered}\nDelivered sales: {fmt(revenue)}"
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if admin(update):
        await update.message.reply_text(
            "🛠 Admin Panel\nChoose an action:", reply_markup=ADMIN_PANEL
        )


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not admin(update):
        await q.edit_message_text("Access denied.")
        return
    action = q.data.split(":", 1)[1]
    if action == "broadcast":
        context.user_data["awaiting_broadcast"] = True
        await q.message.reply_text(
            "📢 Send the text message to broadcast. Nothing is sent until confirmation."
        )
    elif action == "addproduct":
        await q.message.reply_text("Use: /addproduct NAME|PRICE|DESCRIPTION")
    elif action == "addcoupon":
        await q.message.reply_text("Use: /addcoupon PRODUCT_ID|CODE")
    elif action == "deleteproduct":
        context.user_data["awaiting_delete_product"] = True
        await q.message.reply_text("Send Product ID to deactivate, or /cancel.")
    elif action == "personal_coupon":
        context.user_data["awaiting_personal_coupon"] = True
        await q.message.reply_text(
            "Send: TELEGRAM_USER_ID|PRODUCT_ID\nExample: 123456789|2\n"
            "Type /cancel to stop."
        )
    elif action == "pending":
        await pending_from_panel(update, context)
    elif action == "stats":
        await stats_from_panel(update, context)


async def pending_from_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        rows = con.execute(
            "SELECT id,user_id,amount,status FROM orders "
            "WHERE status IN ('awaiting_payment','payment_submitted') "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
    if not rows:
        await update.callback_query.message.reply_text("No pending orders.")
        return
    for r in rows:
        markup = payment_review_keyboard(r["id"]) if r["status"] == "payment_submitted" else None
        await update.callback_query.message.reply_text(
            f"#{r['id']} | User {r['user_id']} | {fmt(r['amount'])} | {r['status']}",
            reply_markup=markup,
        )


async def stats_from_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        delivered = con.execute(
            "SELECT COUNT(*) FROM orders WHERE status='delivered'"
        ).fetchone()[0]
        revenue = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='delivered'"
        ).fetchone()[0]
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    await update.callback_query.message.reply_text(
        f"📊 Store stats\nUsers: {users}\nOrders: {total}\n"
        f"Delivered: {delivered}\nDelivered sales: {fmt(revenue)}"
    )


async def broadcast_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not admin(update) or not context.user_data.get("awaiting_broadcast"):
        return False
    if not msg or not msg.text:
        await msg.reply_text("Please send a text message, or /cancel.")
        return True
    if msg.text.strip() == "/cancel":
        context.user_data.pop("awaiting_broadcast", None)
        await msg.reply_text("Broadcast cancelled.")
        return True
    context.user_data["broadcast_message_id"] = msg.message_id
    context.user_data["broadcast_chat_id"] = msg.chat_id
    context.user_data.pop("awaiting_broadcast", None)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Send to all users", callback_data="broadcast:confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="broadcast:cancel"),
    ]])
    await msg.reply_text(
        "Preview received. Confirm to send to all registered users.",
        reply_markup=keyboard,
    )
    return True


async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not admin(update):
        await q.edit_message_text("Access denied.")
        return
    if q.data == "broadcast:cancel":
        context.user_data.pop("broadcast_message_id", None)
        context.user_data.pop("broadcast_chat_id", None)
        await q.edit_message_text("Broadcast cancelled.")
        return
    message_id = context.user_data.pop("broadcast_message_id", None)
    chat_id = context.user_data.pop("broadcast_chat_id", None)
    if not message_id or not chat_id:
        await q.edit_message_text("No pending broadcast. Open Admin Panel and try again.")
        return
    with db() as con:
        recipients = [
            r[0] for r in con.execute("SELECT user_id FROM users").fetchall()
        ]
    sent = failed = 0
    await q.edit_message_text(f"📢 Broadcasting to {len(recipients)} users...")
    for uid in recipients:
        try:
            await context.bot.copy_message(
                chat_id=uid, from_chat_id=chat_id, message_id=message_id
            )
            sent += 1
        except Exception as exc:
            failed += 1
            log.warning("Broadcast failed for %s: %s", uid, exc)
        await asyncio.sleep(0.05)
    await q.message.reply_text(
        f"✅ Broadcast finished.\nSent: {sent}\nFailed: {failed}",
        reply_markup=ADMIN_PANEL,
    )


async def process_delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE, value):
    try:
        pid = int(value.strip())
    except ValueError:
        await update.message.reply_text("Product ID must be a number, or /cancel.")
        return
    with db() as con:
        cur = con.execute(
            "UPDATE products SET active=0 WHERE id=? AND active=1", (pid,)
        )
    context.user_data.pop("awaiting_delete_product", None)
    await update.message.reply_text(
        f"Product #{pid} removed from the store." if cur.rowcount
        else "Active product not found."
    )


async def process_personal_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE, value):
    parts = [p.strip() for p in value.split("|", 1)]
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await update.message.reply_text(
            "Use TELEGRAM_USER_ID|PRODUCT_ID. Try again or /cancel."
        )
        return
    user_id, pid = int(parts[0]), int(parts[1])
    with db() as con:
        product = con.execute(
            "SELECT name FROM products WHERE id=? AND active=1", (pid,)
        ).fetchone()
        if not product:
            await update.message.reply_text("Active product not found.")
            return
        coupon = con.execute(
            "SELECT id,code FROM coupons WHERE product_id=? "
            "AND reserved_order_id IS NULL ORDER BY id LIMIT 1", (pid,)
        ).fetchone()
        if not coupon:
            await update.message.reply_text("No unused coupon is available.")
            return
        reserved = con.execute(
            "UPDATE coupons SET reserved_order_id=-1 "
            "WHERE id=? AND reserved_order_id IS NULL", (coupon["id"],)
        )
        if reserved.rowcount != 1:
            await update.message.reply_text("Coupon was just taken. Try again.")
            return
    try:
        await context.bot.send_message(
            user_id,
            f"🎁 Personal coupon from the store\n{product['name']}\n"
            f"Your code: {coupon['code']}",
        )
    except Exception as exc:
        with db() as con:
            con.execute(
                "UPDATE coupons SET reserved_order_id=NULL "
                "WHERE id=? AND reserved_order_id=-1", (coupon["id"],)
            )
        await update.message.reply_text(
            f"Could not send coupon. The user must start the bot first. Details: {exc}"
        )
        return
    with db() as con:
        con.execute(
            "DELETE FROM coupons WHERE id=? AND reserved_order_id=-1",
            (coupon["id"],),
        )
    context.user_data.pop("awaiting_personal_coupon", None)
    await update.message.reply_text(
        f"✅ Coupon sent to user {user_id}.", reply_markup=ADMIN_PANEL
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    ensure_user(update.effective_user)

    menu_actions = {
        "🛒 Buy Vouchers", "🔥 My Orders", "❤️ Support", "🛠 Admin Panel"
    }
    if text in menu_actions:
        context.user_data.pop("awaiting_quantity", None)
        context.user_data.pop("awaiting_utr", None)

    if context.user_data.get("awaiting_utr"):
        oid = context.user_data.pop("awaiting_utr")
        if len(text) < 6:
            await update.message.reply_text(
                "Please send a valid UTR/reference number (at least 6 characters)."
            )
            context.user_data["awaiting_utr"] = oid
            return
        await submit_utr(update, context, oid, text)
        return

    if admin(update) and context.user_data.get("awaiting_broadcast"):
        await broadcast_preview(update, context)
        return
    if admin(update) and context.user_data.get("awaiting_delete_product"):
        await process_delete_product(update, context, text)
        return
    if admin(update) and context.user_data.get("awaiting_personal_coupon"):
        await process_personal_coupon(update, context, text)
        return

    if text == "🛠 Admin Panel" and admin(update):
        await admin_command(update, context)
    elif text == "🛒 Buy Vouchers":
        await show_products(update, context)
    elif text == "🔥 My Orders":
        await my_orders(update, context)
    elif text == "❤️ Support":
        await support(update, context)
    elif context.user_data.get("awaiting_quantity"):
        await quantity_text_handler(update, context)
    else:
        await support_message(update, context)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "awaiting_quantity", "awaiting_utr", "awaiting_broadcast",
        "broadcast_message_id", "broadcast_chat_id",
        "awaiting_delete_product", "awaiting_personal_coupon",
        "file_product_id",
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text(
        "Cancelled. Choose an option from the menu.",
        reply_markup=ADMIN_MENU if admin(update) else MENU,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("paid", paid_command))
    app.add_handler(CommandHandler("recover", recover_order))
    app.add_handler(CommandHandler("addproduct", addproduct))
    app.add_handler(CommandHandler("addcoupon", addcoupon))
    app.add_handler(CommandHandler("addfile", addfile))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(broadcast_callback, pattern=r"^broadcast:"))
    app.add_handler(CallbackQueryHandler(
        payment_review_callback, pattern=r"^payreview:(approve|reject):\d+$"
    ))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(recover_callback, pattern=r"^recover:\d+$"))
    app.add_handler(CallbackQueryHandler(
        verify_payment_callback, pattern=r"^verify:\d+$"
    ))

    app.add_handler(MessageHandler(filters.Document.ALL, receive_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    log.info("Voucher store bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
