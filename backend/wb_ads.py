import datetime

from .wb_client import nm_ids_from_campaign_detail


def get_campaign_clusters(client, advert_id: int, days: int = 30) -> list:
    """Search-cluster/phrase-level ad spend for one campaign — the
    "Кластеры и фразы" breakdown a downloaded WB ad-campaign export shows,
    now reachable live via normquery/stats (WBClient.get_normquery_stats —
    confirmed 2026-09-15 to return real data for both cpm and cpc
    campaigns, despite WB's own docs claiming cpm-only). Called lazily,
    per-campaign, from the Реклама tab on demand — not fetched for every
    campaign up front, since the endpoint's rate limit (10 req/min with a
    personal token) doesn't leave room for that on an account with dozens
    of campaigns.

    Returns clusters sorted by spend descending: [{norm_query, spend,
    clicks, orders, atbs, shks, views, ctr, cpc}, ...]. views/ctr are 0 for
    a cpc campaign's clusters (WB doesn't report impressions there)."""
    today = datetime.date.today()
    date_from = (today - datetime.timedelta(days=days - 1)).isoformat()
    date_to = today.isoformat()

    details = client.get_campaign_details([advert_id])
    if not details:
        return []
    nm_ids = nm_ids_from_campaign_detail(details[0])
    if not nm_ids:
        return []

    items = [{"advert_id": advert_id, "nm_id": nm} for nm in nm_ids]
    raw = client.get_normquery_stats(items, date_from, date_to)

    clusters = {}
    for entry in raw:
        for s in (entry.get("stats") or []):
            key = s.get("norm_query") or "(без кластера)"
            c = clusters.setdefault(key, {
                "norm_query": key, "spend": 0.0, "clicks": 0, "orders": 0,
                "atbs": 0, "shks": 0, "views": 0,
            })
            c["spend"] += s.get("spend") or 0
            c["clicks"] += s.get("clicks") or 0
            c["orders"] += s.get("orders") or 0
            c["atbs"] += s.get("atbs") or 0
            c["shks"] += s.get("shks") or 0
            c["views"] += s.get("views") or 0

    result = list(clusters.values())
    for c in result:
        c["spend"] = round(c["spend"], 2)
        c["ctr"] = round(c["clicks"] / c["views"] * 100, 2) if c["views"] else None
        c["cpc"] = round(c["spend"] / c["clicks"], 2) if c["clicks"] else None
    result.sort(key=lambda x: -x["spend"])
    return result


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
