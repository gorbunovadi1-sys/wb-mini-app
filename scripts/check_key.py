import os
import requests
from dotenv import load_dotenv

load_dotenv()
key = os.environ["WB_API_KEY"]
headers = {"Authorization": key}

checks = [
    ("Общая инфа о продавце (Content)", "https://common-api.wildberries.ru/api/v1/seller-info"),
    ("Статистика продаж (Statistics)", "https://statistics-api.wildberries.ru/api/v1/supplier/sales?dateFrom=2026-09-01"),
    ("Рекламные кампании (Promotion)", "https://advert-api.wildberries.ru/adv/v1/promotion/count"),
]

for name, url in checks:
    try:
        r = requests.get(url, headers=headers, timeout=15)
        print(f"{name}: HTTP {r.status_code}")
        print(f"  {r.text[:300]}")
    except Exception as e:
        print(f"{name}: ERROR {e}")
    print()
