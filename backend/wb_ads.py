import datetime


def get_campaigns_summary(client, days: int = 30) -> dict:
    """Per-campaign ad spend/revenue/ДРР for the last `days`, from WB's
    advert-api fullstats (already includes attributed revenue via
    sum_price, so ДРР = spend / attributed revenue needs no separate
    margin-report join)."""
    today = datetime.date.today()
    date_from = today - datetime.timedelta(days=days - 1)

    advert_ids = client.get_active_campaign_ids(changed_since=date_from.isoformat())
    if not advert_ids:
        return {
            "period_from": date_from.isoformat(), "period_to": today.isoformat(),
            "campaigns": [], "totals": _empty_totals(),
        }

    fullstats = client.get_campaign_fullstats(advert_ids, date_from.isoformat(), today.isoformat())
    details = client.get_campaign_details(advert_ids)
    names = {d.get("id"): (d.get("settings") or {}).get("name") for d in details}

    campaigns = []
    for s in fullstats:
        aid = s.get("advertId")
        spend = s.get("sum", 0)
        revenue = s.get("sum_price", 0)
        campaigns.append({
            "id": aid,
            "name": names.get(aid) or str(aid),
            "spend": round(spend, 2),
            "revenue": round(revenue, 2),
            "drr": round(spend / revenue * 100, 2) if revenue else None,
            "orders": s.get("orders", 0),
            "views": s.get("views", 0),
            "clicks": s.get("clicks", 0),
            "ctr": s.get("ctr", 0),
            "cpc": s.get("cpc", 0),
            "cr": s.get("cr", 0),
        })
    campaigns.sort(key=lambda x: -x["spend"])

    totals = _empty_totals()
    totals["spend"] = round(sum(c["spend"] for c in campaigns), 2)
    totals["revenue"] = round(sum(c["revenue"] for c in campaigns), 2)
    totals["orders"] = sum(c["orders"] for c in campaigns)
    totals["clicks"] = sum(c["clicks"] for c in campaigns)
    totals["views"] = sum(c["views"] for c in campaigns)
    totals["drr"] = round(totals["spend"] / totals["revenue"] * 100, 2) if totals["revenue"] else None

    return {"period_from": date_from.isoformat(), "period_to": today.isoformat(), "campaigns": campaigns, "totals": totals}


def _empty_totals():
    return {"spend": 0, "revenue": 0, "orders": 0, "clicks": 0, "views": 0, "drr": None}
