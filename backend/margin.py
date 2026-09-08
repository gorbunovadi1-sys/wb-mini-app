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


def _row_date(row):
    d = row.get("rrDate") or row.get("saleDt") or row.get("orderDt")
    if not d:
        return None
    return d[:10]


def _empty_bucket():
    return {
        "revenue": 0.0, "qty": 0, "forpay": 0.0, "commission": 0.0,
        "logistics": 0.0, "storage": 0.0, "penalty_deduction": 0.0,
    }


def _accumulate(bucket, row):
    bucket["forpay"] += _num(row, "forPay")
    bucket["commission"] += _num(row, "ppvzSalesCommission")
    bucket["logistics"] += _num(row, "deliveryAmount") + _num(row, "deliveryService") + _num(row, "rebillLogisticCost")
    bucket["storage"] += _num(row, "paidStorage") + _num(row, "paidAcceptance")
    bucket["penalty_deduction"] += _num(row, "penalty") + _num(row, "deduction")
    if row.get("docTypeName") == "Продажа":
        bucket["revenue"] += _num(row, "retailAmount")
        bucket["qty"] += int(row.get("quantity") or 0)


def build_margin_summary(days: int = 30) -> dict:
    date_to = datetime.date.today()
    cutoff = date_to - datetime.timedelta(days=days)
    fetch_from = date_to - datetime.timedelta(days=days * 2)

    log.info("Fetching sales report list...")
    reports = wb_client.get_sales_reports(fetch_from.isoformat(), date_to.isoformat(), period="weekly")
    report_ids = sorted({r["reportId"] for r in reports})
    log.info(f"{len(report_ids)} reports to pull detail for")

    all_rows = []
    for rid in report_ids:
        all_rows.extend(wb_client.get_report_detail(rid))
    log.info(f"{len(all_rows)} detail rows fetched")

    per_nm = collections.defaultdict(lambda: {
        **_empty_bucket(), "title": "", "vendor_code": "", "brand": "",
    })
    prev_totals = _empty_bucket()
    daily = collections.defaultdict(lambda: {"revenue": 0.0, "forpay": 0.0})

    for row in all_rows:
        row_date_str = _row_date(row)
        if not row_date_str:
            continue
        try:
            row_date = datetime.date.fromisoformat(row_date_str)
        except ValueError:
            continue
        if row_date < fetch_from:
            continue

        if row_date >= cutoff:
            nm = row.get("nmId")
            if nm:
                p = per_nm[nm]
                _accumulate(p, row)
                p["title"] = row.get("title") or p["title"]
                p["vendor_code"] = row.get("vendorCode") or p["vendor_code"]
                p["brand"] = row.get("brandName") or p["brand"]
            d = daily[row_date_str]
            d["forpay"] += _num(row, "forPay")
            if row.get("docTypeName") == "Продажа":
                d["revenue"] += _num(row, "retailAmount")
        else:
            _accumulate(prev_totals, row)

    total_revenue = sum(p["revenue"] for p in per_nm.values())
    prev_revenue = prev_totals["revenue"]

    # --- Ad spend: current period allocated per-product, plus previous period total for comparison ---
    log.info("Fetching ad campaign spend...")
    advert_ids = wb_client.get_active_campaign_ids(changed_since=fetch_from.isoformat())
    fullstats = wb_client.get_campaign_fullstats(advert_ids, cutoff.isoformat(), date_to.isoformat())
    total_ad_spend = sum(_num(s, "sum") for s in fullstats)

    prev_fullstats = wb_client.get_campaign_fullstats(advert_ids, fetch_from.isoformat(), (cutoff - datetime.timedelta(days=1)).isoformat())
    prev_ad_spend = sum(_num(s, "sum") for s in prev_fullstats)

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

    prev_forpay = prev_totals["forpay"]
    # previous period COGS uses today's cost-price table too (best available estimate)
    # Previous period COGS isn't known per-product, so approximate it using the
    # current period's overall COGS-to-revenue ratio applied to previous revenue.
    cogs_ratio = (total_cogs / total_revenue) if total_revenue else 0.0
    prev_cogs_approx = cogs_ratio * prev_revenue
    prev_profit_approx = prev_forpay - prev_ad_spend - prev_cogs_approx

    daily_series = [
        {"date": d, "revenue": round(v["revenue"], 2), "forpay": round(v["forpay"], 2)}
        for d, v in sorted(daily.items())
    ]

    def _delta(cur, prev):
        diff = cur - prev
        pct = (diff / prev * 100) if prev else None
        return {"prev": round(prev, 2), "diff": round(diff, 2), "pct": round(pct, 2) if pct is not None else None}

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
            "qty_total": sum(p["qty"] for p in per_nm.values()),
        },
        "compare": {
            "revenue": _delta(total_revenue, prev_revenue),
            "profit": _delta(total_profit, prev_profit_approx),
            "ad_spend": _delta(total_ad_spend, prev_ad_spend),
            "margin_percent": _delta(
                total_margin_pct,
                (prev_profit_approx / prev_revenue * 100) if prev_revenue else 0.0,
            ),
        },
        "daily": daily_series,
        "products": products,
    }
