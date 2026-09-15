import datetime


def parse_dt(raw: str) -> datetime.datetime:
    """Returns a naive UTC datetime. Handles both an explicit offset/'Z'
    (Ozon's usual shape) and a bare local-looking timestamp (WB's usual
    shape, assumed Moscow/UTC+3 — WB doesn't consistently send an offset)."""
    s = raw.replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt - datetime.timedelta(hours=3)  # assume Moscow time
    else:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt
