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
    _assign_verdicts(result)
    return result


# Below this many impressions, a cluster's performance doesn't mean
# anything yet — used whenever the campaign actually reports impressions
# (cpm). Below this much SPEND is the fallback for a cpc campaign, which
# WB never reports impressions for at all (confirmed live 2026-09-15 —
# `views` comes back 0/absent on every cpc entry, never a real count), so
# "100 показов" has nothing to compare against there and spend is the only
# signal left to gate on.
_MIN_VIEWS_FOR_VERDICT = 100  # показов (cpm)
_MIN_SPEND_FOR_VERDICT = 100  # ₽ (cpc fallback)

# Thresholds relative to THIS campaign's own average cost-per-click/
# conversion rate, not fixed numbers — a cheap click or a 2% CR in one
# category can be expensive/bad in another, so these only mean anything
# compared to the rest of the same campaign. Matches the reasoning a
# manually-built weekly ad report already uses for its own "Зачистить/
# Проверить/Масштабировать" verdict column.
_SCALE_CPC_RATIO = 0.7  # cpc at or below 70% of campaign average — cheap traffic
_CHECK_CPC_RATIO = 1.5  # cpc at or above 150% of campaign average — worth a look


def _assign_verdicts(clusters: list) -> None:
    """Mutates each cluster dict in place, adding `verdict` (machine key)
    and `verdict_label` (what to actually do) — a recommendation for
    whether to exclude, watch, or scale a search cluster's spend, mirroring
    the verdict column Дарья's manually-built weekly report already shows."""
    spent_with_clicks = [c for c in clusters if c["clicks"]]
    total_spend = sum(c["spend"] for c in spent_with_clicks)
    total_clicks = sum(c["clicks"] for c in spent_with_clicks)
    total_orders = sum(c["orders"] for c in spent_with_clicks)
    avg_cpc = (total_spend / total_clicks) if total_clicks else None
    avg_cr = (total_orders / total_clicks * 100) if total_clicks else None

    # A campaign either reports real impression counts throughout (cpm) or
    # never does (cpc) — never a mix — so one cluster having any views at
    # all is a reliable signal for the whole campaign, no need to thread
    # payment_type through the call chain separately.
    has_views_data = any(c["views"] for c in clusters)

    for c in clusters:
        enough_data = (c["views"] >= _MIN_VIEWS_FOR_VERDICT) if has_views_data else (c["spend"] >= _MIN_SPEND_FOR_VERDICT)
        if not enough_data:
            c["verdict"] = "low_data"
            c["verdict_label"] = (
                f"Мало данных — всего {c['views']} показов" if has_views_data
                else f"Мало данных — потрачено всего {c['spend']} ₽"
            )
        elif not c["clicks"] and not c["orders"]:
            # Real spend, nobody even clicked — a targeting/bid problem
            # (the ad isn't earning attention), distinct from the next case
            # below (it IS earning clicks, just not converting them) —
            # different diagnosis, both worth showing separately rather than
            # folding into one generic "clean this up".
            c["verdict"] = "zero_clicks"
            exposure = f"{c['views']} показов" if c["views"] else "без показов в отчёте"
            c["verdict_label"] = f"Зачистить — {c['spend']} ₽ расхода, {exposure}, ни одного клика"
        elif not c["orders"]:
            # Clicks happened, spend happened, nothing converted — paying
            # for traffic that doesn't buy, not just for exposure.
            c["verdict"] = "zero_orders"
            c["verdict_label"] = f"Зачистить — {c['spend']} ₽ расхода, {c['clicks']} кликов, 0 заказов"
        elif c["cpc"] is not None and avg_cpc:
            cr = (c["orders"] / c["clicks"] * 100) if c["clicks"] else 0
            cheap = c["cpc"] <= avg_cpc * _SCALE_CPC_RATIO
            expensive = c["cpc"] >= avg_cpc * _CHECK_CPC_RATIO
            converting_ok = avg_cr is None or cr >= avg_cr
            if cheap and converting_ok:
                c["verdict"] = "scale"
                c["verdict_label"] = f"Масштабировать — клик дешевле среднего ({round(avg_cpc, 2)} ₽), конверсия {round(cr, 1)}% не хуже средней"
            elif expensive or (cheap and not converting_ok):
                # Cheap clicks that convert worse than average are still
                # worth a look, not an automatic scale-up — cheap traffic
                # that doesn't buy just means cheap wasted spend.
                c["verdict"] = "check"
                reason = "клик дороже среднего" if expensive else "дешёвый клик, но конверсия ниже средней"
                c["verdict_label"] = f"Проверить — {reason} ({round(avg_cpc, 2)} ₽ в среднем по кампании)"
            else:
                c["verdict"] = "normal"
                c["verdict_label"] = "Норма"
        else:
            c["verdict"] = "normal"
            c["verdict_label"] = "Норма"


def cluster_summary(clusters: list) -> dict:
    """Rollup for the campaign card — "N кластеров под зачистку, ₽X можно
    сэкономить" — shown once the cluster box is opened rather than fetched
    for every campaign up front (see get_campaign_clusters's own docstring
    on why: normquery/stats' rate limit doesn't allow that)."""
    clean = [c for c in clusters if c["verdict"] in ("zero_clicks", "zero_orders")]
    return {
        "clean_count": len(clean),
        "clean_spend": round(sum(c["spend"] for c in clean), 2),
        "check_count": sum(1 for c in clusters if c["verdict"] == "check"),
        "scale_count": sum(1 for c in clusters if c["verdict"] == "scale"),
    }


def exclude_cluster_from_campaign(client, advert_id: int, norm_query: str) -> dict:
    """Adds `norm_query` (a search cluster/phrase, exactly as shown by
    get_campaign_clusters) to the minus-phrase list for every item this
    campaign promotes. WB scopes minus-phrases per (advert_id, nm_id), not
    per campaign as a whole (see WBClient.set_minus_phrases) — applying it
    to every nm_id the campaign has is what actually excludes the cluster
    campaign-wide, matching what "Кластеры и фразы" shows (spend already
    summed across all the campaign's items).

    Always reads each item's EXISTING minus-phrase list first and merges
    `norm_query` into it — set_minus_phrases replaces the whole list, so
    skipping this step would silently wipe out any minus-phrases already
    set (via this app or WB's own seller cabinet)."""
    details = client.get_campaign_details([advert_id])
    if not details:
        return {"updated": [], "failed": [], "reason": "campaign not found"}
    nm_ids = nm_ids_from_campaign_detail(details[0])
    if not nm_ids:
        return {"updated": [], "failed": [], "reason": "no items on this campaign"}

    items = [{"advert_id": advert_id, "nm_id": nm} for nm in nm_ids]
    current = client.get_minus_phrases(items)
    current_by_nm = {c.get("nm_id"): (c.get("norm_queries") or []) for c in current}

    updated, failed = [], []
    for nm in nm_ids:
        existing = current_by_nm.get(nm, [])
        if norm_query in existing:
            updated.append(nm)
            continue
        try:
            client.set_minus_phrases(advert_id, nm, existing + [norm_query])
            updated.append(nm)
        except Exception:
            failed.append(nm)
    return {"updated": updated, "failed": failed}


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
