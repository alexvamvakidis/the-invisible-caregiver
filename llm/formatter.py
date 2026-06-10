"""
llm/formatter.py

Pre-processes the sensor summary dict into a human-readable text report.
All hazard detection, correlation, and time math is done HERE so the LLM
only needs to read and narrate — no reasoning required.
"""

from datetime import datetime, timezone


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(iso: str) -> str:
    """ISO timestamp → HH:MM string."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except Exception:
        return str(iso)


def _hour(iso: str) -> float:
    """ISO timestamp → hour as float (e.g. 3.5 = 03:30)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.hour + dt.minute / 60
    except Exception:
        return -1


def _duration(sec) -> str:
    """Seconds → human-readable string."""
    if sec is None:
        return "ongoing"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60} min"
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h}h {m}min" if m else f"{h}h"


def _is_night(iso: str) -> bool:
    """True if timestamp is between 00:00 and 06:00."""
    h = _hour(iso)
    return 0 <= h < 6


def _is_daytime(iso: str) -> bool:
    """True if timestamp is between 08:00 and 20:00."""
    h = _hour(iso)
    return 8 <= h < 20


def _true_events(sensor: dict) -> list:
    """Return all events where value is True."""
    return [e for e in sensor.get("events", [])
            if e.get("value") is True or e.get("value") == "true"]


def _false_events(sensor: dict) -> list:
    """Return all events where value is False."""
    return [e for e in sensor.get("events", [])
            if e.get("value") is False or e.get("value") == "false"]


def _any_motion_at(summary: dict, from_iso: str, to_hour: float) -> bool:
    """Check if any motion sensor fired between from_iso and to_hour."""
    from_h = _hour(from_iso)
    motion_sensors = [
        summary.get("kitchen", {}).get("kitchen_motion", {}),
        summary.get("bedroom", {}).get("bedroom_motion", {}),
        summary.get("bathroom", {}).get("bathroom_motion", {}),
        summary.get("living_room", {}).get("living_motion", {}),
        summary.get("entrance", {}).get("entrance_motion", {}),
    ]
    for sensor in motion_sensors:
        for ev in _true_events(sensor):
            h = _hour(ev["time"])
            if from_h <= h <= to_hour:
                return True
    return False


def _motion_gaps_daytime(summary: dict) -> list:
    """
    Find gaps >4h with no motion in ANY room during daytime (08:00-20:00).
    Excludes periods where person is known to be stationary (on sofa, in bed).
    Returns list of (gap_start_hour, gap_duration_sec).
    """
    # Collect all motion events across all rooms
    all_motion_times = []
    motion_sensors = [
        summary.get("kitchen", {}).get("kitchen_motion", {}),
        summary.get("bedroom", {}).get("bedroom_motion", {}),
        summary.get("bathroom", {}).get("bathroom_motion", {}),
        summary.get("living_room", {}).get("living_motion", {}),
        summary.get("entrance", {}).get("entrance_motion", {}),
    ]
    for sensor in motion_sensors:
        for ev in _true_events(sensor):
            h = _hour(ev["time"])
            if 8 <= h <= 20:
                all_motion_times.append(h)

    # Also treat sofa/bed occupancy as "known location" — not a gap
    stationary_sensors = [
        summary.get("living_room", {}).get("sofa_pressure", {}),
        summary.get("bedroom", {}).get("bed_pressure", {}),
    ]
    for sensor in stationary_sensors:
        for ev in _true_events(sensor):
            h_start = _hour(ev["time"])
            dur = ev.get("duration_sec", 0) or 0
            h_end = h_start + dur / 3600
            # Add activity markers every 30 min during occupancy
            h = max(h_start, 8.0)
            while h <= min(h_end, 20.0):
                all_motion_times.append(h)
                h += 0.5

    if not all_motion_times:
        return [(8, 12 * 3600)]  # No motion all daytime

    all_motion_times.sort()
    gaps = []
    prev = 8.0
    for h in all_motion_times:
        gap_h = h - prev
        if gap_h > 4:
            gaps.append((prev, int(gap_h * 3600)))
        prev = h
    # Check gap after last motion to end of daytime
    if 20 - prev > 4:
        gaps.append((prev, int((20 - prev) * 3600)))
    return gaps


