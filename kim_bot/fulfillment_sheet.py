"""Reads her fulfillment team's own Google Sheet (публично доступна по
ссылке, без OAuth) — the "Артикулы" tab (article mapping + their manually-
tracked current stock) and "Журнал отгрузок" tab (their manual shipment
log). Uses the gviz CSV export endpoint, which works anonymously for a
link-shared sheet (the plain /export?format=csv endpoint returned 400
without a Google session when this was tested — gviz is the one that works)."""
import csv
import datetime
import io
import logging
import re

import requests

from . import articles
from .db import SessionLocal
from .models import ArticleMap

log = logging.getLogger("kim_bot.fulfillment_sheet")

BASELINE_COL_PATTERN = re.compile(r"Остаток на ФФ\s+(\d{1,2})\.(\d{1,2})")

SPREADSHEET_ID = "1kGAUiRPX1Xww6iyVEKvzcVQ6LrCIfwGUqy4F7o6flZo"
ARTICLES_GID = "525491536"
JOURNAL_GID = "116632056"

ARTICLE_COL = "Артикул завода"
WB_ARTICLE_COL = "WB: Артикул продавца"
FF_CURRENT_STOCK_COL = "Остаток на данный момент"

JOURNAL_DATE_COL = "Дата отгрузки"
JOURNAL_PLATFORM_COL = "Площадка"
JOURNAL_SCHEME_COL = "Схема"
JOURNAL_ARTICLE_COL = "Артикул завода"
JOURNAL_QTY_COL = "Количество"


def _fetch_csv_rows(gid: str) -> list[dict]:
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/gviz/tq?tqx=out:csv&gid={gid}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def fetch_article_rows() -> list[dict]:
    return _fetch_csv_rows(ARTICLES_GID)


def fetch_journal_rows() -> list[dict]:
    return _fetch_csv_rows(JOURNAL_GID)


def sync_article_map() -> int:
    """Refreshes ArticleMap from the "Артикулы" tab. Ozon isn't mapped here
    on purpose — her sheet has no separate Ozon seller-article column, i.e.
    offer_id already equals the factory article on Ozon; only WB's own
    article routinely differs from it."""
    rows = fetch_article_rows()
    updated = 0
    with SessionLocal() as db:
        for row in rows:
            canonical = (row.get(ARTICLE_COL) or "").strip()
            wb_article = (row.get(WB_ARTICLE_COL) or "").strip()
            if not canonical:
                continue
            existing = db.query(ArticleMap).filter_by(canonical_article=canonical).first()
            if existing is None:
                existing = ArticleMap(canonical_article=canonical)
                db.add(existing)
            existing.wb_article = wb_article or None
            updated += 1
        db.commit()
    articles.invalidate_cache()
    log.info(f"Synced {updated} article mappings from fulfillment sheet")
    return updated


def fetch_current_ff_stock() -> dict:
    """{canonical_article: qty} from "Остаток на данный момент" — her
    fulfillment team's own manually-tracked current count, the figure the
    reconciliation report compares the bot's own balance against."""
    stock = {}
    for row in fetch_article_rows():
        canonical = (row.get(ARTICLE_COL) or "").strip()
        raw = (row.get(FF_CURRENT_STOCK_COL) or "").strip()
        if not canonical or not raw:
            continue
        try:
            stock[canonical] = int(float(raw))
        except ValueError:
            continue
    return stock


def fetch_baseline_from_sheet(year: int = None) -> tuple[str, dict]:
    """Finds the "Остаток на ФФ ДД.ММ" column in the "Артикулы" tab — her
    one-time counted baseline, dated by its own header — and returns
    (as_of_date iso string, {canonical_article: qty}). `year` defaults to
    the current year since her sheet doesn't carry one in the header text."""
    rows = fetch_article_rows()
    if not rows:
        raise ValueError("Лист «Артикулы» пуст или недоступен")

    col = next((k for k in rows[0].keys() if BASELINE_COL_PATTERN.search(k or "")), None)
    if not col:
        raise ValueError('Не нашла колонку вида "Остаток на ФФ ДД.ММ" в листе «Артикулы»')

    m = BASELINE_COL_PATTERN.search(col)
    day, month = int(m.group(1)), int(m.group(2))
    as_of_date = datetime.date(year or datetime.date.today().year, month, day).isoformat()

    qty_by_article = {}
    for row in rows:
        article = (row.get(ARTICLE_COL) or "").strip()
        raw = (row.get(col) or "").strip()
        if not article or not raw:
            continue
        try:
            qty_by_article[article] = int(float(raw))
        except ValueError:
            continue
    return as_of_date, qty_by_article


def parse_journal_entries() -> list[dict]:
    """Returns [{date, platform, scheme, article, qty}] from "Журнал
    отгрузок" — platform lowercased ("wb"/"ozon"), scheme uppercased
    ("FBS"/"FBO"), matching the conventions used elsewhere in this bot."""
    entries = []
    for row in fetch_journal_rows():
        date_raw = (row.get(JOURNAL_DATE_COL) or "").strip()
        article = (row.get(JOURNAL_ARTICLE_COL) or "").strip()
        qty_raw = (row.get(JOURNAL_QTY_COL) or "").strip()
        if not date_raw or not article or not qty_raw:
            continue
        try:
            date = datetime.datetime.strptime(date_raw, "%d.%m.%Y").date()
            qty = int(float(qty_raw))
        except ValueError:
            continue
        entries.append({
            "date": date,
            "platform": (row.get(JOURNAL_PLATFORM_COL) or "").strip().lower(),
            "scheme": (row.get(JOURNAL_SCHEME_COL) or "").strip().upper(),
            "article": article,
            "qty": qty,
        })
    return entries
