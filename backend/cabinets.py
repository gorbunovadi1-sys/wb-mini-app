import datetime
import json

from .crypto import decrypt, encrypt
from .db import SessionLocal
from .models import Cabinet, CostPrice, User


class AccessDenied(Exception):
    """Raised when a Telegram user is blocked or their timed access expired."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def get_or_create_user(telegram_user_id: int, first_name: str = None, username: str = None) -> int:
    with SessionLocal() as session:
        user = session.query(User).filter_by(telegram_user_id=telegram_user_id).first()
        if user:
            return user.id
        user = User(telegram_user_id=telegram_user_id, first_name=first_name, username=username)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def add_cabinet(user_id: int, marketplace: str, credentials: dict, display_name: str = None) -> int:
    encrypted = encrypt(json.dumps(credentials))
    with SessionLocal() as session:
        cabinet = Cabinet(
            user_id=user_id, marketplace=marketplace,
            display_name=display_name, encrypted_credentials=encrypted,
        )
        session.add(cabinet)
        session.commit()
        session.refresh(cabinet)
        return cabinet.id


def list_cabinets(user_id: int) -> list:
    with SessionLocal() as session:
        cabinets = session.query(Cabinet).filter_by(user_id=user_id, is_active=True).all()
        return [
            {
                "id": c.id, "marketplace": c.marketplace, "display_name": c.display_name,
                "is_active": c.is_active, "last_synced_at": c.last_synced_at,
                "last_error": c.last_error,
            }
            for c in cabinets
        ]


def get_credentials(cabinet_id: int) -> dict:
    with SessionLocal() as session:
        cabinet = session.get(Cabinet, cabinet_id)
        if not cabinet:
            raise ValueError(f"cabinet {cabinet_id} not found")
        return json.loads(decrypt(cabinet.encrypted_credentials))


def get_cabinet(cabinet_id: int) -> dict:
    """Returns marketplace/ownership/credentials in one call — used by the API
    layer to check ownership before building a client from the stored keys."""
    with SessionLocal() as session:
        cabinet = session.get(Cabinet, cabinet_id)
        if not cabinet or not cabinet.is_active:
            return None
        return {
            "id": cabinet.id,
            "user_id": cabinet.user_id,
            "marketplace": cabinet.marketplace,
            "display_name": cabinet.display_name,
            "credentials": json.loads(decrypt(cabinet.encrypted_credentials)),
            "settings": cabinet.settings or {},
        }


def get_cabinet_settings(cabinet_id: int) -> dict:
    with SessionLocal() as session:
        cabinet = session.get(Cabinet, cabinet_id)
        return (cabinet.settings or {}) if cabinet else {}


def update_cabinet_settings(cabinet_id: int, patch: dict) -> dict:
    """Merges `patch` into the cabinet's settings JSON — per-cabinet rules
    like promo_auto_remove, margin floor, etc."""
    with SessionLocal() as session:
        cabinet = session.get(Cabinet, cabinet_id)
        if not cabinet:
            raise ValueError(f"cabinet {cabinet_id} not found")
        settings = dict(cabinet.settings or {})
        settings.update(patch)
        cabinet.settings = settings
        session.commit()
        return settings


def deactivate_cabinet(cabinet_id: int, user_id: int) -> bool:
    with SessionLocal() as session:
        cabinet = session.query(Cabinet).filter_by(id=cabinet_id, user_id=user_id).first()
        if not cabinet:
            return False
        cabinet.is_active = False
        session.commit()
        return True


def list_all_active_cabinets(marketplace: str = None) -> list:
    """Every active cabinet belonging to a user with current access (not
    blocked, not expired), with decrypted credentials and the owner's
    Telegram id — used by background jobs (e.g. price monitoring), not
    exposed via any user-scoped API route."""
    now = datetime.datetime.utcnow()
    with SessionLocal() as session:
        query = (
            session.query(Cabinet, User)
            .join(User, Cabinet.user_id == User.id)
            .filter(Cabinet.is_active.is_(True))
            .filter(User.is_blocked.is_(False))
            .filter((User.access_until.is_(None)) | (User.access_until >= now))
        )
        if marketplace:
            query = query.filter(Cabinet.marketplace == marketplace)
        rows = query.all()
        return [
            {
                "id": c.id, "user_id": c.user_id, "telegram_user_id": u.telegram_user_id,
                "marketplace": c.marketplace, "display_name": c.display_name,
                "credentials": json.loads(decrypt(c.encrypted_credentials)),
                "settings": c.settings or {},
            }
            for c, u in rows
        ]


def get_admin_stats() -> dict:
    """Every user with their connected cabinets (marketplace + display name,
    no credentials) — for the bot's /admin command, not exposed via API."""
    with SessionLocal() as session:
        users = session.query(User).order_by(User.created_at).all()
        result = []
        for u in users:
            cabs = session.query(Cabinet).filter_by(user_id=u.id, is_active=True).all()
            result.append({
                "telegram_user_id": u.telegram_user_id,
                "first_name": u.first_name,
                "username": u.username,
                "created_at": u.created_at,
                "is_blocked": u.is_blocked,
                "access_until": u.access_until,
                "cabinets": [
                    {"marketplace": c.marketplace, "display_name": c.display_name, "last_synced_at": c.last_synced_at}
                    for c in cabs
                ],
            })
        return {"total_users": len(users), "users": result}


def check_access(telegram_user_id: int):
    """Raises AccessDenied if this user is blocked or their timed access has
    expired. No matching row (new user) or access_until=None both mean
    unrestricted — access is opt-out (block/limit), not opt-in."""
    with SessionLocal() as session:
        user = session.query(User).filter_by(telegram_user_id=telegram_user_id).first()
        if not user:
            return
        if user.is_blocked:
            raise AccessDenied("blocked")
        if user.access_until and user.access_until < datetime.datetime.utcnow():
            raise AccessDenied("expired")


def set_blocked(telegram_user_id: int, blocked: bool) -> bool:
    with SessionLocal() as session:
        user = session.query(User).filter_by(telegram_user_id=telegram_user_id).first()
        if not user:
            return False
        user.is_blocked = blocked
        session.commit()
        return True


def grant_access_days(telegram_user_id: int, days: int) -> bool:
    """Sets access_until to `days` from now (extends from an already-future
    expiry rather than from now, so repeated top-ups stack)."""
    with SessionLocal() as session:
        user = session.query(User).filter_by(telegram_user_id=telegram_user_id).first()
        if not user:
            return False
        now = datetime.datetime.utcnow()
        base = user.access_until if (user.access_until and user.access_until > now) else now
        user.access_until = base + datetime.timedelta(days=days)
        user.is_blocked = False
        session.commit()
        return True


def get_cost_prices(cabinet_id: int) -> dict:
    """offer_id (Ozon) / str(nm_id) (WB) -> cost price in RUB."""
    with SessionLocal() as session:
        rows = session.query(CostPrice).filter_by(cabinet_id=cabinet_id).all()
        return {r.item_key: r.cost_price for r in rows}


def set_cost_prices(cabinet_id: int, prices: dict):
    """Upserts a batch of {item_key: cost_price} for one cabinet."""
    with SessionLocal() as session:
        existing = {
            r.item_key: r for r in session.query(CostPrice).filter_by(cabinet_id=cabinet_id).all()
        }
        for item_key, price in prices.items():
            item_key = str(item_key)
            row = existing.get(item_key)
            if row:
                row.cost_price = price
                row.updated_at = datetime.datetime.utcnow()
            else:
                session.add(CostPrice(cabinet_id=cabinet_id, item_key=item_key, cost_price=price))
        session.commit()
