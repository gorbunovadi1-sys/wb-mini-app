import json
import logging
import os
import threading
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import margin
from .cost_prices import load_cost_prices, save_cost_prices
from .telegram_auth import validate_init_data

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
SUMMARY_FILE = os.path.join(DATA_DIR, "margin_summary.json")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

DEV_MODE = os.environ.get("DEV_MODE", "1") == "1"  # skips Telegram auth check when set

app = FastAPI(title="WB Mini App API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_refresh_lock = threading.Lock()


def refresh_summary():
    if not _refresh_lock.acquire(blocking=False):
        log.info("Refresh already in progress, skipping")
        return
    try:
        log.info("Refreshing margin summary...")
        summary = margin.build_margin_summary(days=30)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        log.info(f"Refreshed. Revenue={summary['account']['revenue']}, Profit={summary['account']['profit']}")
    except Exception:
        log.exception("Refresh failed")
    finally:
        _refresh_lock.release()


scheduler = BackgroundScheduler()
scheduler.add_job(refresh_summary, "interval", hours=1, id="refresh_margin")


@app.on_event("startup")
async def on_startup():
    scheduler.start()
    if not os.path.exists(SUMMARY_FILE):
        threading.Thread(target=refresh_summary, daemon=True).start()

    bot_token = os.environ.get("BOT_TOKEN")
    mini_app_url = os.environ.get("MINI_APP_URL")
    if bot_token and mini_app_url:
        import asyncio
        from bot.bot import build_dispatcher, build_bot
        bot = build_bot(bot_token)
        dp = build_dispatcher(mini_app_url)
        asyncio.create_task(dp.start_polling(bot))
        log.info("Telegram bot polling started")
    else:
        log.info("BOT_TOKEN/MINI_APP_URL not set — running API only, no bot polling")


def _check_auth(x_telegram_init_data: Optional[str]):
    if DEV_MODE:
        return
    if not x_telegram_init_data or not validate_init_data(x_telegram_init_data):
        raise HTTPException(status_code=401, detail="invalid Telegram init data")


@app.get("/api/margin")
def get_margin(x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    if not os.path.exists(SUMMARY_FILE):
        raise HTTPException(status_code=503, detail="data not ready yet, try again shortly")
    with open(SUMMARY_FILE, encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/margin/refresh")
def trigger_refresh(x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    threading.Thread(target=refresh_summary, daemon=True).start()
    return {"status": "refresh started"}


@app.get("/api/cost-prices")
def get_cost_prices(x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    return load_cost_prices()


@app.post("/api/cost-prices")
def set_cost_prices(prices: dict, x_telegram_init_data: Optional[str] = Header(default=None)):
    _check_auth(x_telegram_init_data)
    save_cost_prices(prices)
    threading.Thread(target=refresh_summary, daemon=True).start()
    return {"status": "saved"}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
