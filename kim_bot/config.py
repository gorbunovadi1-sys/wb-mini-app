"""Single-tenant config, read from env vars — no multi-cabinet DB layer, this
bot only ever serves ИП Ким. All KIM_-prefixed to keep it obviously separate
from the multi-tenant app's own env vars if both ever end up in the same
Railway project."""
import datetime
import os


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set — see kim_bot/.env.example")
    return v


BOT_TOKEN = os.environ.get("KIM_BOT_TOKEN")
PERSONAL_CHAT_ID = os.environ.get("KIM_PERSONAL_CHAT_ID")
MANAGERS_CHAT_ID = os.environ.get("KIM_MANAGERS_CHAT_ID")
FULFILLMENT_CHAT_ID = os.environ.get("KIM_FULFILLMENT_CHAT_ID")

WB_API_KEY = os.environ.get("KIM_WB_API_KEY")
OZON_CLIENT_ID = os.environ.get("KIM_OZON_CLIENT_ID")
OZON_API_KEY = os.environ.get("KIM_OZON_API_KEY")

# Hours after order creation before an unassembled FBS order is considered late.
SLA_HOURS = float(os.environ.get("KIM_SLA_HOURS", "8"))
# Minimum gap between repeat alerts for the same still-unassembled order.
ALERT_COOLDOWN_MINUTES = float(os.environ.get("KIM_ALERT_COOLDOWN_MINUTES", "60"))

# Telegram user ids allowed to interact with the bot at all — anyone else's
# messages/commands are refused. Comma-separated in KIM_ALLOWED_USER_IDS;
# always includes PERSONAL_CHAT_ID (her own Telegram user id) so a bare
# KIM_PERSONAL_CHAT_ID setup isn't accidentally left wide open.
_extra_allowed = [u.strip() for u in os.environ.get("KIM_ALLOWED_USER_IDS", "").split(",") if u.strip()]
ALLOWED_USER_IDS = {int(u) for u in ([PERSONAL_CHAT_ID] if PERSONAL_CHAT_ID else []) + _extra_allowed}

# Group chats where anyone already in the group (she controls membership)
# can use the bot, not just individually-allowlisted users — e.g. the
# managers group, so nobody has to send /chatid one by one to get added.
ALLOWED_CHAT_IDS = {int(c) for c in (MANAGERS_CHAT_ID,) if c}


def notify_chat_ids() -> list[str]:
    return [c for c in (PERSONAL_CHAT_ID, MANAGERS_CHAT_ID, FULFILLMENT_CHAT_ID) if c]


# No SLA alerts during these hours (Moscow time) — nobody's there to act on
# one at 2am, and it would just re-fire every hour until morning anyway.
QUIET_HOURS_START_MSK = int(os.environ.get("KIM_QUIET_HOURS_START_MSK", "22"))
QUIET_HOURS_END_MSK = int(os.environ.get("KIM_QUIET_HOURS_END_MSK", "8"))


def is_quiet_hours_now() -> bool:
    msk_hour = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).hour
    start, end = QUIET_HOURS_START_MSK, QUIET_HOURS_END_MSK
    if start == end:
        return False
    if start < end:
        return start <= msk_hour < end
    return msk_hour >= start or msk_hour < end  # window wraps past midnight
