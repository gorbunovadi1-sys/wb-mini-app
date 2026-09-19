"""DB-backed себестоимость для Сельдереевой — same {item_key: price} dict
shape backend/margin.py and backend/ozon_margin.py already expect via their
`cost_prices` parameter, just sourced from seld_cost_price instead of the
legacy single-shop data/cost_prices.json (see models.py's SeldCostPrice for
why that file must not be reused here)."""
from .db import SessionLocal
from .models import SeldCostPrice


def load_cost_prices(marketplace: str) -> dict:
    """Maps item_key (str) -> cost price in RUB for one marketplace."""
    with SessionLocal() as db:
        rows = db.query(SeldCostPrice).filter_by(marketplace=marketplace).all()
        return {r.item_key: r.cost_price for r in rows}


def set_cost_price(marketplace: str, item_key: str, cost_price: float):
    with SessionLocal() as db:
        row = db.query(SeldCostPrice).filter_by(marketplace=marketplace, item_key=item_key).first()
        if row:
            row.cost_price = cost_price
        else:
            db.add(SeldCostPrice(marketplace=marketplace, item_key=item_key, cost_price=cost_price))
        db.commit()
