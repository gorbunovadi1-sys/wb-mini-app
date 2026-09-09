import logging
import os
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import cabinets
from . import margin
from . import ozon_dimensions
from . import ozon_margin
from . import ozon_prices
from . import ozon_promotions
from .db import init_db
from .ozon_client import OzonClient
from .telegram_auth import parse_init_data_user, validate_init_data
from .wb_client import WBClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai_engine_app")

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend_engine")
DEV_MODE = os.environ.get("DEV_MODE", "1") == "1"  # skips Telegram signature check when set

app = FastAPI(title="ИИ Движок API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def on_startup():
    init_db()

    ai_engine_token = os.environ.get("AI_ENGINE_BOT_TOKEN")
    if ai_engine_token:
        import asyncio
        from .ai_engine_bot import build_bot, build_dispatcher
        bot = build_bot(ai_engine_token)
        dp = build_dispatcher(os.environ.get("AI_ENGINE_MINI_APP_URL"))
        asyncio.create_task(dp.start_polling(bot))
        log.info("AI Engine bot polling started")
    else:
        log.info("AI_ENGINE_BOT_TOKEN not set — bot polling not started")


def _resolve_user_id(x_telegram_init_data: Optional[str], telegram_id: Optional[int]) -> int:
    """Resolves the caller's internal user_id. In production, trusts Telegram's
    signed initData. In DEV_MODE, accepts a ?telegram_id= query param instead,
    since there's no real Telegram context when testing the API directly."""
    if DEV_MODE:
        if telegram_id is None:
            raise HTTPException(status_code=400, detail="DEV_MODE: pass ?telegram_id=<your Telegram id>")
        return cabinets.get_or_create_user(telegram_id)

    ai_bot_token = os.environ.get("AI_ENGINE_BOT_TOKEN")
    if not x_telegram_init_data or not validate_init_data(x_telegram_init_data, bot_token=ai_bot_token):
        raise HTTPException(status_code=401, detail="invalid Telegram init data")
    tg_user = parse_init_data_user(x_telegram_init_data)
    if not tg_user.get("id"):
        raise HTTPException(status_code=401, detail="no user in init data")
    return cabinets.get_or_create_user(tg_user["id"], tg_user.get("first_name"), tg_user.get("username"))


def _build_client(cabinet: dict):
    creds = cabinet["credentials"]
    if cabinet["marketplace"] == "wb":
        return WBClient(creds["api_key"])
    if cabinet["marketplace"] == "ozon":
        return OzonClient(creds["client_id"], creds["api_key"])
    raise ValueError(f"unknown marketplace {cabinet['marketplace']}")


def _owned_cabinet_or_404(cabinet_id: int, user_id: int) -> dict:
    cabinet = cabinets.get_cabinet(cabinet_id)
    if not cabinet or cabinet["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="cabinet not found")
    return cabinet


@app.get("/api/me/cabinets")
def get_my_cabinets(x_telegram_init_data: Optional[str] = Header(default=None), telegram_id: Optional[int] = None):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    return {"cabinets": cabinets.list_cabinets(user_id)}


@app.get("/api/cabinets/{cabinet_id}/margin")
def get_cabinet_margin(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    client = _build_client(cabinet)
    try:
        # cost_prices={} until per-cabinet cost prices are wired up — profit
        # will show as 0 COGS (flagged "без себестоимости") until then.
        if cabinet["marketplace"] == "wb":
            return margin.build_margin_summary(client=client, cost_prices={})
        return ozon_margin.build_margin_summary(client=client, cost_prices={})
    except Exception as e:
        log.exception(f"Failed to build margin for cabinet {cabinet_id}")
        raise HTTPException(status_code=502, detail=f"upstream marketplace API error: {e}")


@app.get("/api/cabinets/{cabinet_id}/ozon/prices")
def get_cabinet_ozon_prices(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    return {"items": ozon_prices.get_price_list(client=client)}


@app.post("/api/cabinets/{cabinet_id}/ozon/prices")
def set_cabinet_ozon_prices(
    cabinet_id: int,
    body: dict,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    updates = body.get("updates", [])
    if not updates:
        raise HTTPException(status_code=400, detail="updates list is empty")
    return {"status": "done", "results": ozon_prices.update_prices(updates, client=client)}


@app.get("/api/cabinets/{cabinet_id}/ozon/promotions")
def get_cabinet_ozon_promotions(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    return ozon_promotions.get_cached(cabinet_id=str(cabinet_id))


@app.post("/api/cabinets/{cabinet_id}/ozon/promotions/refresh")
def refresh_cabinet_ozon_promotions(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    return ozon_promotions.refresh_promotions(client=client, cabinet_id=str(cabinet_id))


@app.get("/api/cabinets/{cabinet_id}/ozon/dimensions")
def get_cabinet_ozon_dimensions(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    return ozon_dimensions.get_cached(cabinet_id=str(cabinet_id))


@app.post("/api/cabinets/{cabinet_id}/ozon/dimensions/refresh")
def refresh_cabinet_ozon_dimensions(
    cabinet_id: int,
    x_telegram_init_data: Optional[str] = Header(default=None),
    telegram_id: Optional[int] = None,
):
    user_id = _resolve_user_id(x_telegram_init_data, telegram_id)
    cabinet = _owned_cabinet_or_404(cabinet_id, user_id)
    if cabinet["marketplace"] != "ozon":
        raise HTTPException(status_code=400, detail="not an Ozon cabinet")
    client = _build_client(cabinet)
    return ozon_dimensions.refresh_dimensions(client=client, cabinet_id=str(cabinet_id))


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
