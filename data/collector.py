import requests
import time
from config.settings import TB_URL, TB_USERNAME, TB_PASSWORD, HISTORY_WINDOW_MS, SENSOR_KEYS_BY_ROOM

def get_token():
    r = requests.post(f"{TB_URL}/api/auth/login",
                      json={"username": TB_USERNAME, "password": TB_PASSWORD})
    return r.json()["token"]

def get_device_id_map(token: str) -> dict:
    """Returns {device_name: device_uuid} for all tenant devices."""
    page, page_size = 0, 100
    name_to_id = {}
    while True:
        r = requests.get(
            f"{TB_URL}/api/tenant/devices",
            headers={"Authorization": f"Bearer {token}"},
            params={"pageSize": page_size, "page": page},
        )
        r.raise_for_status()
        data = r.json()
        for device in data.get("data", []):
            name_to_id[device["name"]] = device["id"]["id"]
        if not data.get("hasNext", False):
            break
        page += 1
    return name_to_id

def get_telemetry(device_id, keys, token, window_ms=None):
    now      = int(time.time() * 1000)
    start_ms = now - (window_ms or HISTORY_WINDOW_MS)
    r = requests.get(
        f"{TB_URL}/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "keys":     ",".join(keys),
            "startTs":  start_ms,
            "endTs":    now,
            "agg":      "NONE",
            "limit":    10000,
            "orderBy":  "ASC",
        },
    )
    data = r.json()
    # Post-filter: guarantee the window is respected regardless of TB version.
    return {
        key: [p for p in readings if start_ms <= p["ts"] <= now]
        for key, readings in data.items()
    }

def get_all_rooms(window_ms=None) -> dict:
    token = get_token()
    device_id_map = get_device_id_map(token)
    raw = {}
    for room, keys in SENSOR_KEYS_BY_ROOM.items():
        raw[room] = {}
        for sensor_id in keys:
            device_id = device_id_map.get(sensor_id)
            if device_id is None:
                continue  # not yet registered in ThingsBoard
            raw[room][sensor_id] = get_telemetry(device_id, [sensor_id], token, window_ms)
    return raw

def summarize(raw: dict) -> dict:
    """
    For boolean/state sensors, find state transitions (False→True, True→False).
    For continuous sensors, keep min/max/avg.
    Returns a structured dict per room per sensor.
    """
    result = {}
    for room, sensors in raw.items():
        result[room] = {}
        for sensor_id, data in sensors.items():
            values = data.get(sensor_id, [])
            if not values:
                continue

            first = values[0]["value"]

            # Continuous sensor (ThingsBoard may return numbers as strings)
            try:
                is_continuous = not isinstance(first, bool) and float(first) is not None
            except (ValueError, TypeError):
                is_continuous = False

            if is_continuous:
                floats = [float(v["value"]) for v in values]
                result[room][sensor_id] = {
                    "type": "continuous",
                    "min": min(floats),
                    "max": max(floats),
                    "avg": round(sum(floats) / len(floats), 2)
                }

            # Boolean/state sensor — extract transitions
            elif not is_continuous:
                events = []
                prev = None
                for v in values:
                    val = v["value"]
                    ts  = v["ts"]  # millisecond timestamp
                    if val != prev:
                        events.append({"ts": ts, "value": val})
                        prev = val

                # Compute durations between transitions
                annotated = []
                for i, ev in enumerate(events):
                    duration_sec = None
                    if i + 1 < len(events):
                        duration_sec = (events[i+1]["ts"] - ev["ts"]) // 1000
                    annotated.append({
                        "value": ev["value"],
                        "ts": ev["ts"],
                        "duration_sec": duration_sec
                    })

                result[room][sensor_id] = {
                    "type": "boolean",
                    "current": values[-1]["value"],
                    "events": annotated
                }

    return result