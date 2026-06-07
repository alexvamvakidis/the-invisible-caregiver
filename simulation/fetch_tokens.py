#!/usr/bin/env python3
"""
fetch_tokens.py
---------------
Logs into ThingsBoard, finds the device whose name matches each sensor_id
in all_sensors.csv, fetches its access token, and writes the token back
into the CSV.

Run this once after you have created all devices in ThingsBoard.
Device names in ThingsBoard must match the sensor_id column in the CSV
(e.g. the device named "bed_pressure_01" maps to sensor_id "bed_pressure_01").

Usage:
    python fetch_tokens.py
    python fetch_tokens.py --host localhost --port 9090
    python fetch_tokens.py --user tenant@thingsboard.org --password tenant
"""

import csv
import sys
import requests
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import TB_HOST, TB_PORT_API, TB_USERNAME, TB_PASSWORD, CSV_PATH, TOKENS_PATH


# ── ThingsBoard API helpers ───────────────────────────────────────────────────

def tb_login(base_url, username, password):
    """
    POST credentials to ThingsBoard and return a JWT token.
    This JWT is used to authorise all subsequent API calls.
    """
    resp = requests.post(
        f"{base_url}/api/auth/login",
        json={"username": username, "password": password},
        timeout=5
    )
    resp.raise_for_status()
    return resp.json()["token"]


def tb_get_devices(base_url, jwt):
    """
    Fetch all tenant devices from ThingsBoard (up to 1000).
    Returns a list of device objects, each containing 'name' and 'id'.
    """
    resp = requests.get(
        f"{base_url}/api/tenant/devices?pageSize=1000&page=0",
        headers={"X-Authorization": f"Bearer {jwt}"},
        timeout=5
    )
    resp.raise_for_status()
    return resp.json()["data"]


def tb_get_token(base_url, jwt, device_id):
    """
    Fetch the access token (credentials) for one device by its ThingsBoard device ID.
    Returns the token string (credentialsId).
    """
    resp = requests.get(
        f"{base_url}/api/device/{device_id}/credentials",
        headers={"X-Authorization": f"Bearer {jwt}"},
        timeout=5
    )
    resp.raise_for_status()
    return resp.json()["credentialsId"]


# ── Main logic ────────────────────────────────────────────────────────────────

def fetch_and_save_tokens(host, port, username, password):
    base_url = f"http://{host}:{port}"

    # Step 1: Authenticate with ThingsBoard
    print(f"Connecting to {base_url} …")
    try:
        jwt = tb_login(base_url, username, password)
    except requests.exceptions.ConnectionError:
        print("Error: Cannot reach ThingsBoard. Is it running?")
        return
    except requests.exceptions.HTTPError as e:
        print(f"Login failed: {e}")
        return
    print("Logged in OK\n")

    # Step 2: Fetch all devices and build a name → device_id lookup
    devices = tb_get_devices(base_url, jwt)
    device_lookup = {d["name"]: d["id"]["id"] for d in devices}
    print(f"Found {len(devices)} device(s) in ThingsBoard\n")

    # Step 3: Load the sensor list
    csv_path    = Path(CSV_PATH)
    tokens_path = Path(TOKENS_PATH)
    with open(csv_path, newline="") as f:
        sensor_ids = [row["sensor_id"] for row in csv.DictReader(f)]

    # Step 4: Match each sensor_id to a ThingsBoard device and fetch its token
    matched   = 0
    not_found = []
    rows      = []

    for sensor_id in sensor_ids:
        device_id = device_lookup.get(sensor_id)

        if device_id:
            token = tb_get_token(base_url, jwt, device_id)
            rows.append({"sensor_id": sensor_id, "token": token})
            matched += 1
            print(f"  OK  {sensor_id:<35} token: {token[:12]}…")
        else:
            rows.append({"sensor_id": sensor_id, "token": ""})
            not_found.append(sensor_id)
            print(f"  --  {sensor_id:<35} not found in ThingsBoard")

    # Step 5: Write tokens to sensor_tokens.csv
    with open(tokens_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["sensor_id", "token"])
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    print(f"\n{matched}/{len(sensor_ids)} tokens saved to {tokens_path}")

    if not_found:
        print(f"\nNot matched ({len(not_found)}):")
        for sid in not_found:
            print(f"  {sid}")
        print("\nMake sure the device name in ThingsBoard exactly matches the sensor_id.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch ThingsBoard device tokens and save them to all_sensors.csv."
    )
    parser.add_argument("--host",     default=TB_HOST,
                        help=f"ThingsBoard host (default: {TB_HOST})")
    parser.add_argument("--port",     type=int, default=TB_PORT_API,
                        help=f"ThingsBoard port (default: {TB_PORT_API})")
    parser.add_argument("--user",     default=TB_USERNAME,
                        help=f"ThingsBoard login email (default: {TB_USERNAME})")
    parser.add_argument("--password", default=TB_PASSWORD,
                        help="ThingsBoard login password")
    args = parser.parse_args()

    fetch_and_save_tokens(args.host, args.port, args.user, args.password)
