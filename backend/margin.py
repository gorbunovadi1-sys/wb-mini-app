import collections
import datetime
import logging

from . import wb_client
from .cost_prices import load_cost_prices

log = logging.getLogger("margin")


def _num(row, key):
    v = row.get(key)
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_margin_summary(days: int = 30) -> dict:
    date_to = datetime.date.today()
    date_from = date_to - datetime.timedelta(days=days)

    log.info("Fetching sales report list...")
    reports = wb_client.get_sales_reports(date_from.isoformat(), date_to.isoformat(), period="weekly")
    report_ids = sorted({r["reportId"] for r in reports})
    log.info(f"{len(report_ids)} reports to pull detail for")

    all_rows = []
    for rid in report_ids:
        all_rows.extend(wb_client.get_report_detail(rid))
    log.info(f"{len(all_rows)} detail rows fetched")

    per_nm = collections.defaultdict(lambda: {
        "revenue": 0.0, "qty": 0, "forpay": 0.0, "commission": 0.0,
        "logistics": 0.0, "storage": 0.0, "penalty_deduction": 0.0,
        "title": "", "vendor_code": "", "brand": "",
    })

    for row in all_rows:
        nm = row.get("nmId")
        if not nm:
            continue
        p = per_nm[nm]
        p["forpay"] += _num(row, "forPay")
        p["commission"] += _num(row, "ppvzSalesCommission")
        p["logistics"] += _num(row, "deliveryAmount") + _num(row, "deliveryService") + _num(row, "rebillLogisticCost")
        p["storage"] += _num(row, "paidStorage") + _num(row, "paidAcceptance")
        p["penalty_deduction"] += _num(row, "penalty") + _num(row, "deduction")
        if row.get("docTypeName") == "Продажа":
            p["revenue"] += _num(row, "retailAmount")
            p["qty"] += int(row.get("quantity") or 0)
        p["title"] = row.get("title") or p["title"]
        p["vendor_code"] = row.get("vendorCode") or p["vendor_code"]
        p["brand"] = row.get("brandName") or p["brand"]

    total_revenue = sum(p["revenue"] for p in per_nm.values())

    # --- Ad spend, allocated proportionally to each product's revenue share ---
    log.info("Fetching ad campaign spend...")
    advert_ids = wb_client.get_active_campaign_ids()
    fullstats = wb_client.get_campaign_fullstats(advert_ids, date_from.isoformat(), date_to.isoformat())
    total_ad_spend = sum(_num(s, "sum") for s in fullstats)

    cost_prices = load_cost_prices()

    products = []
    for nm, p in per_nm.items():
        ad_share = (p["revenue"] / total_revenue * total_ad_spend) if total_revenue else 0.0
        cogs_unit = cost_prices.get(str(nm), 0)
        cogs_total = cogs_unit * p["qty"]
        profit = p["forpay"] - ad_share - cogs_total
        margin_pct = (profit / p["revenue"] * 100) if p["revenue"] else 0.0
        products.append({
            "nm_id": nm,
            "title": p["title"] or p["vendor_code"] or str(nm),
            "vendor_code": p["vendor_code"],
            "brand": p["brand"],
            "revenue": round(p["revenue"], 2),
            "qty": p["qty"],
            "commission": round(p["commission"], 2),
            "logistics": round(p["logistics"], 2),
            "storage": round(p["storage"], 2),
            "penalty_deduction": round(p["penalty_deduction"], 2),
            "forpay": round(p["forpay"], 2),
            "ad_spend": round(ad_share, 2),
            "cogs_unit": cogs_unit,
            "cogs_total": round(cogs_total, 2),
            "profit": round(profit, 2),
            "margin_percent": round(margin_pct, 2),
            "has_cost_price": str(nm) in cost_prices,
        })

    products.sort(key=lambda x: -x["revenue"])

    total_forpay = sum(p["forpay"] for p in per_nm.values())
    total_cogs = sum(pr["cogs_total"] for pr in products)
    total_profit = total_forpay - total_ad_spend - total_cogs
    total_margin_pct = (total_profit / total_revenue * 100) if total_revenue else 0.0

    return {
        "generated_at": datetime.datetime.now().isoformat(),
        "period_days": days,
        "account": {
            "revenue": round(total_revenue, 2),
            "commission": round(sum(p["commission"] for p in per_nm.values()), 2),
            "logistics": round(sum(p["logistics"] for p in per_nm.values()), 2),
            "storage": round(sum(p["storage"] for p in per_nm.values()), 2),
            "penalty_deduction": round(sum(p["penalty_deduction"] for p in per_nm.values()), 2),
            "forpay": round(total_forpay, 2),
            "ad_spend": round(total_ad_spend, 2),
            "cogs_total": round(total_cogs, 2),
            "profit": round(total_profit, 2),
            "margin_percent": round(total_margin_pct, 2),
            "cost_prices_known_for": sum(1 for pr in products if pr["has_cost_price"]),
            "cost_prices_total_products": len(products),
        },
        "products": products,
    }
