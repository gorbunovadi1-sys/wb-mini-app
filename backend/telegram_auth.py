import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
AI_ENGINE_BOT_TOKEN = os.environ.get("AI_ENGINE_BOT_TOKEN", "")


def validate_init_data(init_data: str, bot_token: str = None, max_age_seconds: int = 86400) -> bool:
    """Validates Telegram WebApp initData per Telegram's documented HMAC scheme.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    The HMAC secret is derived from whichever bot owns the WebApp that produced
    this initData — pass `bot_token` explicitly when it isn't the original WB bot."""
    bot_token = bot_token if bot_token is not None else BOT_TOKEN
    if not bot_token or not init_data:
        return False
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return False
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return False

    auth_date = pairs.get("auth_date")
    if auth_date and (time.time() - int(auth_date)) > max_age_seconds:
        return False

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed_hash, received_hash)


def parse_init_data_user(init_data: str) -> dict:
    """Extracts the `user` object (id, first_name, username, ...) Telegram embeds
    in initData. Call only after validate_init_data() has confirmed the signature."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
        return json.loads(pairs.get("user", "{}"))
    except (ValueError, json.JSONDecodeError):
        return {}
