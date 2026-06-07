
from pathlib import Path
from datetime import date

_ROOT = Path(__file__).parent.parent

# ThingsBoard
TB_HOST      = "localhost"
TB_PORT_API  = 9090
TB_PORT_MQTT = 1883
TB_URL       = f"http://{TB_HOST}:{TB_PORT_API}"
TB_USERNAME  = "tenant@thingsboard.org"
TB_PASSWORD  = "tenant"

# Simulation
CSV_PATH         = _ROOT / "config" / "all_sensors.csv"
TOKENS_PATH      = _ROOT / "config" / "sensor_tokens.csv"
SIM_INTERVAL_SEC = 5
SIM_DATE         = date.today().strftime("%Y-%m-%d")

SCENARIO_FILES = {
    "normal":  _ROOT / "simulation" / "scenarios" / "scenario_normal.json",
    "decline": _ROOT / "simulation" / "scenarios" / "scenario_decline.json",
    "hazard":  _ROOT / "simulation" / "scenarios" / "scenario_hazard.json",
}

EVENT_FILES = {
    "normal":  _ROOT / "simulation" / "events" / "events_normal.json",
    "decline": _ROOT / "simulation" / "events" / "events_decline.json",
    "hazard":  _ROOT / "simulation" / "events" / "events_hazard.json",
}

# Telemetry pull 
HISTORY_WINDOW_MS = 3_600_000  # 1 hour

SENSOR_KEYS_BY_ROOM = {
    "bathroom": [
        "bathroom_motion", "toilet_pressure", "bathroom_water_flow",
        "bathroom_shower_water_temp", "bathroom_temperature"
    ],
    "bedroom": [
        "bed_pressure", "bedroom_motion",
        "bedroom_lamp_plug", "bedroom_temperature"
    ],
    "living_room": [
        "living_motion", "sofa_pressure", "sofa_pressure_2", "tv_plug"
    ],
    "entrance": [
        "entrance_motion", "entrance_door"
    ],
    "kitchen": [
        "kitchen_motion", "kitchen_temperature", "stove_power",
        "smoke_detector", "fridge_door", "kitchen_faucet",
        "kitchen_medication_cabinet"
    ],
}

# Flat list for simple queries
ALL_SENSOR_KEYS = [s for keys in SENSOR_KEYS_BY_ROOM.values() for s in keys]

#  Anomaly thresholds 
THRESHOLDS = {
    "bathroom_shower_water_temp": {"min": 35, "max": 50},
    "bathroom_temperature":       {"min": 18, "max": 30},
    "bedroom_temperature":        {"min": 16, "max": 28},
    "kitchen_temperature":        {"min": 18, "max": 35},
}

#  LLM 
LLM_HOST    = "http://localhost:11434"  # Ollama
LLM_MODEL   = "gemma3:latest"
MAX_TOKENS  = 512

#  Alerts 
ALERT_EMAIL = "caregiver@example.com"