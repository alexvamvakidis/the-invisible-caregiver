# The Invisible Caregiver

Smart home sensor monitoring for elderly residents. A local LLM reads sensor data from ThingsBoard and answers a caretaker's questions via the terminal.

---

## How it works

```
Sensors (simulated)
      │  MQTT
      ▼
 ThingsBoard          ← stores all sensor readings
      │  REST API
      ▼
 data/collector.py    ← pulls the last 1h or 24h of readings and summarises them
      │
      ▼
 llm/client.py        ← sends the summary to Ollama (local LLM)
      │
      ├─ Safety Auditor (report) → structured JSON alert: severity + bullet points
      └─ Narrator      (chat)   → conversational answers to caretaker questions
             │
             └─ cli.py   — terminal interface
```

Scenario mode bypasses ThingsBoard entirely — `cli.py` reads pre-generated per-sensor JSON files from `simulation/sensor_data/` and feeds the same summarised format to the LLM.

---

## What you need

- Python 3.11+
- Docker (for ThingsBoard)
- Ollama (local LLM)

---

## Step 1 — Install Python dependencies

```bash
pip install -r requirements.txt
```

---

## Step 2 — Install and start Ollama

**Linux / macOS**
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

**Windows** (run in PowerShell):
```powershell
irm https://ollama.com/install.ps1 | iex
```

Pull the model (~5 GB):
```bash
ollama pull gemma3:latest
```

---

## Step 3 — Start ThingsBoard

```bash
docker compose -f simulation/docker-compose.yml up -d
```

Wait ~60 seconds, then open [http://localhost:8080](http://localhost:8080).  
Login: `tenant@thingsboard.org` / `tenant`

---

## Step 4 — Import devices

1. Go to **Entities → Devices** → click **+** → **Import devices**
2. Upload `config/all_sensors.csv`
3. Map: Column 1 → Name, Column 2 → Type, Column 3 → Label → **Import**

---

## Step 5 — Fetch device tokens

```bash
python simulation/fetch_tokens.py
```

This writes `config/sensor_tokens.csv`. Run once after importing devices.

---

## Step 6 — Generate and publish a scenario

```bash
python simulation/simulate.py hazard
python simulation/publish.py hazard
```

`publish.py` accepts optional flags:

| Flag | Description |
|---|---|
| `--interval N` | Pause N seconds between batches (default: 0 — bulk upload) |
| `--dry-run` | Print readings without sending to ThingsBoard |

| Scenario | What it simulates |
|---|---|
| `normal` | Healthy day — regular wake-up, medication, meals, sleep |
| `decline` | Subtle problems — late wake-up, missed meds, sedentary 7+ h |
| `hazard` | Acute danger — stove on 85 min, smoke alarm, 3 AM door exit |

---

## Terminal usage

```bash
# Safety audit — live ThingsBoard data (last 1 hour)
python cli.py report

# Safety audit on a local scenario (no ThingsBoard needed)
python cli.py report --scenario hazard
python cli.py report --scenario decline
python cli.py report --scenario normal

# Interactive Q&A — live ThingsBoard data (last 24 hours)
python cli.py chat

# Interactive Q&A on a local scenario
python cli.py chat --scenario decline

# Load a pre-fetched JSON file instead of hitting ThingsBoard or a scenario
python cli.py report --file fetch_hour.json
python cli.py chat   --file fetch_day.json
```

**Example output:**
```
$ python cli.py report --scenario hazard

[ALERT] severity=critical
--------------------------
  • Smoke detector activated at 20:00
  • Stove on unattended for 85 minutes (18:35–20:00)
  • Nighttime door exit at 03:00 (41 min outside)
```

### All CLI commands

| Command | Description |
|---|---|
| `report` | Safety Auditor — structured alert for the last hour |
| `chat` | Narrator — interactive Q&A about the last 24 hours |
| `fetch` | Pull live ThingsBoard data and write JSON files for offline use |

### `fetch` — save ThingsBoard data for offline use

```bash
# Fetch both windows (writes fetch_hour.json and fetch_day.json)
python cli.py fetch

# Fetch only one window
python cli.py fetch --hour
python cli.py fetch --day

# Custom output paths
python cli.py fetch --hour --out-hour h.json --day --out-day d.json
```

### Common flags

| Flag | Applies to | Description |
|---|---|---|
| `--scenario normal\|decline\|hazard` | report, chat | Use a local scenario file instead of ThingsBoard |
| `--file FILE` | report, chat | Load a JSON summary file directly |
| `--window-ms N` | report, chat | Override the telemetry window in milliseconds |
| `--raw` | chat | Print raw LLM output before parsing (debugging) |

---

## Configuration

All settings are in `config/settings.py`.

| Setting | Default | Description |
|---|---|---|
| `TB_HOST` | `localhost` | ThingsBoard host |
| `TB_PORT_API` | `8080` | ThingsBoard HTTP port |
| `LLM_HOST` | `http://localhost:11434` | Ollama URL |
| `LLM_MODEL` | `gemma3:latest` | Model name |
