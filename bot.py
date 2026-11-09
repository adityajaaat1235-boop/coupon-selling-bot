"""
Voucher Store Telegram Bot
Python 3.10+
Features: Buy Vouchers, My Orders, Support (@Adityajat55), Admin Panel.
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
import io
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import qrcode
from pathlib import Path
from contextlib import contextmanager

from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from telegram.constants import ChatAction

from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_TEXT = os.getenv("ADMIN_ID", "").strip()
UPI_ID = os.getenv("UPI_ID", "").strip()
UPI_NAME = os.getenv("UPI_NAME", "Voucher Store").strip()
SUPPORT_USERNAME = "Adityajat55"
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
        [KeyboardButton("🛒 Buy Vouchers"))],
        [
            KeyboardButton("🔥 My Orders", style="primary"),
            KeyboardButton("❤️ Support", style="primary"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

ADMIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🛠 Admin Panel", style="primary")],
        [KeyboardButton("🛒 Buy Vouchers", style="success")],
        [
            KeyboardButton("🔥 My Orders", style="primary"),
            KeyboardButton("❤️ Support", style="primary"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

ADMIN_PANEL = InlineKeyboardMarkup([
    [InlineKeyboardButton("📢 Broadcast Message", callback_data="admin:broadcast", style="primary")],
    [InlineKeyboardButton("➕ Add Product", callback_data="admin:addproduct", style="success"),
     InlineKeyboardButton("🗑 Delete Product", callback_data="admin:deleteproduct", style="danger")],
    [InlineKeyboardButton("🎟 Add Coupon", callback_data="admin:addcoupon", style="success"),
     InlineKeyboardButton("🎁 Send Personal Coupon", callback_data="admin:personal_coupon", style="success")],
    [InlineKeyboardButton("🧾 Pending Payments", callback_data="admin:pending", style="primary"),
     InlineKeyboardButton("📊 Statistics", callback_data="admin:stats", style="primary")],
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
        if "quantity" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
        if "payment_ref" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN payment_ref TEXT")
        if "expires_at" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN expires_at TEXT")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_ref "
            "ON orders(payment_ref) WHERE payment_ref IS NOT NULL"
        )


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
        reply_markup=ADMIN_MENU if admin(update) else MENU,
    )


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
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
        await update.message.reply_text(
            "No vouchers are available right now.", reply_markup=MENU
        )
        return

    buttons = []
    for p in products:
        available = stock.get(p["id"], 0) > 0 or p["id"] in digital
        if available:
            buttons.append([InlineKeyboardButton(
                f"🛒 {p['name']} · {fmt(p['price'])}",
                callback_data=f"buy:{p['id']}",
                style="success",
            )])

    if not buttons:
        await update.message.reply_text(
            "All vouchers are currently out of stock.", reply_markup=MENU
        )
        return

    await update.message.reply_text(
        "👇 Select a service to continue:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


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
        stock = con.execute(
            "SELECT COUNT(*) FROM coupons "
            "WHERE product_id=? AND reserved_order_id IS NULL", (pid,)
        ).fetchone()[0]
        digital = con.execute(
            "SELECT file_path FROM digital_products WHERE product_id=?", (pid,)
        ).fetchone()

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
        "digital_only": digital_available,
    }
    max_qty = 1 if digital_available else min(stock, 50)
    await q.edit_message_text(
        f"🛒 {p['name']}\n"
        f"Price per unit: {fmt(p['price'])}\n"
        f"Available: {'1 digital file' if digital_available else stock}\n\n"
        f"How many would you like? Allowed: 1–{max_qty} units.\n"
        "Type a number below."
    )


async def quantity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, pid_text, qty_text = q.data.split(":")
        pid, quantity = int(pid_text), int(qty_text)
        if quantity < 1 or quantity > 10:
            raise ValueError
    except Exception:
        await q.edit_message_text("Invalid quantity. Please try again.")
        return
    with db() as con:
        p = con.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            await q.edit_message_text("Product is unavailable.")
            return
        stock = con.execute(
            "SELECT COUNT(*) FROM coupons WHERE product_id=? AND reserved_order_id IS NULL", (pid,)
        ).fetchone()[0]
        digital = con.execute(
            "SELECT file_path FROM digital_products WHERE product_id=?", (pid,)
        ).fetchone()
        if digital and Path(digital["file_path"]).is_file() and stock == 0:
            if quantity != 1:
                await q.edit_message_text("Digital-file products can currently be ordered one at a time.")
                return
        elif stock < quantity:
            await q.edit_message_text(f"Only {stock} coupon(s) are currently available. Please choose again.")
            return
        total = round(float(p["price"]) * quantity, 2)
        cur = con.execute(
            "INSERT INTO orders(user_id, product_id, amount, quantity, status) VALUES(?,?,?,?, 'awaiting_payment')",
            (q.from_user.id, pid, total, quantity),
        )
        oid = cur.lastrowid
    await q.edit_message_text(
        f"🧾 Order #{oid}\n{p['name']}\nQuantity: {quantity}\n"
        f"Price each: {fmt(p['price'])}\nTotal: {fmt(total)}\n\n"
        "Choose payment:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Pay using UPI", callback_data=f"upipay:{oid}")
        ]]),
    )


async def quantity_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Read a typed quantity after the customer selects a product."""
    pending = context.user_data.get("awaiting_quantity")
    if not pending:
        return False

    raw = (update.message.text or "").strip()
    if not raw.isdigit():
        await update.message.reply_text("Please type a whole number, for example: 2")
        return True

    quantity = int(raw)
    if not 1 <= quantity <= 50:
        await update.message.reply_text("Quantity must be between 1 and 50. Try again.")
        return True

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
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
            "SELECT COUNT(*) FROM coupons "
            "WHERE product_id=? AND reserved_order_id IS NULL", (pid,)
        ).fetchone()[0]
        digital = con.execute(
            "SELECT file_path FROM digital_products WHERE product_id=?", (pid,)
        ).fetchone()

        digital_available = bool(
            digital and Path(digital["file_path"]).is_file() and stock == 0
        )
        if digital_available and quantity != 1:
            await update.message.reply_text(
                "This digital-file product can only be ordered one at a time."
            )
            return True
        if not digital_available and stock < quantity:
            await update.message.reply_text(
                f"Only {stock} unit(s) are currently available. "
                "Please enter a smaller quantity."
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
    # Send the QR immediately after quantity confirmation; no second payment-button step.
    await send_payment_qr(
        context=context,
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id,
        oid=oid,
        status_message=None,
    )


async def send_payment_qr(context, chat_id: int, user_id: int, oid: int, status_message=None):
    """Create and send one UPI QR photo with the complete order details in its caption."""
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=10)

    with db() as con:
        order = con.execute(
            "SELECT o.*, p.name FROM orders o JOIN products p ON p.id=o.product_id "
            "WHERE o.id=? AND o.user_id=?", (oid, user_id)
        ).fetchone()
        if not order or order["status"] != "awaiting_payment":
            if status_message:
                await status_message.reply_text("This order is no longer awaiting payment.")
            else:
                await context.bot.send_message(chat_id, "This order is no longer awaiting payment.")
            return
        con.execute(
            "UPDATE orders SET payment_method='upi', expires_at=? WHERE id=?",
            (expires_at.isoformat(), oid),
        )

    if not UPI_ID:
        message = "⚠️ UPI is not configured. Please contact support; no QR could be generated."
        if status_message:
            await status_message.reply_text(message)
        else:
            await context.bot.send_message(chat_id, message)
        return

    upi_params = {
        "pa": UPI_ID,
        "pn": UPI_NAME,
        "am": f"{float(order['amount']):.2f}",
        "cu": "INR",
        "tn": order["payment_ref"] or f"Order {oid}",
    }
    upi_uri = "upi://pay?" + urlencode(upi_params)
    qr = qrcode.QRCode(box_size=8, border=3)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    photo = io.BytesIO()
    photo.name = f"payment_{order['payment_ref'] or oid}.png"
    image.save(photo, format="PNG")
    photo.seek(0)

    caption = (
        "💳 Paytm Business Payment\n\n"
        f"🧾 Order ID: {order['payment_ref'] or f'ORDER-{oid}'}\n"
        f"⭐️ Service: {order['name']}\n"
        "⚡ Instant Use\n"
        f"📦 Qty: {order['quantity']}\n"
        f"💰 Amount: {fmt(order['amount'])}\n"
        "📲 Scan the QR with any UPI app\n"
        "⏰ QR valid for 10 minutes\n\n"
        "After payment, tap ⭐️ Verify Paytm Payment and submit your UTR. "
        "Payment is verified before voucher delivery."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐️ Verify Paytm Payment", callback_data=f"verify:{oid}", style="success")
    ]])

    if status_message:
        await status_message.reply_text("Preparing your payment QR…")

    await context.bot.send_photo(
        chat_id=chat_id,
        photo=photo,
        caption=caption,
        reply_markup=keyboard,
    )
    await context.bot.send_message(
        ADMIN_ID,
        f"New payment QR created\nOrder: {order['payment_ref']}\n"
        f"User: {user_id}\nAmount: {fmt(order['amount'])}\n"
        "QR expires in 10 minutes."
    )


