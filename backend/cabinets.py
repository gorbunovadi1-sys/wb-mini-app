import datetime
import json

from .crypto import decrypt, encrypt
from .db import SessionLocal
from .models import Cabinet, CostPrice, User


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
        }


def deactivate_cabinet(cabinet_id: int, user_id: int) -> bool:
    with SessionLocal() as session:
        cabinet = session.query(Cabinet).filter_by(id=cabinet_id, user_id=user_id).first()
        if not cabinet:
            return False
        cabinet.is_active = False
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