def _bed_night_transitions(bed_sensor: dict) -> list:
    """Return all bed_pressure transitions between 22:00 and 07:00."""
    return [e for e in bed_sensor.get("events", [])
            if _hour(e["time"]) >= 22 or _hour(e["time"]) < 7]


# ── Main formatter ────────────────────────────────────────────────────────────

def format_for_llm(summary: dict) -> str:
    """
    Convert sensor summary into a pre-computed human-readable report.
    Hazard detection and correlation is done here — LLM just reads and narrates.
    """
    lines = []
    emergencies = []
    concerns = []
    mild = []

    kitchen   = summary.get("kitchen", {})
    bedroom   = summary.get("bedroom", {})
    bathroom  = summary.get("bathroom", {})
    living    = summary.get("living_room", {})
    entrance  = summary.get("entrance", {})

    # ══════════════════════════════════════════════════════════════
    # SECTION 1: FIRE & KITCHEN HAZARDS
    # ══════════════════════════════════════════════════════════════

    kt = kitchen.get("kitchen_temperature", {})
    kt_max = kt.get("max", 0)
    if kt_max > 40:
        emergencies.append(f"CRITICAL OVERHEATING: Kitchen temperature reached {kt_max}°C — fire risk")
    elif kt_max > 35:
        concerns.append(f"OVERHEATING: Kitchen temperature peaked at {kt_max}°C")

    # Smoke detector — ALWAYS critical
    smoke = kitchen.get("smoke_detector", {})
    for ev in _true_events(smoke):
        dur = _duration(ev.get("duration_sec"))
        emergencies.append(f"SMOKE ALARM FIRED at {_ts(ev['time'])} for {dur} — IMMEDIATE ACTION REQUIRED")

    # Stove analysis
    stove = kitchen.get("stove_power", {})
    km    = kitchen.get("kitchen_motion", {})
    km_true = _true_events(km)

    for ev in _true_events(stove):
        t   = _ts(ev["time"])
        dur = ev.get("duration_sec")

        if dur is None:
            concerns.append(f"STOVE still ON at end of day — turned on at {t}")
            continue

        dur_str = _duration(dur)
        on_hour = _hour(ev["time"])
        off_hour = on_hour + dur / 3600

        # Check if kitchen had motion during stove-on period
        motion_during = any(
            on_hour <= _hour(m["time"]) <= off_hour
            for m in km_true
        )

        # Check if person was in bed while stove was on
        bed = bedroom.get("bed_pressure", {})
        bed_during = any(
            (be.get("value") is True or be.get("value") == "true")
            and on_hour <= _hour(be["time"]) <= off_hour
            for be in bed.get("events", [])
        )

        # Check where person was
        living_motion_during = any(
            on_hour <= _hour(m["time"]) <= off_hour
            for m in _true_events(living.get("living_motion", {}))
        )
        bedroom_motion_during = any(
            on_hour <= _hour(m["time"]) <= off_hour
            for m in _true_events(bedroom.get("bedroom_motion", {}))
        )

        if dur > 4200:  # >70 min
            location = "in bed" if bed_during else ("in living room" if living_motion_during else "elsewhere in home")
            emergencies.append(
                f"STOVE LEFT ON for {dur_str} from {t} — person was {location} — FIRE RISK"
            )
        elif dur > 900:  # >15 min unattended
            if not motion_during:
                location = "in living room" if living_motion_during else ("in bedroom" if bedroom_motion_during else "elsewhere")
                concerns.append(
                    f"STOVE UNATTENDED for {dur_str} from {t} — person was {location} during this time"
                )

    # ══════════════════════════════════════════════════════════════
    # SECTION 2: NIGHT WANDERING
    # ══════════════════════════════════════════════════════════════

    door  = entrance.get("entrance_door", {})
    e_mot = entrance.get("entrance_motion", {})

    # Build a unified timeline of entrance events sorted by time
    # State machine: INSIDE → (door opens + motion leaves) → OUTSIDE → (motion returns) → INSIDE
    door_events   = sorted(door.get("events", []),  key=lambda e: _hour(e["time"]))
    motion_events = sorted(e_mot.get("events", []), key=lambda e: _hour(e["time"]))

    # Find night exit episodes using state machine
    # An episode = door opens at night + entrance_motion goes false (left) + entrance_motion goes true (returned)
    processed_return_hours = set()  # avoid double-counting

    for ev in _false_events(door):  # door opening
        if not _is_night(ev["time"]):
            continue

        door_h = _hour(ev["time"])

        # Find entrance_motion going false AFTER door opens (within 10 min) = person left
        motion_left = next(
            (m for m in _false_events(e_mot)
             if door_h - 0.05 <= _hour(m["time"]) <= door_h + 0.17),
            None
        )

        if not motion_left:
            # Door opened but motion didn't leave — person stayed inside
            mild.append(f"Front door opened at night at {_ts(ev['time'])} — person remained inside")
            continue

        left_h   = _hour(motion_left["time"])
        gone_sec = motion_left.get("duration_sec", 0) or 0

        # Skip if this return was already accounted for
        if left_h in processed_return_hours:
            continue
        processed_return_hours.add(left_h)

        # Find entrance_motion going true AFTER person left = person returned
        motion_return = next(
            (m for m in _true_events(e_mot)
             if _hour(m["time"]) > left_h),
            None
        )

        gone_min = gone_sec // 60
        leave_t  = _ts(motion_left["time"])
        return_t = _ts(motion_return["time"]) if motion_return else "unknown"

        # Skip false positives — person returning triggers motion, not leaving
        if gone_sec < 60:
            continue

        if gone_sec > 3600:  # >60 min outside
            emergencies.append(
                f"NIGHT WANDERING EMERGENCY: Person left home at {leave_t}, "
                f"was OUTSIDE for {gone_min} min, returned at {return_t}"
            )
        elif gone_sec > 1200:  # >20 min outside
            concerns.append(
                f"NIGHT EXIT: Person left home at {leave_t}, "
                f"outside for {gone_min} min, returned at {return_t}"
            )
        else:
            mild.append(
                f"Brief night exit at {leave_t}, outside ~{gone_min} min, returned at {return_t}"
            )

    # ══════════════════════════════════════════════════════════════
    # SECTION 3: SLEEP CONCERNS
    # ══════════════════════════════════════════════════════════════

    bed = bedroom.get("bed_pressure", {})
    bed_events = bed.get("events", [])
    bed_true  = _true_events(bed)
    bed_false = _false_events(bed)

    # Never went to bed
    night_in_bed = [e for e in bed_true if _hour(e["time"]) >= 21 or _hour(e["time"]) < 6]
    if not night_in_bed and bed_events:
        concerns.append("NEVER WENT TO BED: No bed occupancy detected during night hours")

    # Very restless sleep — count night transitions
    night_transitions = _bed_night_transitions(bed)
    if len(night_transitions) > 6:
        concerns.append(f"RESTLESS NIGHT: Bed pressure changed {len(night_transitions)} times during night hours")

    # Person location unknown at night
    for ev in bed_false:
        if _is_night(ev["time"]):
            dur = ev.get("duration_sec", 0) or 0
            if dur > 7200:  # >2h out of bed at night
                motion = _any_motion_at(summary, ev["time"], _hour(ev["time"]) + dur / 3600)
                if not motion:
                    concerns.append(
                        f"PERSON LOCATION UNKNOWN: Out of bed at {_ts(ev['time'])} "
                        f"for {_duration(dur)} at night with no motion detected anywhere"
                    )

    # Sleeping very late
    for ev in bed_true:
        if 10 <= _hour(ev["time"]) <= 14:
            mild.append(f"Still in bed at {_ts(ev['time'])} — sleeping unusually late")

    # Early wake, no return
    for ev in bed_false:
        if _hour(ev["time"]) < 5:
            dur = ev.get("duration_sec", 0) or 0
            if dur > 3600:
                concerns.append(
                    f"EARLY WAKE: Out of bed at {_ts(ev['time'])} "
                    f"for {_duration(dur)} — did not return to bed for over an hour"
                )

    # Bed occupied >14h during daytime
    for ev in bed_true:
        dur = ev.get("duration_sec", 0) or 0
        if _is_daytime(ev["time"]) and dur > 50400:
            concerns.append(f"IN BED ALL DAY: Bed occupied for {_duration(dur)} during daytime — person not getting up")

    # ══════════════════════════════════════════════════════════════
    # SECTION 4: NUTRITION & ROUTINE
    # ══════════════════════════════════════════════════════════════

    km_all = _true_events(km)
    fridge = kitchen.get("fridge_door", {})
    fridge_opens = _false_events(fridge)
    med = kitchen.get("kitchen_medication_cabinet", {})
    med_opens = _false_events(med)

    if not km_all:
        concerns.append("NO KITCHEN VISITS all day — person may not have eaten")
    else:
        morning_kitchen = [e for e in km_all if _hour(e["time"]) < 12]
        if not morning_kitchen:
            mild.append("No kitchen visit before noon — may have skipped breakfast")

        if len(km_all) == 1:
            mild.append(f"Only 1 kitchen visit today (at {_ts(km_all[0]['time'])}) — possible skipped meals")

    if not fridge_opens:
        concerns.append("FRIDGE NEVER OPENED — person may not have eaten all day")

    # In kitchen but no eating activity
    for ev in km_all:
        t = _ts(ev["time"])
        dur = ev.get("duration_sec", 0) or 0
        on_h = _hour(ev["time"])
        off_h = on_h + dur / 3600
        fridge_during = any(on_h <= _hour(f["time"]) <= off_h for f in fridge_opens)
        stove_during  = any(on_h <= _hour(s["time"]) <= off_h for s in _true_events(stove))
        if dur > 300 and not fridge_during and not stove_during:
            mild.append(f"In kitchen at {t} for {_duration(dur)} but no fridge or stove activity")

    if not med_opens:
        concerns.append("MEDICATION CABINET NEVER OPENED — possible missed medication")
    elif len(med_opens) > 3:
        mild.append(f"Medication cabinet opened {len(med_opens)} times — possible confusion with doses")

    # ══════════════════════════════════════════════════════════════
    # SECTION 5: HYGIENE
    # ══════════════════════════════════════════════════════════════

    bm    = bathroom.get("bathroom_motion", {})
    water = bathroom.get("bathroom_water_flow", {})
    toilet = bathroom.get("toilet_pressure", {})
    bwt   = bathroom.get("bathroom_shower_water_temp", {})

    if not _true_events(bm):
        mild.append("NO BATHROOM VISIT detected all day — very unusual")

    if not _true_events(toilet):
        mild.append("NO TOILET USE detected all day — very unusual")

    shower_events = _true_events(water)
    if not shower_events:
        mild.append("NO SHOWER detected today")
    else:
        for ev in shower_events:
            dur = ev.get("duration_sec", 0) or 0
            if dur > 2700:  # >45 min
                concerns.append(
                    f"UNUSUALLY LONG SHOWER: {_duration(dur)} at {_ts(ev['time'])} — possible fall in bathroom"
                )

    # Shower temperature — only meaningful if shower was actually used
    bwt_max = bwt.get("max", 0)
    bwt_min = bwt.get("min", 100)
    if shower_events:
        if bwt_max > 50:
            concerns.append(f"SHOWER TOO HOT: Water temperature reached {bwt_max}°C — scalding risk")
        if bwt_min < 30 and bwt_max > 30:
            # Only flag cold shower if temp actually varied (not just baseline room temp)
            mild.append(f"COLD SHOWER detected: water temperature as low as {bwt_min}°C — possible confusion")

    # ══════════════════════════════════════════════════════════════
    # SECTION 6: INACTIVITY & FALL RISK
    # ══════════════════════════════════════════════════════════════

    motion_gaps = _motion_gaps_daytime(summary)
    for gap_start, gap_sec in motion_gaps:
        h = int(gap_start)
        m = int((gap_start - h) * 60)
        gap_start_str = f"{h:02d}:{m:02d}"
        if gap_sec > 21600:  # >6h
            emergencies.append(
                f"NO MOTION ANYWHERE for {_duration(gap_sec)} starting {gap_start_str} during daytime — "
                f"possible fall or medical emergency"
            )
        elif gap_sec > 14400:  # >4h
            concerns.append(
                f"NO MOTION for {_duration(gap_sec)} starting {gap_start_str} during daytime — "
                f"possible fall or health issue"
            )

    # Possible fall in bathroom
    for ev in _true_events(bm):
        dur = ev.get("duration_sec", 0) or 0
        if dur > 1800:  # >30 min in bathroom with no other motion
            other_motion = any(
                _hour(ev["time"]) <= _hour(m["time"]) <= _hour(ev["time"]) + dur / 3600
                for sensor_key in ["kitchen_motion", "bedroom_motion", "living_motion"]
                for m in _true_events(kitchen.get(sensor_key, {}) if "kitchen" in sensor_key
                                      else bedroom.get(sensor_key, {}) if "bedroom" in sensor_key
                                      else living.get(sensor_key, {}))
            )
            if not other_motion:
                concerns.append(
                    f"POSSIBLE FALL IN BATHROOM: Motion detected for {_duration(dur)} "
                    f"at {_ts(ev['time'])} with no movement elsewhere"
                )

    # Possible fall in bedroom during daytime
    for ev in _true_events(bedroom.get("bedroom_motion", {})):
        if not _is_daytime(ev["time"]):
            continue
        dur = ev.get("duration_sec", 0) or 0
        if dur > 3600:  # >1h
            concerns.append(
                f"POSSIBLE FALL IN BEDROOM: Motion detected for {_duration(dur)} "
                f"at {_ts(ev['time'])} during daytime with no movement elsewhere"
            )

    # ══════════════════════════════════════════════════════════════
    # SECTION 7: ENVIRONMENT
    # ══════════════════════════════════════════════════════════════

    temp_sensors = {
        "Bathroom": bathroom.get("bathroom_temperature", {}),
        "Bedroom":  bedroom.get("bedroom_temperature", {}),
        "Kitchen":  kitchen.get("kitchen_temperature", {}),
    }
    for room_name, ts in temp_sensors.items():
        tmin = ts.get("min", 20)
        tmax = ts.get("max", 20)
        if tmin < 16:
            concerns.append(f"TOO COLD: {room_name} temperature dropped to {tmin}°C — hypothermia risk")
        if tmax > 30 and room_name != "Kitchen":
            concerns.append(f"TOO HOT: {room_name} temperature reached {tmax}°C — heatstroke risk")

    # ══════════════════════════════════════════════════════════════
    # BUILD THE REPORT
    # ══════════════════════════════════════════════════════════════

    report = []

    # Alerts header
    if emergencies:
        report.append("=" * 55)
        report.append("🚨 EMERGENCIES — IMMEDIATE ACTION REQUIRED:")
        for e in emergencies:
            report.append(f"  ❗ {e}")
        report.append("=" * 55)
        report.append("")

    if concerns:
        report.append("⚠ SERIOUS CONCERNS:")
        for c in concerns:
            report.append(f"  • {c}")
        report.append("")

    if mild:
        report.append("ℹ MILD CONCERNS / OBSERVATIONS:")
        for m in mild:
            report.append(f"  - {m}")
        report.append("")

    if not emergencies and not concerns and not mild:
        report.append("✅ NO CONCERNS DETECTED — Day appears normal")
        report.append("")

    # ── Daily activity summary ────────────────────────────────────
    report.append("─" * 55)
    report.append("DAILY ACTIVITY SUMMARY:")
    report.append("─" * 55)
    report.append("")

    # Sleep
    report.append("SLEEP:")
    if bed_false:
        first_wake = next((e for e in bed_false if _hour(e["time"]) > 4), None)
        if first_wake:
            report.append(f"  Woke up at: {_ts(first_wake['time'])}")
    last_bed = next((e for e in reversed(bed_true) if _hour(e["time"]) > 18), None)
    if last_bed:
        report.append(f"  Went to bed: {_ts(last_bed['time'])}")
    night_out = [e for e in bed_false if _is_night(e["time"])]
    for e in night_out:
        report.append(f"  Night disturbance: out of bed at {_ts(e['time'])} for {_duration(e.get('duration_sec'))}")
    report.append("")

    # Morning routine
    report.append("MORNING ROUTINE:")
    bm_morning = [e for e in _true_events(bm) if _hour(e["time"]) < 12]
    report.append(f"  Bathroom: {'Yes at ' + _ts(bm_morning[0]['time']) if bm_morning else 'Not detected'}")
    shower_morning = [e for e in shower_events if _hour(e["time"]) < 12]
    report.append(f"  Shower: {'Yes at ' + _ts(shower_morning[0]['time']) + ' for ' + _duration(shower_morning[0].get('duration_sec')) if shower_morning else 'Not detected'}")
    report.append(f"  Medication: {'Taken at ' + _ts(med_opens[0]['time']) if med_opens else '⚠ NOT TAKEN'}")
    report.append("")

    # Meals
    report.append("MEALS:")
    meal_times = {"Breakfast (06-10)": (6, 10), "Lunch (11-15)": (11, 15), "Dinner (17-21)": (17, 21)}
    for meal, (h_start, h_end) in meal_times.items():
        kitchen_visit = any(h_start <= _hour(e["time"]) <= h_end for e in km_all)
        fridge_visit  = any(h_start <= _hour(e["time"]) <= h_end for e in fridge_opens)
        stove_visit   = any(h_start <= _hour(e["time"]) <= h_end for e in _true_events(stove))
        if kitchen_visit or fridge_visit:
            details = []
            if fridge_visit:  details.append("fridge opened")
            if stove_visit:   details.append("cooked")
            report.append(f"  {meal}: ✓ ({', '.join(details) if details else 'kitchen visited'})")
        else:
            report.append(f"  {meal}: ✗ Not detected")
    report.append("")

    # Kitchen details
    report.append("KITCHEN:")
    report.append(f"  Temperature: min={kt.get('min')}°C  max={kt.get('max')}°C  avg={kt.get('avg')}°C")
    stove_true = _true_events(stove)
    if stove_true:
        for ev in stove_true:
            report.append(f"  Stove ON: {_ts(ev['time'])} for {_duration(ev.get('duration_sec'))}")
    else:
        report.append("  Stove: Not used")
    smoke_true = _true_events(smoke)
    if smoke_true:
        for ev in smoke_true:
            report.append(f"  ❗ Smoke alarm: {_ts(ev['time'])} for {_duration(ev.get('duration_sec'))}")
    report.append("")

    # Living room / leisure
    report.append("LEISURE:")
    tv_on = _true_events(living.get("tv_plug", {}))
    if tv_on:
        total_tv = sum(e.get("duration_sec") or 0 for e in tv_on)
        report.append(f"  TV: {_duration(total_tv)} total")
    else:
        report.append("  TV: Not used")
    sofa_on = _true_events(living.get("sofa_pressure", {}))
    if sofa_on:
        total_sofa = sum(e.get("duration_sec") or 0 for e in sofa_on)
        report.append(f"  Sofa: {_duration(total_sofa)} total")
    report.append("")

    # Entrance — show exit episodes, not raw door events
    report.append("EXITS:")
    day_exits = [e for e in _false_events(door) if not _is_night(e["time"])]
    night_exit_episodes = []

    # Rebuild night exit episodes for summary (same logic as above)
    seen = set()
    for ev in _false_events(door):
        if not _is_night(ev["time"]):
            continue
        door_h = _hour(ev["time"])
        motion_left = next(
            (m for m in _false_events(e_mot)
             if door_h - 0.05 <= _hour(m["time"]) <= door_h + 0.17),
            None
        )
        if not motion_left:
            continue
        left_h = _hour(motion_left["time"])
        if left_h in seen:
            continue
        seen.add(left_h)
        gone_sec = motion_left.get("duration_sec", 0) or 0
        if gone_sec < 60:
            continue
        motion_return = next(
            (m for m in _true_events(e_mot) if _hour(m["time"]) > left_h), None
        )
        night_exit_episodes.append({
            "leave": _ts(motion_left["time"]),
            "return": _ts(motion_return["time"]) if motion_return else "unknown",
            "duration": gone_sec
        })

    if day_exits:
        for e in day_exits:
            report.append(f"  Daytime exit at {_ts(e['time'])} for {_duration(e.get('duration_sec'))}")
    for ep in night_exit_episodes:
        report.append(f"  ⚠ Night exit: left at {ep['leave']}, outside for {_duration(ep['duration'])}, returned at {ep['return']}")
    if not day_exits and not night_exit_episodes:
        report.append("  No exits detected today")
    report.append("")

    return "\n".join(report)