"""Single-tenant config, read from env vars — no multi-cabinet DB layer, this
bot only ever serves Сельдереева. All SELD_-prefixed to keep it obviously
separate from the multi-tenant app's own env vars (and from kim_bot's KIM_
ones) if they all end up in the same Railway project."""
import os

BOT_TOKEN = os.environ.get("SELD_BOT_TOKEN")
PERSONAL_CHAT_ID = os.environ.get("SELD_PERSONAL_CHAT_ID")
MANAGERS_CHAT_ID = os.environ.get("SELD_MANAGERS_CHAT_ID")

WB_API_KEY = os.environ.get("SELD_WB_API_KEY")
OZON_CLIENT_ID = os.environ.get("SELD_OZON_CLIENT_ID")
OZON_API_KEY = os.environ.get("SELD_OZON_API_KEY")

# Own turnover-based tax rate (УСН "доходы" etc.), charged on revenue — same
# meaning as ozon_margin.build_margin_summary's tax_pct parameter.
TAX_PCT = float(os.environ.get("SELD_TAX_PCT", "0"))

# Mini app URL (points at the /seldereeva static mount inside the existing
# ai_engine_app.py web process — see backend/seldereeva_routes.py).
MINI_APP_URL = os.environ.get("SELD_MINI_APP_URL")

# Доступ закрыт по умолчанию: бот отвечает только из личного чата (и чата
# менеджеров) и явно перечисленных здесь чатов/групп (например «Seldereeva
# WB») — запятая-разделённый список id. /chatid работает всегда, вне
# зависимости от списка — иначе неоткуда узнать id новой группы, чтобы
# её сюда добавить.
_ALLOWED_CHAT_IDS_RAW = os.environ.get("SELD_ALLOWED_CHAT_IDS", "")


def allowed_chat_ids() -> set:
    ids = set()
    for part in _ALLOWED_CHAT_IDS_RAW.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                pass
    for c in (PERSONAL_CHAT_ID, MANAGERS_CHAT_ID):
        if c:
            try:
                ids.add(int(c))
            except ValueError:
                pass
    return ids


def notify_chat_ids() -> list[str]:
    return [c for c in (PERSONAL_CHAT_ID, MANAGERS_CHAT_ID) if c]
