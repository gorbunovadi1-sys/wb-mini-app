import datetime
import json
import logging
import os

from . import ozon_client

log = logging.getLogger("ozon_dimensions")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MAX_EVENTS = 500

TRACKED_FIELDS = [
    ("height", "высота"), ("width", "ширина"), ("depth", "глубина"), ("weight", "вес"),
]


def _state_file(cabinet_id):
    return os.path.join(DATA_DIR, f"ozon_dimensions_state_{cabinet_id}.json")


def _events_file(cabinet_id):
    return os.path.join(DATA_DIR, f"ozon_dimensions_events_{cabinet_id}.json")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def refresh_dimensions(client=None, cabinet_id="default") -> dict:
    """Snapshots product weight/dimensions, diffs against the last snapshot,
    and logs a change event whenever Ozon's catalog data for a product shifts
    (this affects logistics tariffs, so a silent change is worth surfacing).
    `cabinet_id` scopes the on-disk snapshot/events files to one Ozon cabinet."""
    client = client or ozon_client.default_client
    state_file, events_file = _state_file(cabinet_id), _events_file(cabinet_id)

    log.info("Fetching product attributes (dimensions)...")
    attrs = client.get_all_attributes()

    new_state = {}
    for a in attrs:
        offer_id = a.get("offer_id")
        if not offer_id:
            continue
        new_state[offer_id] = {
            "product_id": a.get("id"),
            "name": a.get("name") or offer_id,
            "height": a.get("height", 0),
            "width": a.get("width", 0),
            "depth": a.get("depth", 0),
            "weight": a.get("weight", 0),
            "dimension_unit": a.get("dimension_unit", "mm"),
            "weight_unit": a.get("weight_unit", "g"),
        }

    old_state = _load_json(state_file, {})
    events = _load_json(events_file, [])
    now = datetime.datetime.now().isoformat()

    if old_state:
        for offer_id, new in new_state.items():
            old = old_state.get(offer_id)
            if not old:
                continue
            for field, label in TRACKED_FIELDS:
                old_val, new_val = old.get(field, 0), new.get(field, 0)
                if old_val and new_val and old_val != new_val:
                    events.append({
                        "at": now, "offer_id": offer_id, "title": new["name"],
                        "field": field, "field_label": label,
                        "old_value": old_val, "new_value": new_val,
                        "unit": new["dimension_unit"] if field != "weight" else new["weight_unit"],
                    })

    events = events[-MAX_EVENTS:]
    _save_json(state_file, new_state)
    _save_json(events_file, events)

    return {
        "generated_at": now,
        "products_tracked": len(new_state),
        "recent_events": list(reversed(events[-50:])),
    }


def get_cached(cabinet_id="default"):
    state = _load_json(_state_file(cabinet_id), {})
    events = _load_json(_events_file(cabinet_id), [])
    return {
        "products_tracked": len(state),
        "recent_events": list(reversed(events[-50:])),
    }
