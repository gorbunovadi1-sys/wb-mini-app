import json
import collections
import datetime

sales = json.load(open("data/sales.json"))
orders = json.load(open("data/orders.json"))
adverts = json.load(open("data/adverts.json"))
fullstats = json.load(open("data/fullstats.json"))
balance = json.load(open("data/balance.json"))

STATUS_NAMES = {4: "Готова к запуску", 9: "Активна", 11: "На паузе"}

# --- Sales/revenue ---
daily_revenue = collections.defaultdict(float)
product_revenue = collections.defaultdict(lambda: {"revenue": 0.0, "qty": 0, "brand": "", "subject": ""})
total_revenue = 0.0
total_returns = 0.0
for s in sales:
    day = s["date"][:10]
    amount = s.get("forPay", 0) or 0
    if amount >= 0:
        daily_revenue[day] += amount
        total_revenue += amount
        key = s.get("supplierArticle", "?")
        product_revenue[key]["revenue"] += amount
        product_revenue[key]["qty"] += 1
        product_revenue[key]["brand"] = s.get("brand", "")
        product_revenue[key]["subject"] = s.get("subject", "")
    else:
        total_returns += -amount

# --- Orders ---
total_orders = len(orders)
cancelled_orders = sum(1 for o in orders if o.get("isCancel"))
daily_orders = collections.defaultdict(int)
for o in orders:
    daily_orders[o["date"][:10]] += 0 if o.get("isCancel") else 1

# --- Ads ---
adv_by_id = {a["id"]: a for a in adverts}
stats_by_id = {s["advertId"]: s for s in fullstats}

campaigns = []
total_ad_spend = 0.0
total_ad_orders = 0
total_views = 0
total_clicks = 0
for adv_id, adv in adv_by_id.items():
    st = stats_by_id.get(adv_id, {})
    spend = st.get("sum", 0) or 0
    total_ad_spend += spend
    total_ad_orders += st.get("orders", 0) or 0
    total_views += st.get("views", 0) or 0
    total_clicks += st.get("clicks", 0) or 0
    nm_names = []
    for nm in adv.get("nm_settings", []) or []:
        nm_names.append(nm.get("subject", {}).get("name", ""))
    campaigns.append({
        "id": adv_id,
        "name": adv.get("settings", {}).get("name", str(adv_id)),
        "status": STATUS_NAMES.get(adv.get("status"), str(adv.get("status"))),
        "status_code": adv.get("status"),
        "payment_type": adv.get("settings", {}).get("payment_type", ""),
        "subjects": ", ".join(sorted(set(n for n in nm_names if n))),
        "spend": round(spend, 2),
        "views": st.get("views", 0) or 0,
        "clicks": st.get("clicks", 0) or 0,
        "ctr": st.get("ctr", 0) or 0,
        "cpc": st.get("cpc", 0) or 0,
        "orders": st.get("orders", 0) or 0,
        "atbs": st.get("atbs", 0) or 0,
        "cr": st.get("cr", 0) or 0,
        "created": adv.get("timestamps", {}).get("created", ""),
    })

campaigns.sort(key=lambda c: -c["spend"])

drr = (total_ad_spend / total_revenue * 100) if total_revenue else 0

# --- Top products ---
top_products = sorted(
    [{"name": k, **v} for k, v in product_revenue.items()],
    key=lambda x: -x["revenue"]
)[:15]

summary = {
    "generated_at": datetime.datetime.now().isoformat(),
    "period_days": 30,
    "revenue": {
        "total": round(total_revenue, 2),
        "returns": round(total_returns, 2),
        "daily": [{"date": d, "revenue": round(v, 2)} for d, v in sorted(daily_revenue.items())],
    },
    "orders": {
        "total": total_orders,
        "cancelled": cancelled_orders,
        "daily": [{"date": d, "orders": v} for d, v in sorted(daily_orders.items())],
    },
    "ads": {
        "balance_net": balance.get("net", 0),
        "balance_cashback": sum(c.get("sum", 0) for c in balance.get("cashbacks", [])),
        "total_spend": round(total_ad_spend, 2),
        "total_orders_from_ads": total_ad_orders,
        "total_views": total_views,
        "total_clicks": total_clicks,
        "drr_percent": round(drr, 2),
        "campaigns": campaigns,
    },
    "top_products": top_products,
}

with open("data/summary.json", "w") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print(f"Revenue: {total_revenue:.0f} RUB, Ad spend: {total_ad_spend:.0f} RUB, DRR: {drr:.1f}%")
print(f"Active campaigns: {sum(1 for c in campaigns if c['status_code']==9)}, Paused: {sum(1 for c in campaigns if c['status_code']==11)}")
