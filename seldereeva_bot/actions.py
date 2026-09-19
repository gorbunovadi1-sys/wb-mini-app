"""Единая точка исполнения для управляющих действий над рекламой — recommend
+ confirm, никогда не auto-execute (см. project memory: feedback_ai_action_
confirmation). Два входа (мини-апп, чат) создают pending-запись через
create_pending_action и вызывают confirm_and_execute с одним и тем же
action_id после явного подтверждения пользователем."""
import json
import logging
from typing import Optional

from backend import wb_ads
from backend.wb_client import WBClient

from . import config
from .db import SessionLocal
from .models import SeldActionLog

log = logging.getLogger("seldereeva_bot.actions")


def _wb_client():
    if not config.WB_API_KEY:
        raise RuntimeError("SELD_WB_API_KEY не настроен")
    return WBClient(config.WB_API_KEY)


def create_pending_action(action_type: str, target: str, payload: dict, requested_by: str, marketplace: str = "wb") -> int:
    with SessionLocal() as db:
        row = SeldActionLog(
            action_type=action_type, marketplace=marketplace, target=target,
            payload=json.dumps(payload, ensure_ascii=False), status="pending", requested_by=requested_by,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def get_action(action_id: int) -> Optional[SeldActionLog]:
    with SessionLocal() as db:
        return db.query(SeldActionLog).filter_by(id=action_id).first()


def cancel_action(action_id: int):
    with SessionLocal() as db:
        row = db.query(SeldActionLog).filter_by(id=action_id).first()
        if row and row.status == "pending":
            row.status = "cancelled"
            db.commit()


def confirm_and_execute(action_id: int) -> str:
    """Returns a human-readable result string; also persists it on the row.
    Never raises to the caller — a failure is a normal outcome here (a bad
    campaign id, an expired WB key), not a bug, so it's reported the same
    way a success is."""
    import datetime

    with SessionLocal() as db:
        row = db.query(SeldActionLog).filter_by(id=action_id).first()
        if not row:
            return "Действие не найдено."
        if row.status != "pending":
            return f"Действие уже в статусе «{row.status}»."
        row.status = "confirmed"
        row.confirmed_at = datetime.datetime.utcnow()
        db.commit()

        payload = json.loads(row.payload) if row.payload else {}
        try:
            result = _execute(row.action_type, payload)
            row.status = "executed"
            row.result = result
        except Exception as e:
            log.exception(f"Action {action_id} ({row.action_type}) failed")
            row.status = "failed"
            row.result = str(e)
        row.executed_at = datetime.datetime.utcnow()
        db.commit()
        return row.result


def _execute(action_type: str, payload: dict) -> str:
    if action_type == "exclude_phrase":
        return _execute_exclude_phrase(payload)
    if action_type == "pause_campaign":
        return _execute_pause_campaign(payload)
    if action_type == "set_bid":
        return _execute_set_bid(payload)
    raise ValueError(f"unknown action_type {action_type}")


def _execute_exclude_phrase(payload: dict) -> str:
    client = _wb_client()
    advert_id, norm_query = payload["advert_id"], payload["norm_query"]
    res = wb_ads.exclude_cluster_from_campaign(client, advert_id, norm_query)
    if res.get("failed"):
        return f"Частично: обновлено {len(res['updated'])}, не удалось {len(res['failed'])}"
    return f"Фраза «{norm_query}» исключена из кампании {advert_id} ({len(res['updated'])} товаров)"


def _execute_pause_campaign(payload: dict) -> str:
    # backend/wb_client.py не содержит методов паузы/возобновления кампании —
    # прежде чем писать их, нужно сверить актуальные пути WB Advert API
    # (различаются по типу кампании, auto/search) с официальной документацией
    # на момент реализации — см. план, открытый вопрос №4. Намеренно не
    # угадываю путь здесь.
    raise NotImplementedError(
        "Пауза кампании ещё не реализована — нужно сверить актуальный WB Advert API "
        "(см. открытый вопрос в плане) перед тем, как писать вызов."
    )


def _execute_set_bid(payload: dict) -> str:
    raise NotImplementedError(
        "Изменение ставки ещё не реализовано — нужно сверить актуальный WB Advert API "
        "(см. открытый вопрос в плане) перед тем, как писать вызов."
    )
