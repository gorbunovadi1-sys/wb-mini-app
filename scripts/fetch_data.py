import os
import json
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["WB_API_KEY"]
H = {"Authorization": KEY}

date_from = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()

print("Fetching sales...")
sales = requests.get(
    "https://statistics-api.wildberries.ru/api/v1/supplier/sales",
    headers=H, params={"dateFrom": date_from}, timeout=60
).json()

print("Fetching orders...")
orders = requests.get(
    "https://statistics-api.wildberries.ru/api/v1/supplier/orders",
    headers=H, params={"dateFrom": date_from}, timeout=60
).json()

print("Fetching ad campaign list...")
adv_count = requests.get(
    "https://advert-api.wildberries.ru/adv/v1/promotion/count",
    headers=H, timeout=60
).json()

# Only currently relevant statuses: 4 ready, 9 active, 11 paused
RELEVANT_STATUSES = {4, 9, 11}
advert_ids = []
for group in adv_count.get("adverts", []):
    if group.get("status") in RELEVANT_STATUSES:
        for a in group.get("advert_list", []):
            advert_ids.append(a["advertId"])

print(f"Fetching details for {len(advert_ids)} active/paused/ready campaigns...")
adverts_detail = []
for i in range(0, len(advert_ids), 50):
    chunk = advert_ids[i:i + 50]
    r = requests.get(
        "https://advert-api.wildberries.ru/api/advert/v2/adverts",
        headers=H, params={"ids": ",".join(map(str, chunk))}, timeout=60
    )
    if r.status_code == 200:
        adverts_detail.extend(r.json().get("adverts", []))
    else:
        print("adverts detail error:", r.status_code, r.text[:300])

print(f"Fetching balance...")
balance = requests.get("https://advert-api.wildberries.ru/adv/v1/balance", headers=H, timeout=30).json()

print(f"Fetching stats for {len(advert_ids)} campaigns...")
fullstats = []
if advert_ids:
    for i in range(0, len(advert_ids), 50):
        chunk = advert_ids[i:i + 50]
        r = requests.get(
            "https://advert-api.wildberries.ru/adv/v3/fullstats",
            headers=H, params={
                "ids": ",".join(map(str, chunk)),
                "beginDate": date_from,
                "endDate": datetime.date.today().isoformat(),
            }, timeout=60
        )
        if r.status_code == 200:
            fullstats.extend(r.json())
        else:
            print("fullstats error:", r.status_code, r.text[:300])

os.makedirs("data", exist_ok=True)
with open("data/sales.json", "w") as f:
    json.dump(sales, f, ensure_ascii=False, indent=2)
with open("data/orders.json", "w") as f:
    json.dump(orders, f, ensure_ascii=False, indent=2)
with open("data/adverts.json", "w") as f:
    json.dump(adverts_detail, f, ensure_ascii=False, indent=2)
with open("data/balance.json", "w") as f:
    json.dump(balance, f, ensure_ascii=False, indent=2)
with open("data/fullstats.json", "w") as f:
    json.dump(fullstats, f, ensure_ascii=False, indent=2)

print(f"Saved: {len(sales)} sales, {len(orders)} orders, {len(adverts_detail)} ad campaigns, balance={balance}, {len(fullstats)} stat rows")
