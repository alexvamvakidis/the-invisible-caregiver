# The Invisible Caregiver

A context-aware assisted living system for elderly residents. Sensor data from a smart home is collected via [ThingsBoard](https://thingsboard.io/), summarised, and fed to a local LLM ([Ollama](https://ollama.com/) / gemma3) that acts as a safety auditor and conversational assistant.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | |
| [Ollama](https://ollama.com/) running locally | `ollama pull gemma3` |
| Docker + Compose | only for the live ThingsBoard stack |

```bash
pip install requests paho-mqtt
```

---

## How the simulation works

Since there are no real sensors, the simulation pipeline produces realistic sensor data and pushes it into ThingsBoard so the rest of the system can consume it as if it were live.

There are three scenarios, each representing a different day in the resident's life:

| Scenario | What it models |
|---|---|
| `normal` | Healthy routine — regular meals, sleep, hygiene |
| `decline` | Subtle changes — skipped meals, reduced activity |
| `hazard` | Acute event — smoke detected, prolonged inactivity |

Each scenario is driven by an **event file** (`simulation/events/events_<name>.json`). An event is a simple record: at a given time of day, a sensor changes to a given value. Everything between events stays at a baseline, with small noise added to continuous sensors (temperature, humidity) to look realistic.

```
events_hazard.json  →  simulate.py  →  scenario_hazard.json  →  publish.py  →  ThingsBoard
```

**`simulate.py`** steps through a full 24-hour day in 5-second increments, applies the event changes on top of the baseline, and writes every reading to a scenario JSON file. This is purely local — no network needed.

**`publish.py`** reads that JSON and replays each timestamped reading to ThingsBoard over MQTT, one batch per time step. Each sensor authenticates with its own device token (stored in `config/all_sensors.csv`).

To create a new scenario, edit the events JSON and re-run `simulate.py`.

---

## Running the simulation

### Step 1 — Start ThingsBoard

```bash
cd simulation
docker compose up -d
```

Wait ~60 s, then open http://localhost:9090.  
Default credentials: `tenant@thingsboard.org` / `tenant`

### Step 2 — Create devices in ThingsBoard

Add devices from the left side menu Entities -> Devices, then click the plus icon top left and import the file: `config/all_sensors.csv`. In the model that opens, click Browse to find the file and upload, then click Continue. Click Continue in the Import Configuration also. In the collumns type, set the first as Name, second as Type and the  third as Label. Keep the rest the same and click Continue. The devices should be added succesfully.

### Step 3 — Fetch device tokens

```bash
cd simulation
python fetch_tokens.py
```

This logs into ThingsBoard, finds each device by name, and writes its access token into `config/sensor_tokens.csv`. Run this once after creating the devices.

### Step 4 — Generate a scenario

```bash
cd simulation
python simulate.py normal
python simulate.py decline
python simulate.py hazard
python simulate.py normal --date 2026-06-01   # specific date
```

Output is written to `simulation/scenarios/scenario_<name>.json`.

### Step 5 — Publish to ThingsBoard

```bash
cd simulation
python publish.py hazard
python publish.py hazard --interval 0   # indicates the step of each publication of data (0 -> all at once)
```

---

## Querying the data

Once data is in ThingsBoard, use the CLI to run the LLM on it:

```bash
# Safety audit on live ThingsBoard data (last hour)
python cli.py report

# Interactive Q&A on live data
python cli.py chat

# Use a local scenario file instead of ThingsBoard (no live stack needed)
python cli.py report --scenario hazard
python cli.py chat   --scenario decline

# Print the raw sensor summary
python cli.py summary --scenario normal

# Pull a longer window
python cli.py report --window-ms 7200000   # last 2 hours
```

---

## Verification

```bash
# Every event in the events file appears in the generated scenario
python verify.py events --scenario hazard

# Every event value appears in ThingsBoard telemetry (requires live TB)
python verify.py tb --scenario hazard

# Every sensor value matches its declared unit type
python verify.py types --scenario normal

# Run all three
python verify.py all --scenario decline
```

---

## Configuration

All constants are in `config/settings.py`.

| Setting | Default | Description |
|---|---|---|
| `TB_HOST` | `localhost` | ThingsBoard host |
| `TB_PORT_API` | `9090` | ThingsBoard HTTP API port |
| `TB_PORT_MQTT` | `1883` | ThingsBoard MQTT port |
| `LLM_HOST` | `http://localhost:11434` | Ollama base URL |
| `LLM_MODEL` | `gemma3:latest` | Model name |
| `HISTORY_WINDOW_MS` | `3_600_000` | Telemetry window pulled by the CLI (1 h) |
| `SIM_INTERVAL_SEC` | `5` | Seconds between simulation time steps |

## Ollama setup

```bash
ollama

ollama run <model>
```
