import os

OWNER_ID = 8731647972
BOT_NAME = "UnZipper Pro"
BOT_VERSION = "2.0.0"

MAX_FILE_SIZE = 500 * 1024 * 1024  # 50MB
TEMP_DIR = "/tmp/unzipper_sessions"

SUPPORTED_FORMATS = [
    ".zip", ".7z", ".tar", ".tar.gz", ".tar.bz2",
    ".tar.xz", ".tgz", ".tbz2", ".txz", ".rar",
    ".gz", ".bz2", ".xz"
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

FLASK_PORT = int(os.environ.get("PORT", 8080))

BOT_EMOJI = "⚡"
LOCK_EMOJI = "🔒"
UNLOCK_EMOJI = "🔓"
FOLDER_EMOJI = "📁"
FILE_EMOJI = "📄"
CROWN_EMOJI = "👑"
STAR_EMOJI = "⭐"
FIRE_EMOJI = "🔥"
