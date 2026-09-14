# AI assistance: parts of this file were written with Claude (Anthropic).
# Architecture, review and debugging are my own. See README.md, "AI use".

"""Every environment variable the project reads, in one place.

Read once at import. Nothing here is secret enough to hide and nothing is
validated: an unset Telegram token simply means alerts are logged instead of
sent, which is what running the project without a bot should do.
"""

import os
from pathlib import Path

DB_PATH = Path(os.environ.get("MONITOR_DB", "monitor.db"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