async def payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        oid = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await q.edit_message_text("Invalid order.")
        return

    await send_payment_qr(
        context=context,
        chat_id=q.message.chat_id,
        user_id=q.from_user.id,
        oid=oid,
        status_message=q.message,
    )


async def verify_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        oid = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await q.message.reply_text("Invalid order.")
        return

    with db() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id=? AND user_id=?",
            (oid, q.from_user.id),
        ).fetchone()

    if not order or order["status"] != "awaiting_payment":
        await q.message.reply_text("This order is not awaiting payment.")
        return
    if not order["expires_at"] or datetime.now(timezone.utc) > datetime.fromisoformat(order["expires_at"]):
        with db() as con:
            con.execute(
                "UPDATE orders SET status='expired' WHERE id=? AND status='awaiting_payment'",
                (oid,),
            )
        await q.message.reply_text(
            "⏰ This QR has expired. Please start a new order to get a fresh QR."
        )
        return

    context.user_data["awaiting_utr"] = oid
    await q.message.reply_text(
        f"🧾 Order {order['payment_ref']}\n"
        "Please send your UPI transaction UTR/reference number. "
        "The admin will verify the payment before delivery."
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
        if not order["expires_at"] or datetime.now(timezone.utc) > datetime.fromisoformat(order["expires_at"]):
            con.execute("UPDATE orders SET status='expired' WHERE id=?", (oid,))
            await update.message.reply_text("⏰ QR expired. Please create a new order and payment QR.")
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
        quantity = int(order["quantity"] or 1)
        coupons = con.execute(
            "SELECT id, code FROM coupons WHERE product_id=? AND reserved_order_id IS NULL ORDER BY id LIMIT ?",
            (order["product_id"], quantity),
        ).fetchall()
        coupon = coupons[0] if coupons else None
        digital = con.execute(
            "SELECT file_path FROM digital_products WHERE product_id=?", (order["product_id"],)
        ).fetchone()
        if len(coupons) < quantity and not (not coupons and quantity == 1 and digital and Path(digital["file_path"]).is_file()):
            await context.bot.send_message(ADMIN_ID, f"Order #{oid} paid but {quantity} coupon(s)/file stock is missing.")
            return False
        # Reserve all requested coupons before sending to avoid duplicate delivery.
        if coupons:
            for item in coupons:
                con.execute("UPDATE coupons SET reserved_order_id=? WHERE id=? AND reserved_order_id IS NULL",
                            (oid, item["id"]))
    try:
        if coupons:
            codes = "\n".join(f"{i}. {item['code']}" for i, item in enumerate(coupons, 1))
            await context.bot.send_message(
                order["user_id"],
                f"🎉 Payment confirmed!\nOrder #{oid}\n{order['name']}\n"
                f"Quantity: {quantity}\nYour voucher codes:\n{codes}",
            )
        else:
            with open(digital["file_path"], "rb") as f:
                await context.bot.send_document(
                    order["user_id"], document=f, caption=f"Order #{oid}: {order['name']}"
                )
        with db() as con:
            if coupons:
                con.execute("DELETE FROM coupons WHERE reserved_order_id=?", (oid,))
            con.execute("UPDATE orders SET status='delivered', coupon_id=? WHERE id=?",
                        (coupons[0]["id"] if coupons else None, oid))
        await context.bot.send_message(ADMIN_ID, f"✅ Order #{oid} delivered.")
        return True
    except Exception:
        log.exception("Delivery failed for order %s", oid)
        # Keep reservation so an admin can inspect before retrying; do not risk double-selling.
        await context.bot.send_message(
            ADMIN_ID, f"⚠️ Delivery for order #{oid} failed. Check customer chat and stock before retrying."
        )
        return False


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
    username = "Adityajat55"
    msg = (
        "❤️ Support\\n"
        f"Contact support: @{username}\\n"
        "Tap the button below to open Telegram support."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "❤️ Contact Support @Adityajat55",
            url="https://t.me/Adityajat55",
        )
    ]])
    await update.message.reply_text(
        msg,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


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
    if not orders:
        await update.message.reply_text("No pending orders.")
        return
    for o in orders:
        await update.message.reply_text(
            f"Order #{o['id']} | {o['name']} | {fmt(o['amount'])}\n"
            f"User: {o['user_id']} | Status: {o['status']} | UTR: {o['utr'] or '—'}"
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
        # The customer must submit the UTR before expiry. Admin may verify it later.
        con.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
    await update.message.reply_text("Payment marked verified. Attempting delivery.")
    await deliver_order(context, oid)


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


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update):
        return
    await update.message.reply_text("🛠 Admin Panel\nChoose an action:", reply_markup=ADMIN_PANEL)


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not admin(update):
        await q.edit_message_text("Access denied.")
        return
    action = q.data.split(":", 1)[1]
    if action == "broadcast":
        context.user_data["awaiting_broadcast"] = True
        context.user_data.pop("broadcast_message_id", None)
        await q.message.reply_text(
            "📢 Send the message you want to broadcast to all registered bot users.\n"
            "Send a text message. Nothing is sent until you confirm."
        )
    elif action == "addproduct":
        await q.message.reply_text("Use: /addproduct NAME|PRICE|DESCRIPTION")
    elif action == "addcoupon":
        await q.message.reply_text("Use: /addcoupon PRODUCT_ID|CODE (one code per command)")
    elif action == "deleteproduct":
        context.user_data["awaiting_delete_product"] = True
        await q.message.reply_text("Send the Product ID to deactivate, or /cancel.")
    elif action == "personal_coupon":
        context.user_data["awaiting_personal_coupon"] = True
        await q.message.reply_text(
            "Send: TELEGRAM_USER_ID|PRODUCT_ID\n"
            "Example: 123456789|2\n"
            "The bot will send one available coupon to that user. Type /cancel to stop."
        )
    elif action == "pending":
        await pending_from_panel(update, context)
    elif action == "stats":
        await stats_from_panel(update, context)


async def pending_from_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        rows = con.execute("SELECT id,user_id,amount,status FROM orders WHERE status IN ('created','payment_submitted') ORDER BY id DESC LIMIT 20").fetchall()
    text = "🧾 Pending orders\n\n" + ("\n".join(f"#{r['id']} | User {r['user_id']} | {fmt(r['amount'])} | {r['status']}" for r in rows) if rows else "No pending orders.")
    await update.callback_query.message.reply_text(text)


async def stats_from_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        delivered = con.execute("SELECT COUNT(*) FROM orders WHERE status='delivered'").fetchone()[0]
        revenue = con.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='delivered'").fetchone()[0]
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    await update.callback_query.message.reply_text(f"📊 Store stats\nUsers: {users}\nOrders: {total}\nDelivered: {delivered}\nDelivered sales: {fmt(revenue)}")


async def broadcast_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update) or not context.user_data.get("awaiting_broadcast"):
        return False
    msg = update.effective_message
    if not msg or not msg.text:
        await msg.reply_text("Please send a text message for the broadcast, or type /cancel.")
        return True
    if msg.text.strip() in {"/cancel", "Cancel"}:
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
    await msg.reply_text("Preview received. Confirm to send this message to all registered users.", reply_markup=keyboard)
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
        recipients = [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]
    sent = failed = 0
    await q.edit_message_text(f"📢 Broadcasting to {len(recipients)} registered users...")
    for uid in recipients:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=message_id)
            sent += 1
        except Exception as exc:
            failed += 1
            log.warning("Broadcast failed for user %s: %s", uid, exc)
        import asyncio
        await asyncio.sleep(0.05)
    await q.message.reply_text(f"✅ Broadcast finished.\nSent: {sent}\nFailed: {failed}", reply_markup=ADMIN_PANEL)


