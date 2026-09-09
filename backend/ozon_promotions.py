import datetime
import json
import logging
import os

from . import ozon_client

log = logging.getLogger("ozon_promotions")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MAX_EVENTS = 500


def _state_file(cabinet_id):
    return os.path.join(DATA_DIR, f"ozon_promotions_state_{cabinet_id}.json")


def _events_file(cabinet_id):
    return os.path.join(DATA_DIR, f"ozon_promotions_events_{cabinet_id}.json")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def refresh_promotions(client=None, cabinet_id="default") -> dict:
    """Snapshots which products currently participate in which Ozon promotions,
    diffs against the last snapshot, and logs join/leave events. `cabinet_id`
    scopes the on-disk snapshot/events files to one connected Ozon cabinet."""
    client = client or ozon_client.default_client
    state_file, events_file = _state_file(cabinet_id), _events_file(cabinet_id)

    log.info("Fetching product attributes (names)...")
    attrs = client.get_all_attributes()
    product_names = {str(a["id"]): (a.get("name") or a.get("offer_id") or "") for a in attrs}
    product_offer_ids = {str(a["id"]): a.get("offer_id", "") for a in attrs}

    log.info("Fetching actions list...")
    actions = client.get_actions()
    action_titles = {str(a["id"]): a["title"] for a in actions}

    relevant_actions = [a for a in actions if a.get("is_participating") or a.get("participating_products_count", 0) > 0]

    new_state = {}  # product_id(str) -> {action_id(str): action_price}
    for action in relevant_actions:
        action_id = str(action["id"])
        try:
            products = client.get_action_products(action["id"])
        except Exception:
            log.exception(f"Failed to fetch products for action {action_id}")
            continue
        for p in products:
            pid = str(p["id"])
            new_state.setdefault(pid, {})[action_id] = p.get("action_price", 0)

    old_state = _load_json(state_file, {})
    events = _load_json(events_file, [])
    now = datetime.datetime.now().isoformat()

    all_pids = set(old_state) | set(new_state)
    for pid in all_pids:
        old_actions = set(old_state.get(pid, {}))
        new_actions = set(new_state.get(pid, {}))
        title = product_names.get(pid, pid)
        offer_id = product_offer_ids.get(pid, "")

        for action_id in new_actions - old_actions:
            events.append({
                "at": now, "product_id": pid, "offer_id": offer_id, "title": title,
                "action_id": action_id, "action_title": action_titles.get(action_id, action_id),
                "event": "joined",
            })
        for action_id in old_actions - new_actions:
            events.append({
                "at": now, "product_id": pid, "offer_id": offer_id, "title": title,
                "action_id": action_id, "action_title": action_titles.get(action_id, action_id),
                "event": "left",
            })

    events = events[-MAX_EVENTS:]
    _save_json(state_file, new_state)
    _save_json(events_file, events)

    current = []
    for pid, action_map in new_state.items():
        current.append({
            "product_id": pid,
            "offer_id": product_offer_ids.get(pid, ""),
            "title": product_names.get(pid, pid),
            "actions": [
                {"action_id": aid, "title": action_titles.get(aid, aid), "action_price": price}
                for aid, price in action_map.items()
            ],
        })
    current.sort(key=lambda x: x["title"])

    return {
        "generated_at": now,
        "actions_tracked": len(relevant_actions),
        "products_in_promotions": len(current),
        "current": current,
        "recent_events": list(reversed(events[-50:])),
    }


def get_cached(cabinet_id="default"):
    state = _load_json(_state_file(cabinet_id), {})
    events = _load_json(_events_file(cabinet_id), [])
    return {
        "products_in_promotions": len(state),
        "recent_events": list(reversed(events[-50:])),
    }
