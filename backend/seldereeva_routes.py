"""Мини-апп API для бренда Сельдереева — однотенантный, живёт в том же
web-процессе, что и многоарендный ИИ Движок (см. план: избегает второго
Railway-сервиса под фронтенд), но полностью не завязан на модель Cabinet —
один WB-клиент и один Ozon-клиент из SELD_-переменных окружения, как у
seldereeva_bot. Подключается в ai_engine_app.py как APIRouter."""
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from seldereeva_bot import actions, config as seld_config, cost_prices as seld_cost_prices, digests as seld_digests
from . import margin, ozon_margin, wb_ads
from .ozon_client import OzonClient
from .telegram_auth import validate_init_data
from .wb_client import WBClient

router = APIRouter(prefix="/api/seldereeva", tags=["seldereeva"])

DEV_MODE = os.environ.get("DEV_MODE", "1") == "1"  # same flag ai_engine_app.py uses


def _check_auth(x_telegram_init_data: Optional[str]):
    if DEV_MODE:
        return
    if not x_telegram_init_data or not validate_init_data(x_telegram_init_data, bot_token=seld_config.BOT_TOKEN):
        raise HTTPException(status_code=401, detail="invalid Telegram init data")


def _wb_client() -> WBClient:
    if not seld_config.WB_API_KEY:
        raise HTTPException(status_code=400, detail="SELD_WB_API_KEY не настроен")
    return WBClient(seld_config.WB_API_KEY)


def _ozon_client() -> OzonClient:
    if not (seld_config.OZON_CLIENT_ID and seld_config.OZON_API_KEY):
        raise HTTPException(status_code=400, detail="SELD_OZON_CLIENT_ID/SELD_OZON_API_KEY не настроены")
    return OzonClient(seld_config.OZON_CLIENT_ID, seld_config.OZON_API_KEY, max_retries=1)


@router.get("/finance")
def get_finance(marketplace: str = "wb", days: int = 30, x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    days = max(7, min(days, 90))
    if marketplace == "wb":
        return margin.build_margin_summary(client=_wb_client(), cost_prices=seld_cost_prices.load_cost_prices("wb"), days=days)
    if marketplace == "ozon":
        return ozon_margin.build_margin_summary(
            client=_ozon_client(), cost_prices=seld_cost_prices.load_cost_prices("ozon"), days=days, tax_pct=seld_config.TAX_PCT,
        )
    raise HTTPException(status_code=400, detail="marketplace must be wb or ozon")


@router.post("/cost-prices")
def set_cost_price(body: dict, x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    marketplace, item_key, cost_price = body.get("marketplace"), body.get("item_key"), body.get("cost_price")
    if marketplace not in ("wb", "ozon") or not item_key or cost_price is None:
        raise HTTPException(status_code=400, detail="marketplace, item_key, cost_price are required")
    seld_cost_prices.set_cost_price(marketplace, str(item_key), float(cost_price))
    return {"ok": True}


@router.get("/ads")
def get_ads(days: int = 30, x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    days = max(7, min(days, 90))
    return wb_ads.get_campaigns_summary(_wb_client(), days=days)


@router.get("/ads/{advert_id}/clusters")
def get_ad_clusters(advert_id: int, days: int = 30, x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    days = max(7, min(days, 90))
    clusters = wb_ads.get_campaign_clusters(_wb_client(), advert_id, days=days)
    return {"clusters": clusters, "summary": wb_ads.cluster_summary(clusters)}


@router.post("/ads/{advert_id}/exclude-phrase")
def request_exclude_phrase(advert_id: int, body: dict, x_telegram_init_data: Optional[str] = Header(default=None)):
    """Не исполняет сразу — создаёт pending-действие, требующее подтверждения
    (см. actions.py). Фронтенд должен показать диалог подтверждения и вызвать
    /actions/{id}/confirm."""
    _check_auth(x_telegram_init_data)
    norm_query = (body or {}).get("norm_query")
    if not norm_query:
        raise HTTPException(status_code=400, detail="norm_query is required")
    action_id = actions.create_pending_action(
        "exclude_phrase", f"campaign {advert_id}", {"advert_id": advert_id, "norm_query": norm_query}, "miniapp",
    )
    return {"action_id": action_id, "status": "pending"}


@router.post("/actions/{action_id}/confirm")
def confirm_action(action_id: int, x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    result = actions.confirm_and_execute(action_id)
    row = actions.get_action(action_id)
    return {"action_id": action_id, "status": row.status if row else "unknown", "result": result}


@router.post("/actions/{action_id}/cancel")
def cancel_action_route(action_id: int, x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    actions.cancel_action(action_id)
    return {"action_id": action_id, "status": "cancelled"}


@router.get("/digests")
def get_digests(x_telegram_init_data: Optional[str] = Header(default=None)):
    """Те же тексты, что уходят в чат по расписанию — для экрана «Сводка» в
    мини-аппе, по запросу."""
    _check_auth(x_telegram_init_data)
    return {
        "finance": seld_digests.finance_digest_text(),
        "ads": seld_digests.ads_digest_text(),
        "stock": seld_digests.stock_digest_text(),
    }