async def process_delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE, value: str):
    try:
        pid = int(value.strip())
    except ValueError:
        await update.message.reply_text("Product ID must be a number. Try again or /cancel.")
        return
    with db() as con:
        cur = con.execute("UPDATE products SET active=0 WHERE id=? AND active=1", (pid,))
    context.user_data.pop("awaiting_delete_product", None)
    await update.message.reply_text(
        f"Product #{pid} removed from the customer store." if cur.rowcount
        else "Active product not found."
    )


async def process_personal_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE, value: str):
    parts = [part.strip() for part in value.split("|", 1)]
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await update.message.reply_text("Use this format: TELEGRAM_USER_ID|PRODUCT_ID. Try again or /cancel.")
        return
    user_id, pid = int(parts[0]), int(parts[1])
    with db() as con:
        product = con.execute("SELECT name FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not product:
            await update.message.reply_text("Active product not found. Try again or /cancel.")
            return
        coupon = con.execute(
            "SELECT id, code FROM coupons WHERE product_id=? AND reserved_order_id IS NULL ORDER BY id LIMIT 1",
            (pid,),
        ).fetchone()
        if not coupon:
            await update.message.reply_text("No unused coupon is available for this product.")
            return
        reserved = con.execute(
            "UPDATE coupons SET reserved_order_id=-1 WHERE id=? AND reserved_order_id IS NULL",
            (coupon["id"],),
        )
        if reserved.rowcount != 1:
            await update.message.reply_text("That coupon was just taken. Please try again.")
            return
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🎁 A personal coupon from the store\n{product['name']}\nYour code: {coupon['code']}"
        )
    except Exception as exc:
        with db() as con:
            con.execute("UPDATE coupons SET reserved_order_id=NULL WHERE id=? AND reserved_order_id=-1", (coupon["id"],))
        await update.message.reply_text(f"Could not send coupon. Check the user's ID and make sure they started the bot. Details: {exc}")
        return
    with db() as con:
        con.execute("DELETE FROM coupons WHERE id=? AND reserved_order_id=-1", (coupon["id"],))
    context.user_data.pop("awaiting_personal_coupon", None)
    await update.message.reply_text(f"✅ One coupon for {product['name']} sent to user {user_id}.", reply_markup=ADMIN_PANEL)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ensure_user(update.effective_user)

    # Main-menu buttons must take priority over a pending quantity prompt.
    # This lets the customer leave a quantity step and choose another product.
    menu_actions = {
        "🛒 Buy Vouchers", "🔥 My Orders",
        "❤️ Support", "🛠 Admin Panel",
    }
    if text in menu_actions:
        context.user_data.pop("awaiting_quantity", None)

    if context.user_data.get("awaiting_utr"):
        oid = context.user_data.pop("awaiting_utr")
        if not text or len(text) < 6:
            await update.message.reply_text("Please send a valid UTR/reference number (at least 6 characters).")
            context.user_data["awaiting_utr"] = oid
            return
        with db() as con:
            order = con.execute(
                "SELECT * FROM orders WHERE id=? AND user_id=?",
                (oid, update.effective_user.id),
            ).fetchone()
            if not order or order["status"] != "awaiting_payment":
                await update.message.reply_text("This order is no longer awaiting payment.")
                return
            if not order["expires_at"] or datetime.now(timezone.utc) > datetime.fromisoformat(order["expires_at"]):
                con.execute("UPDATE orders SET status='expired' WHERE id=?", (oid,))
                await update.message.reply_text("⏰ QR expired. Please create a new order.")
                return
            con.execute(
                "UPDATE orders SET utr=?, status='payment_submitted' WHERE id=?",
                (text, oid),
            )
        await update.message.reply_text(
            "✅ UTR submitted. Delivery happens after admin verifies the actual payment."
        )
        await context.bot.send_message(
            ADMIN_ID,
            f"Payment submitted\nOrder: {order['payment_ref']} (#{oid})\n"
            f"User: {update.effective_user.id}\nUTR: {text}\n"
            f"Verify the actual transaction, then use /approve {oid}."
        )
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
        handled = await quantity_text_handler(update, context)
        if handled:
            return
    else:
        await support_message(update, context)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /cancel is available to every customer to exit quantity entry.
    context.user_data.pop("awaiting_quantity", None)

    if admin(update):
        context.user_data.pop("awaiting_broadcast", None)
        context.user_data.pop("broadcast_message_id", None)
        context.user_data.pop("broadcast_chat_id", None)
        context.user_data.pop("awaiting_delete_product", None)
        context.user_data.pop("awaiting_personal_coupon", None)
        await update.message.reply_text("Cancelled.", reply_markup=ADMIN_MENU)
    else:
        await update.message.reply_text(
            "Quantity selection cancelled. Choose an option from the menu.",
            reply_markup=MENU,
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
    app.add_handler(CommandHandler("addproduct", addproduct))
    app.add_handler(CommandHandler("addcoupon", addcoupon))
    app.add_handler(CommandHandler("addfile", addfile))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(broadcast_callback, pattern=r"^broadcast:"))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(quantity_callback, pattern=r"^qty:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(payment_callback, pattern=r"^upipay:\d+$"))
    app.add_handler(CallbackQueryHandler(verify_payment_callback, pattern=r"^verify:\d+$"))
    app.add_handler(MessageHandler(filters.Document.ALL, receive_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    log.info("Voucher store bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    main()
