#!/usr/bin/env python3
"""
UnZipper Pro — Professional Telegram Archive Extraction Bot
Owner: 8731647972
"""

import os
import sys
import time
import asyncio
import logging
import warnings
import shutil
import zipfile
import tarfile
import tempfile
import traceback
import httpx
from pathlib import Path
from datetime import datetime, timezone

# Suppress PTB per_message informational warnings — per_message=False is intentional
warnings.filterwarnings("ignore", message=".*per_message.*", category=UserWarning)

import humanize
import py7zr
import rarfile

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    constants,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

import database as db
from keep_alive import keep_alive
from config import (
    OWNER_ID,
    BOT_NAME,
    BOT_VERSION,
    MAX_FILE_SIZE,
    TEMP_DIR,
    SUPPORTED_FORMATS,
    TELEGRAM_BOT_TOKEN,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_START_TIME = time.time()

os.makedirs(TEMP_DIR, exist_ok=True)

# ── System helpers ─────────────────────────────────────────────────────────────

def get_disk_info() -> dict:
    """Return disk usage for /"""
    try:
        usage = shutil.disk_usage("/")
        return {
            "total": usage.total,
            "used":  usage.used,
            "free":  usage.free,
            "pct":   round(usage.used / usage.total * 100, 1),
        }
    except Exception:
        return {}


async def measure_download_speed() -> str:
    """Download 1 MB from Cloudflare and report Mbps."""
    url = "https://speed.cloudflare.com/__down?bytes=1048576"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            t0 = time.time()
            r = await client.get(url)
            elapsed = time.time() - t0
        size = len(r.content)
        mbps = (size * 8) / (elapsed * 1_000_000)
        return f"{mbps:.1f} Mbps"
    except Exception:
        return "N/A"


async def cleanup_old_sessions(ctx) -> None:
    """Job: delete session directories older than 30 minutes."""
    if not os.path.exists(TEMP_DIR):
        return
    cutoff = time.time() - (30 * 60)
    cleaned = 0
    for entry in Path(TEMP_DIR).iterdir():
        if not entry.is_dir():
            continue
        try:
            ts_file = entry / ".created_at"
            created = float(ts_file.read_text().strip()) if ts_file.exists() else entry.stat().st_mtime
            if created < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                cleaned += 1
        except Exception:
            pass
    if cleaned:
        logger.info(f"🧹 Auto-cleaned {cleaned} stale session(s) (>30 min old)")

# ── Conversation states ───────────────────────────────────────────────────────
(
    WAITING_FILE,
    WAITING_PASSWORD,
    SELECTING_FOLDERS,
    ADMIN_MENU,
    ADMIN_KEY_COUNT,
    ADMIN_KEY_DURATION,
    ADMIN_BROADCAST,
) = range(7)


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def progress_bar(current: int, total: int, length: int = 18) -> str:
    filled = int(length * current / total) if total else 0
    bar = "█" * filled + "░" * (length - filled)
    pct = int(100 * current / total) if total else 0
    return f"[{bar}] {pct}%"


def format_size(n: int) -> str:
    return humanize.naturalsize(n, binary=True)


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "Expired"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    return " ".join(parts) if parts else "< 1m"


def uptime_str() -> str:
    return format_duration(time.time() - BOT_START_TIME)


def main_menu_keyboard(is_admin: bool = False):
    kb = [
        [
            InlineKeyboardButton("📦  Unzip File", callback_data="menu_unzip"),
            InlineKeyboardButton("👤  My Profile", callback_data="menu_profile"),
        ],
        [
            InlineKeyboardButton("📊  My Stats", callback_data="menu_stats"),
            InlineKeyboardButton("ℹ️  Help", callback_data="menu_help"),
        ],
        [
            InlineKeyboardButton("🗂  Supported Formats", callback_data="menu_formats"),
            InlineKeyboardButton("📞  Support", callback_data="menu_support"),
        ],
    ]
    if is_admin:
        kb.append([InlineKeyboardButton("👑  Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)


def back_to_menu_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️  Main Menu", callback_data="back_main")]])


def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊  Bot Stats",      callback_data="admin_stats"),
            InlineKeyboardButton("👥  All Users",       callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton("🔑  Generate Keys",  callback_data="admin_gen_keys"),
            InlineKeyboardButton("📢  Broadcast",       callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton("🔍  Lookup User",    callback_data="admin_lookup"),
            InlineKeyboardButton("⏫  Active Users",   callback_data="admin_active"),
        ],
        [
            InlineKeyboardButton("⬅️  Main Menu",      callback_data="back_main"),
        ],
    ])


# ═══════════════════════════════════════════════════════════════════════════════
#  Archive utilities
# ═══════════════════════════════════════════════════════════════════════════════

def detect_format(path: str) -> str:
    name = path.lower()
    for ext in [".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz"]:
        if name.endswith(ext):
            return ext
    return Path(name).suffix


def list_archive_members(path: str, password: bytes | None = None) -> list[str]:
    """Return list of member paths inside the archive."""
    fmt = detect_format(path)
    members = []

    if fmt == ".zip":
        with zipfile.ZipFile(path) as z:
            members = z.namelist()
    elif fmt == ".7z":
        with py7zr.SevenZipFile(path, mode="r", password=password.decode() if password else None) as z:
            members = z.getnames()
    elif fmt == ".rar":
        with rarfile.RarFile(path) as r:
            members = r.namelist()
    elif fmt in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"):
        with tarfile.open(path) as t:
            members = t.getnames()
    elif fmt in (".gz", ".bz2", ".xz"):
        members = [Path(path).stem]

    return members


def needs_password(path: str) -> bool:
    fmt = detect_format(path)
    try:
        if fmt == ".zip":
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    if info.flag_bits & 0x1:
                        return True
        elif fmt == ".7z":
            with py7zr.SevenZipFile(path, mode="r") as z:
                return z.needs_password()
        elif fmt == ".rar":
            with rarfile.RarFile(path) as r:
                return r.needs_password()
    except Exception:
        pass
    return False


def extract_archive(path: str, dest: str, password: bytes | None = None) -> bool:
    """Extract archive to dest directory. Returns True on success."""
    fmt = detect_format(path)
    os.makedirs(dest, exist_ok=True)

    if fmt == ".zip":
        with zipfile.ZipFile(path) as z:
            z.extractall(dest, pwd=password)
    elif fmt == ".7z":
        with py7zr.SevenZipFile(path, mode="r", password=password.decode() if password else None) as z:
            z.extractall(dest)
    elif fmt == ".rar":
        with rarfile.RarFile(path) as r:
            if password:
                r.setpassword(password)
            r.extractall(dest)
    elif fmt in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"):
        with tarfile.open(path) as t:
            t.extractall(dest)
    elif fmt == ".gz":
        import gzip
        out = os.path.join(dest, Path(path).stem)
        with gzip.open(path, "rb") as f_in, open(out, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    elif fmt == ".bz2":
        import bz2
        out = os.path.join(dest, Path(path).stem)
        with bz2.open(path, "rb") as f_in, open(out, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    elif fmt == ".xz":
        import lzma
        out = os.path.join(dest, Path(path).stem)
        with lzma.open(path, "rb") as f_in, open(out, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    else:
        return False
    return True


def scan_extracted(dest: str):
    """
    Returns (top_level_folders, flat_files).
    top_level_folders: list of (name, [abs_file_paths])
    flat_files: list of absolute file paths in root
    """
    base = Path(dest)
    top_dirs = {}
    flat = []

    for item in sorted(base.iterdir()):
        if item.is_dir():
            files = [str(f) for f in sorted(item.rglob("*")) if f.is_file()]
            top_dirs[item.name] = files
        elif item.is_file():
            flat.append(str(item))

    return top_dirs, flat


# ═══════════════════════════════════════════════════════════════════════════════
#  Command handlers — /start, /redeem, /admin, /cancel
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name, user.last_name)
    ctx.user_data.clear()

    if db.is_user_active(user.id) or user.id == OWNER_ID:
        await _send_main_menu(update, ctx)
        return ConversationHandler.END

    name = user.first_name or "there"
    text = (
        f"Hey 👋 *{name}*!\n\n"
        f"I'm *{BOT_NAME}* ⚡ — a premium archive extraction bot.\n\n"
        f"🔒 *You don't have access yet.*\n"
        f"I'm limited to licensed users only.\n\n"
        f"If you have a licence key, use:\n"
        f"`/redeem YOUR-KEY`\n\n"
        f"I'll unlock full access for you 🤗"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    return ConversationHandler.END


async def cmd_redeem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name, user.last_name)

    args = ctx.args
    if not args:
        await update.message.reply_text(
            "🔑 Usage: `/redeem YOUR-KEY-HERE`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    key = args[0].strip().upper()
    ok, msg, dur = db.redeem_license(key, user.id)

    if ok:
        name = user.first_name or "there"
        _, label = db.parse_duration("1D")  # ignored, just get label style
        dur_txt = format_duration(dur)
        text = (
            f"🎉 *Access Granted, {name}!*\n\n"
            f"✅ Your licence has been activated.\n"
            f"⏳ Valid for: `{dur_txt}`\n\n"
            f"Welcome to *{BOT_NAME}* ⚡"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        await asyncio.sleep(1)
        await _send_main_menu(update, ctx)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

    return ConversationHandler.END


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("❌ You are not authorised to use this command.")
        return ConversationHandler.END

    # Ensure the owner is registered in the DB
    db.upsert_user(user.id, user.username, user.first_name, user.last_name)

    await update.message.reply_text(
        f"👑 *Admin Panel — {BOT_NAME}*\n\nSelect an option below:",
        parse_mode="Markdown",
        reply_markup=admin_panel_keyboard(),
    )
    return ADMIN_MENU


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelled. Returning to main menu...")
    await _send_main_menu(update, ctx)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
#  Main menu / callback router
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    user = update.effective_user
    is_admin = user.id == OWNER_ID
    text = (
        f"⚡ *{BOT_NAME}* — Main Menu\n\n"
        f"Hello *{user.first_name}* 👋\n"
        f"What would you like to do today?"
    )
    kb = main_menu_keyboard(is_admin)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    user = update.effective_user

    # ── Guard: check access for non-admin, non-back actions ──────────────────
    # FIX: menu_support and menu_help and menu_formats are always accessible
    protected = (
        data.startswith("menu_")
        and data not in ("menu_help", "menu_formats", "menu_support")
    )
    if protected and not db.is_user_active(user.id) and user.id != OWNER_ID:
        await q.answer("🔒 You need an active licence! Use /redeem YOUR-KEY", show_alert=True)
        return

    # ── Back to main menu ─────────────────────────────────────────────────────
    if data == "back_main":
        ctx.user_data.clear()
        await _send_main_menu(update, ctx, edit=True)
        return ConversationHandler.END

    # ── Menu pages ────────────────────────────────────────────────────────────
    if data == "menu_profile":
        await _show_profile(update, ctx)
        return

    if data == "menu_stats":
        await _show_user_stats(update, ctx)
        return

    if data == "menu_help":
        await _show_help(update, ctx)
        return

    if data == "menu_formats":
        await _show_formats(update, ctx)
        return

    if data == "menu_support":
        text = (
            "📞 *Support*\n\n"
            "Need help? Reach out to the bot owner.\n\n"
            "⚡ Powered by *UnZipper Pro*"
        )
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_to_menu_kb())
        return

    # ── Unzip flow ────────────────────────────────────────────────────────────
    if data == "menu_unzip":
        await q.edit_message_text(
            "📦 *Send me your archive file!*\n\n"
            "Supported: `.zip` `.7z` `.rar` `.tar` `.tar.gz` `.tar.bz2` `.tar.xz` and more.\n\n"
            f"⚠️ Max size: `{MAX_FILE_SIZE // (1024*1024)} MB`\n\n"
            "Send the file now, or /cancel to abort.",
            parse_mode="Markdown",
        )
        return WAITING_FILE

    # ── Folder selection ──────────────────────────────────────────────────────
    if data.startswith("folder_toggle:"):
        await _toggle_folder(update, ctx)
        return SELECTING_FOLDERS

    if data.startswith("folder_all:"):
        await _select_all_folders(update, ctx)
        return SELECTING_FOLDERS

    if data.startswith("folder_none:"):
        await _deselect_all_folders(update, ctx)
        return SELECTING_FOLDERS

    if data.startswith("folder_confirm:"):
        await _confirm_folders(update, ctx)
        return ConversationHandler.END

    if data == "folder_cancel":
        _cleanup_session(ctx)
        await q.edit_message_text("❌ Cancelled.")
        await _send_main_menu(update, ctx)
        return ConversationHandler.END

    # ── Admin panel ───────────────────────────────────────────────────────────
    if data == "admin_panel":
        if user.id != OWNER_ID:
            await q.answer("❌ Not authorised.", show_alert=True)
            return
        await q.edit_message_text(
            f"👑 *Admin Panel — {BOT_NAME}*\n\nSelect an option:",
            parse_mode="Markdown",
            reply_markup=admin_panel_keyboard(),
        )
        return ADMIN_MENU

    if data == "admin_stats":
        await _admin_stats(update, ctx)
        return ADMIN_MENU

    if data == "admin_users":
        await _admin_users(update, ctx)
        return ADMIN_MENU

    if data == "admin_active":
        await _admin_active_users(update, ctx)
        return ADMIN_MENU

    if data == "admin_gen_keys":
        await q.edit_message_text(
            "🔑 *Generate Licence Keys*\n\n"
            "How many keys do you want to generate?\n"
            "Send a number (e.g. `5`)\n\n/cancel to abort.",
            parse_mode="Markdown",
        )
        return ADMIN_KEY_COUNT

    if data == "admin_broadcast":
        await q.edit_message_text(
            "📢 *Broadcast Message*\n\n"
            "Send me the message you want to broadcast to all users.\n\n/cancel to abort.",
            parse_mode="Markdown",
        )
        return ADMIN_BROADCAST

    if data == "admin_lookup":
        await q.edit_message_text(
            "🔍 *Lookup User*\n\n"
            "Send me the user's Telegram ID.\n\n/cancel to abort.",
            parse_mode="Markdown",
        )
        ctx.user_data["admin_action"] = "lookup"
        return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
#  Profile / Stats / Help pages
# ═══════════════════════════════════════════════════════════════════════════════

async def _show_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    row = db.get_user(user.id)
    now = time.time()

    # FIX: use UTC for expiry timestamp; handle None license_key gracefully
    if row and row["is_active"] and row["license_expires"]:
        remaining = row["license_expires"] - now
        exp_dt = datetime.fromtimestamp(
            row["license_expires"], tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        status = "✅ Active"
        time_left = format_duration(remaining) if remaining > 0 else "⚠️ Expired"
        key_val = row["license_key"] or "—"
        key_display = f"`{key_val}`"
    else:
        status = "❌ Inactive"
        time_left = "—"
        exp_dt = "—"
        key_display = "—"

    joined = datetime.fromtimestamp(row["joined_at"], tz=timezone.utc).strftime("%Y-%m-%d") if row else "—"
    username = f"@{user.username}" if user.username else "—"

    text = (
        f"👤 *My Profile*\n"
        f"{'━' * 28}\n"
        f"🆔 ID: `{user.id}`\n"
        f"📛 Name: *{user.first_name}*\n"
        f"🔗 Username: {username}\n"
        f"📅 Joined: `{joined}`\n\n"
        f"🔑 *Subscription*\n"
        f"{'━' * 28}\n"
        f"🟢 Status: {status}\n"
        f"🎫 Key: {key_display}\n"
        f"📆 Expires: `{exp_dt}`\n"
        f"⏳ Time Left: `{time_left}`\n"
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_to_menu_kb())


async def _show_user_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    row = db.get_user(user.id)

    files = row["files_sent"] if row else 0
    archives = row["archives_processed"] if row else 0

    text = (
        f"📊 *My Statistics*\n"
        f"{'━' * 28}\n"
        f"📦 Archives processed: `{archives}`\n"
        f"📄 Files received: `{files}`\n\n"
        f"⚡ Powered by *{BOT_NAME}*"
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_to_menu_kb())


async def _show_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    text = (
        f"ℹ️ *How to Use {BOT_NAME}*\n"
        f"{'━' * 28}\n\n"
        f"1️⃣ Redeem a key: `/redeem YOUR-KEY`\n"
        f"2️⃣ Open main menu: `/start`\n"
        f"3️⃣ Tap 📦 *Unzip File*\n"
        f"4️⃣ Send your archive (any format)\n"
        f"5️⃣ If password protected, I'll ask for the password\n"
        f"6️⃣ If it has folders, pick which ones you want\n"
        f"7️⃣ I'll send you all the extracted files!\n\n"
        f"⚠️ *Max file size:* `{MAX_FILE_SIZE // (1024*1024)} MB`\n"
        f"🔐 *Password archives:* supported\n"
        f"📁 *Folder selection:* supported"
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_to_menu_kb())


async def _show_formats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    fmt_list = "\n".join(f"  `{f}`" for f in SUPPORTED_FORMATS)
    text = (
        f"🗂 *Supported Formats*\n"
        f"{'━' * 28}\n\n"
        f"{fmt_list}\n\n"
        f"🔐 Password-protected archives are also supported!"
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_to_menu_kb())


# ═══════════════════════════════════════════════════════════════════════════════
#  Unzip flow — receive file
# ═══════════════════════════════════════════════════════════════════════════════

async def receive_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document

    if not doc:
        await update.message.reply_text("⚠️ Please send a document/file, not a photo or video.")
        return WAITING_FILE

    fname = doc.file_name or "archive"
    ext = detect_format(fname)
    if ext not in SUPPORTED_FORMATS:
        await update.message.reply_text(
            f"❌ Unsupported format: `{ext or 'unknown'}`\n\n"
            f"Use /formats to see what I support.",
            parse_mode="Markdown",
        )
        return WAITING_FILE

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"❌ File too large!\n"
            f"Max: `{format_size(MAX_FILE_SIZE)}`  |  Yours: `{format_size(doc.file_size)}`",
            parse_mode="Markdown",
        )
        return WAITING_FILE

    prog_msg = await update.message.reply_text(
        f"📥 *Downloading...*\n{progress_bar(0, 100)}",
        parse_mode="Markdown",
    )

    session_id = str(user.id) + "_" + str(int(time.time()))
    session_dir = os.path.join(TEMP_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    # Stamp creation time so the 30-min cleanup job knows when to delete
    (Path(session_dir) / ".created_at").write_text(str(time.time()))
    archive_path = os.path.join(session_dir, fname)

    try:
        tg_file = await doc.get_file()
        await prog_msg.edit_text(
            f"📥 *Downloading...*\n{progress_bar(33, 100)}",
            parse_mode="Markdown",
        )
        await tg_file.download_to_drive(archive_path)
        await prog_msg.edit_text(
            f"📥 *Downloading...*\n{progress_bar(100, 100)}\n✅ Download complete!",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Download error: {e}")
        shutil.rmtree(session_dir, ignore_errors=True)
        await prog_msg.edit_text("❌ Failed to download file. Please try again.")
        return WAITING_FILE

    ctx.user_data["session_id"] = session_id
    ctx.user_data["session_dir"] = session_dir
    ctx.user_data["archive_path"] = archive_path
    ctx.user_data["archive_name"] = fname

    await asyncio.sleep(0.5)

    # Check if password required
    try:
        pwd_needed = needs_password(archive_path)
    except Exception:
        pwd_needed = False

    if pwd_needed:
        await prog_msg.edit_text(
            "🔐 *This archive is password protected!*\n\n"
            "Please send me the password now.\n"
            "_Send multiple passwords separated by newlines if files inside have different passwords._",
            parse_mode="Markdown",
        )
        return WAITING_PASSWORD

    await prog_msg.edit_text("🔍 *Analysing archive...*", parse_mode="Markdown")
    # FIX: propagate the return value so folder selection state is entered correctly
    return await _process_archive(update, ctx, prog_msg)


async def receive_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pwd_text = update.message.text.strip()
    passwords = [p.encode() for p in pwd_text.splitlines() if p.strip()]
    ctx.user_data["passwords"] = passwords

    prog_msg = await update.message.reply_text(
        "🔐 *Testing password(s)...*",
        parse_mode="Markdown",
    )

    archive_path = ctx.user_data.get("archive_path")
    ext_dir = os.path.join(ctx.user_data["session_dir"], "extracted")

    success = False
    for pwd in passwords:
        try:
            ok = extract_archive(archive_path, ext_dir, password=pwd)
            if ok:
                success = True
                ctx.user_data["used_password"] = pwd
                break
        except Exception:
            shutil.rmtree(ext_dir, ignore_errors=True)

    if not success:
        # Try no password as fallback
        try:
            ok = extract_archive(archive_path, ext_dir, password=None)
            success = ok
        except Exception:
            pass

    if not success:
        await prog_msg.edit_text(
            "❌ *Wrong password!* None of the provided passwords worked.\n\n"
            "Please send the correct password, or /cancel to abort.",
            parse_mode="Markdown",
        )
        shutil.rmtree(ext_dir, ignore_errors=True)
        return WAITING_PASSWORD

    await prog_msg.edit_text("✅ *Password accepted! Analysing...*", parse_mode="Markdown")
    # FIX: propagate the return value so folder selection state is entered correctly
    return await _process_archive(update, ctx, prog_msg, already_extracted=ext_dir)


# ═══════════════════════════════════════════════════════════════════════════════
#  Archive processing & folder selection
# ═══════════════════════════════════════════════════════════════════════════════

async def _process_archive(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    prog_msg,
    already_extracted: str | None = None,
):
    archive_path = ctx.user_data["archive_path"]
    session_dir = ctx.user_data["session_dir"]
    ext_dir = already_extracted or os.path.join(session_dir, "extracted")

    if not already_extracted:
        try:
            await prog_msg.edit_text(
                f"📂 *Extracting archive...*\n{progress_bar(30, 100)}",
                parse_mode="Markdown",
            )
            pwd = ctx.user_data.get("used_password")
            ok = extract_archive(archive_path, ext_dir, password=pwd)
            if not ok:
                await prog_msg.edit_text("❌ Could not extract this archive format.")
                _cleanup_session(ctx)
                return ConversationHandler.END
        except Exception as e:
            logger.error(f"Extraction error: {traceback.format_exc()}")
            await prog_msg.edit_text(
                f"❌ *Extraction failed!*\n`{str(e)[:200]}`",
                parse_mode="Markdown",
            )
            _cleanup_session(ctx)
            return ConversationHandler.END

    await prog_msg.edit_text(
        f"🔍 *Scanning contents...*\n{progress_bar(70, 100)}",
        parse_mode="Markdown",
    )

    top_dirs, flat_files = scan_extracted(ext_dir)
    ctx.user_data["ext_dir"] = ext_dir
    ctx.user_data["top_dirs"] = top_dirs
    ctx.user_data["flat_files"] = flat_files

    total_files = len(flat_files) + sum(len(v) for v in top_dirs.values())
    await prog_msg.edit_text(
        f"✅ *Archive analysed!*\n"
        f"📄 Files: `{total_files}`  📁 Folders: `{len(top_dirs)}`",
        parse_mode="Markdown",
    )

    await asyncio.sleep(0.5)

    if top_dirs:
        # Show folder selection UI
        ctx.user_data["selected_folders"] = set(top_dirs.keys())  # all selected by default
        await _show_folder_selection(update, ctx, prog_msg)
        return SELECTING_FOLDERS
    else:
        # Send all files directly
        await prog_msg.edit_text("📤 *Sending files...*", parse_mode="Markdown")
        await _send_files(update, ctx, flat_files, prog_msg)
        return ConversationHandler.END


async def _show_folder_selection(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prog_msg=None):
    top_dirs: dict = ctx.user_data["top_dirs"]
    selected: set = ctx.user_data.get("selected_folders", set(top_dirs.keys()))
    session_id = ctx.user_data["session_id"]

    buttons = []
    for folder_name, files in top_dirs.items():
        is_sel = folder_name in selected
        icon = "✅" if is_sel else "☑️"
        safe = folder_name[:30]
        idx = list(top_dirs.keys()).index(folder_name)
        buttons.append([InlineKeyboardButton(
            f"{icon} 📁 {safe}  ({len(files)} files)",
            callback_data=f"folder_toggle:{session_id}:{idx}"
        )])

    buttons.append([
        InlineKeyboardButton("✅ Select All", callback_data=f"folder_all:{session_id}"),
        InlineKeyboardButton("☑️ None",       callback_data=f"folder_none:{session_id}"),
    ])
    buttons.append([
        InlineKeyboardButton("📤 Confirm & Send", callback_data=f"folder_confirm:{session_id}"),
        InlineKeyboardButton("❌ Cancel",          callback_data="folder_cancel"),
    ])

    flat_count = len(ctx.user_data.get("flat_files", []))
    sel_count = sum(len(top_dirs[f]) for f in selected)
    total_sel = sel_count + flat_count

    text = (
        f"📁 *Choose Folders to Extract*\n"
        f"{'━' * 28}\n"
        f"Select which folders you want.\n"
        f"✅ = selected  |  ☑️ = skipped\n\n"
        f"📄 Files in root: `{flat_count}`\n"
        f"📦 Selected: `{total_sel}` files from `{len(selected)}` folder(s)\n"
    )

    if prog_msg:
        try:
            await prog_msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            chat = update.effective_chat
            await ctx.bot.send_message(chat.id, text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        q = update.callback_query
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def _toggle_folder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":", 2)
    idx = int(parts[2])
    top_dirs: dict = ctx.user_data.get("top_dirs", {})
    folder_names = list(top_dirs.keys())
    if idx >= len(folder_names):
        return
    name = folder_names[idx]
    selected: set = ctx.user_data.get("selected_folders", set())
    if name in selected:
        selected.discard(name)
    else:
        selected.add(name)
    ctx.user_data["selected_folders"] = selected
    await _show_folder_selection(update, ctx)


async def _select_all_folders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    top_dirs: dict = ctx.user_data.get("top_dirs", {})
    ctx.user_data["selected_folders"] = set(top_dirs.keys())
    await _show_folder_selection(update, ctx)


async def _deselect_all_folders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["selected_folders"] = set()
    await _show_folder_selection(update, ctx)


async def _confirm_folders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    top_dirs: dict = ctx.user_data.get("top_dirs", {})
    selected: set = ctx.user_data.get("selected_folders", set())
    flat_files: list = ctx.user_data.get("flat_files", [])

    if not selected and not flat_files:
        await q.answer("⚠️ Select at least one folder!", show_alert=True)
        return

    files_to_send = list(flat_files)
    for name in selected:
        files_to_send.extend(top_dirs.get(name, []))

    await q.edit_message_text(
        f"📤 *Sending `{len(files_to_send)}` files...*",
        parse_mode="Markdown",
    )
    await _send_files(update, ctx, files_to_send, q.message)


async def _send_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE, file_paths: list, prog_msg):
    if not file_paths:
        await prog_msg.edit_text("⚠️ No files to send.")
        _cleanup_session(ctx)
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    top_dirs = ctx.user_data.get("top_dirs", {})
    selected = ctx.user_data.get("selected_folders", set())

    total = len(file_paths)
    sent = 0
    failed = 0

    last_folder = None

    # Group files by folder for labelled sends
    folder_map: dict[str | None, list[str]] = {}
    flat_files_set = set(ctx.user_data.get("flat_files", []))

    for fp in file_paths:
        p = Path(fp)
        ext_dir = ctx.user_data.get("ext_dir", "")
        rel = str(p.relative_to(ext_dir))
        parts = rel.split(os.sep)
        folder = parts[0] if len(parts) > 1 else None
        folder_map.setdefault(folder, []).append(fp)

    for folder, files in folder_map.items():
        if folder:
            # Send folder header
            await ctx.bot.send_message(
                chat_id,
                f"📁 *{folder}/*  — `{len(files)}` file(s)",
                parse_mode="Markdown",
            )

        for i, fp in enumerate(files, 1):
            try:
                fname = Path(fp).name
                fsize = os.path.getsize(fp)

                bar = progress_bar(sent + 1, total)
                try:
                    await prog_msg.edit_text(
                        f"📤 *Uploading files...*\n{bar}\n"
                        f"📄 `{fname}` ({format_size(fsize)})\n"
                        f"[{sent + 1}/{total}]",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

                with open(fp, "rb") as f:
                    await ctx.bot.send_document(
                        chat_id,
                        document=f,
                        filename=fname,
                        caption=f"📄 `{fname}`\n💾 {format_size(fsize)}",
                        parse_mode="Markdown",
                    )
                sent += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Failed to send {fp}: {e}")
                failed += 1

    db.increment_user_stats(user.id, files=sent, archives=1)
    db.increment_global_stats(files=sent, archives=1)

    summary = (
        f"✅ *Done!*\n"
        f"{'━' * 22}\n"
        f"📦 Archive: `{ctx.user_data.get('archive_name', '—')}`\n"
        f"📤 Sent: `{sent}` files\n"
    )
    if failed:
        summary += f"⚠️ Failed: `{failed}` files\n"

    try:
        await prog_msg.edit_text(summary, parse_mode="Markdown", reply_markup=back_to_menu_kb())
    except Exception:
        await ctx.bot.send_message(chat_id, summary, parse_mode="Markdown", reply_markup=back_to_menu_kb())

    _cleanup_session(ctx)


def _cleanup_session(ctx: ContextTypes.DEFAULT_TYPE):
    session_dir = ctx.user_data.get("session_dir")
    if session_dir:
        shutil.rmtree(session_dir, ignore_errors=True)
    ctx.user_data.clear()


# ═══════════════════════════════════════════════════════════════════════════════
#  Admin panel handlers
# ═══════════════════════════════════════════════════════════════════════════════

async def _admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.edit_message_text("📊 *Fetching stats...* ⏳", parse_mode="Markdown")

    stats       = db.get_global_stats()
    all_users   = db.get_all_users()
    active_users = db.get_active_users()

    # Disk space
    disk = get_disk_info()
    if disk:
        disk_bar_filled = int(18 * disk["pct"] / 100)
        disk_bar = "█" * disk_bar_filled + "░" * (18 - disk_bar_filled)
        disk_line = (
            f"  [{disk_bar}] {disk['pct']}%\n"
            f"  Free: `{format_size(disk['free'])}`  /  Total: `{format_size(disk['total'])}`"
        )
    else:
        disk_line = "  N/A"

    # Internet speed (async, runs concurrently)
    speed = await measure_download_speed()

    # Active sessions on disk
    session_count = 0
    if os.path.exists(TEMP_DIR):
        session_count = sum(1 for e in Path(TEMP_DIR).iterdir() if e.is_dir())

    text = (
        f"📊 *Bot Statistics*\n"
        f"{'━' * 28}\n"
        f"⏱ Uptime: `{uptime_str()}`\n"
        f"🤖 Version: `{BOT_VERSION}`\n\n"
        f"👥 *Users*\n"
        f"  Total: `{len(all_users)}`\n"
        f"  Active licensed: `{len(active_users)}`\n\n"
        f"📦 *Activity*\n"
        f"  Archives processed: `{stats['total_archives_done']}`\n"
        f"  Files sent: `{stats['total_files_sent']}`\n"
        f"  Keys generated: `{stats['total_keys_generated']}`\n"
        f"  Open sessions: `{session_count}`\n\n"
        f"💾 *Disk Space*\n"
        f"{disk_line}\n\n"
        f"🌐 *Internet Speed*\n"
        f"  Download: `{speed}`\n"
    )
    await q.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")]])
    )


async def _admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    users = db.get_all_users()
    now = time.time()
    lines = []
    for u in users[:25]:
        active = "✅" if u["is_active"] and (not u["license_expires"] or u["license_expires"] > now) else "❌"
        name = u["first_name"] or "—"
        uname = f"@{u['username']}" if u["username"] else f"ID:{u['user_id']}"
        lines.append(f"{active} {name} ({uname})")

    text = (
        f"👥 *All Users* ({len(users)} total)\n"
        f"{'━' * 28}\n\n"
        + "\n".join(lines or ["No users yet."])
        + ("\n\n_Showing first 25_" if len(users) > 25 else "")
    )
    await q.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")]])
    )


async def _admin_active_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    users = db.get_active_users()
    now = time.time()
    lines = []
    for u in users:
        remaining = format_duration(u["license_expires"] - now) if u["license_expires"] else "—"
        name = u["first_name"] or "—"
        lines.append(f"👤 *{name}* — `{remaining}` left")

    text = (
        f"⏫ *Active Users* ({len(users)})\n"
        f"{'━' * 28}\n\n"
        + "\n".join(lines or ["No active users."])
    )
    await q.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")]])
    )


async def admin_receive_key_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit() or int(txt) < 1:
        await update.message.reply_text("⚠️ Send a valid number (e.g. `5`).", parse_mode="Markdown")
        return ADMIN_KEY_COUNT
    ctx.user_data["key_count"] = int(txt)
    await update.message.reply_text(
        "⏳ *Set the duration for these keys:*\n\n"
        "`1D` = 1 Day\n"
        "`7D` = 7 Days\n"
        "`30D` = 30 Days\n"
        "`1W` = 1 Week\n"
        "`1MO` = 1 Month\n"
        "`1H` = 1 Hour\n"
        "`30M` = 30 Minutes\n\n"
        "Send the duration now:",
        parse_mode="Markdown",
    )
    return ADMIN_KEY_DURATION


async def admin_receive_key_duration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    dur_sec, label = db.parse_duration(txt)
    if dur_sec is None:
        await update.message.reply_text(
            "❌ Invalid format. Try: `7D`, `30D`, `1W`, `1MO`, `1H`, `30M`",
            parse_mode="Markdown",
        )
        return ADMIN_KEY_DURATION

    count = ctx.user_data.get("key_count", 1)
    keys = db.generate_keys(count, dur_sec, label)

    keys_text = "\n".join(f"`{k}`" for k in keys)
    text = (
        f"🔑 *{count} Key(s) Generated!*\n"
        f"⏳ Duration: `{label}`\n"
        f"{'━' * 28}\n\n"
        f"{keys_text}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    await update.message.reply_text(
        f"👑 *Admin Panel — {BOT_NAME}*\n\nSelect an option:",
        parse_mode="Markdown",
        reply_markup=admin_panel_keyboard(),
    )
    ctx.user_data.pop("key_count", None)
    return ADMIN_MENU


async def admin_receive_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    users = db.get_all_users()
    success = 0
    failed = 0

    status = await msg.reply_text(f"📢 Broadcasting to {len(users)} users...")

    for u in users:
        try:
            await ctx.bot.forward_message(
                chat_id=u["user_id"],
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status.edit_text(
        f"📢 *Broadcast complete!*\n"
        f"✅ Sent: `{success}`\n"
        f"❌ Failed: `{failed}`",
        parse_mode="Markdown",
    )
    await msg.reply_text(
        f"👑 *Admin Panel — {BOT_NAME}*\n\nSelect an option:",
        parse_mode="Markdown",
        reply_markup=admin_panel_keyboard(),
    )
    return ADMIN_MENU


async def admin_text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    action = ctx.user_data.get("admin_action")
    if action == "lookup":
        txt = update.message.text.strip()
        if not txt.lstrip("-").isdigit():
            await update.message.reply_text("⚠️ Send a valid Telegram user ID (numbers only).")
            return ADMIN_MENU
        uid = int(txt)
        row = db.get_user(uid)
        if not row:
            await update.message.reply_text(f"❌ User `{uid}` not found in database.", parse_mode="Markdown")
        else:
            now = time.time()
            active = row["is_active"] and (not row["license_expires"] or row["license_expires"] > now)
            remaining = format_duration(row["license_expires"] - now) if row["license_expires"] and active else "—"
            text = (
                f"🔍 *User Lookup*\n"
                f"{'━' * 24}\n"
                f"🆔 ID: `{row['user_id']}`\n"
                f"📛 Name: {row['first_name']}\n"
                f"🔗 Username: @{row['username'] or '—'}\n"
                f"🟢 Active: {'Yes' if active else 'No'}\n"
                f"🔑 Key: `{row['license_key'] or '—'}`\n"
                f"⏳ Left: `{remaining}`\n"
                f"📦 Archives: `{row['archives_processed']}`\n"
                f"📄 Files received: `{row['files_sent']}`\n"
            )
            await update.message.reply_text(text, parse_mode="Markdown")
        ctx.user_data.pop("admin_action", None)
        await update.message.reply_text(
            f"👑 *Admin Panel*",
            parse_mode="Markdown",
            reply_markup=admin_panel_keyboard(),
        )
    return ADMIN_MENU


# ═══════════════════════════════════════════════════════════════════════════════
#  Error handler
# ═══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {ctx.error}", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ An unexpected error occurred. Please try again or use /cancel."
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
#  Application setup
# ═══════════════════════════════════════════════════════════════════════════════

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    unzip_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_handler, pattern="^menu_unzip$")],
        states={
            WAITING_FILE: [
                MessageHandler(filters.Document.ALL, receive_file),
            ],
            WAITING_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password),
            ],
            SELECTING_FOLDERS: [
                CallbackQueryHandler(callback_handler, pattern="^folder_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )

    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", cmd_admin),
            CallbackQueryHandler(callback_handler, pattern="^admin_panel$"),
        ],
        states={
            ADMIN_MENU: [
                CallbackQueryHandler(callback_handler, pattern="^admin_"),
                CallbackQueryHandler(callback_handler, pattern="^back_main$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_handler),
            ],
            ADMIN_KEY_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_key_count),
            ],
            ADMIN_KEY_DURATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_key_duration),
            ],
            ADMIN_BROADCAST: [
                MessageHandler(~filters.COMMAND, admin_receive_broadcast),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(admin_conv)
    app.add_handler(unzip_conv)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    return app


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "Open the bot / main menu"),
        BotCommand("redeem", "Redeem a licence key"),
        BotCommand("admin",  "Admin panel (owner only)"),
        BotCommand("cancel", "Cancel current operation"),
    ])
    # Run session cleanup every 5 minutes; first run after 60 seconds
    app.job_queue.run_repeating(cleanup_old_sessions, interval=300, first=60)
    logger.info(f"✅ {BOT_NAME} v{BOT_VERSION} started — session cleanup job registered")


def main():
    db.init_db()
    keep_alive()

    app = build_app()
    app.post_init = post_init

    logger.info(f"🚀 Starting {BOT_NAME}...")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
