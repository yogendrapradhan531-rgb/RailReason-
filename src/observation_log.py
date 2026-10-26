"""
Recording what actually happened, so the models can eventually stop
learning from a simulator.

Everything in this project that predicts is trained on generator.py, a
synthetic simulator. Its hazard rates are my invention, informed by how
railways behave but not measured from any real record. That is the single
largest limitation of the whole system.

This module fixes that slowly. Every refresh cycle observes real trains at
real stations, with real delays, real weather and real junction occupancy.
Thrown away, that information is lost. Appended to a file, it becomes a
genuine historical dataset, and after a few weeks of running it can
replace the simulator as training data.

Design notes:

  - Append-only JSONL. One JSON object per line, so a malformed or partial
    write costs one row rather than the file, and new fields can be added
    later without rewriting history.

  - Deduplicated on (train number, run date, station sequence). The server
    refreshes every five minutes and a station stays "completed" for the
    rest of the journey, so without this the same arrival would be written
    hundreds of times and the file would be mostly repetition.

  - Raw timestamps are stored alongside our derived values. If we later
    find a bug in how a delay was computed, the original RailRadar fields
    are still there and history can be recomputed rather than discarded.

  - run_date matters because a long-distance train has several runs in
    flight at once. Without it, yesterday's Kota arrival and today's would
    collide on the same key.
"""

import json
import os
from datetime import datetime

LOG_PATH = "output/observations.jsonl"
SEEN_PATH = "output/observations_seen.json"

_seen = set()


def _load_seen():
    """Keys already written, so restarts do not duplicate history."""
    global _seen
    if not os.path.exists(SEEN_PATH):
        return
    try:
        with open(SEEN_PATH, encoding="utf-8") as f:
            _seen = set(tuple(k) for k in json.load(f))
    except Exception as e:
        print(f"  observation log: could not read {SEEN_PATH} ({e}); "
              f"starting fresh, duplicates may appear")
        _seen = set()


def _save_seen():
    try:
        os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump([list(k) for k in _seen], f)
    except Exception as e:
        print(f"  observation log: could not write {SEEN_PATH} ({e})")


def record_arrival(train_no, run_date, station_entry, raw_stop, context):
    """
    Records one station arrival, once.

    station_entry  our processed view (cause, delays, clocks)
    raw_stop       the untouched RailRadar route entry
    context        weather, junction and occupancy at the time of observation
    """
    key = (str(train_no), str(run_date), int(station_entry["seq"]))
    if key in _seen:
        return False

    row = {
        "observed_at": datetime.now().isoformat(),
        "train_no": str(train_no),
        "run_date": str(run_date),
        "seq": int(station_entry["seq"]),
        "station_code": station_entry["station_code"],
        "station_name": station_entry["station_name"],
        "distance_km": station_entry.get("distance_km"),

        # what we derived
        "delay_min": station_entry.get("actual_delay_min"),
        "cause": station_entry.get("cause"),
        "explanation": station_entry.get("explanation"),

        # what RailRadar actually said, kept raw so history can be recomputed
        "raw_scheduled_arrival": raw_stop.get("scheduledArrival"),
        "raw_scheduled_departure": raw_stop.get("scheduledDeparture"),
        "raw_actual_arrival": raw_stop.get("actualArrival"),
        "raw_actual_departure": raw_stop.get("actualDeparture"),
        "raw_delay_arrival": raw_stop.get("delayArrival"),
        "raw_delay_departure": raw_stop.get("delayDeparture"),

        # timing context, the inputs the attribution engine reasons from
        "halt_scheduled": context.get("halt_scheduled"),
        "halt_actual": context.get("halt_actual"),
        "run_scheduled": context.get("run_scheduled"),
        "run_actual": context.get("run_actual"),
        "is_junction": context.get("is_junction"),
        "is_fog_zone": context.get("is_fog_zone"),

        # real conditions, the part a simulator can never supply honestly
        "weather_condition": context.get("weather_condition"),
        "weather_description": context.get("weather_description"),
        "visibility_km": context.get("visibility_km"),
        "is_foggy": context.get("is_foggy"),
        "temp_c": context.get("temp_c"),

        # real network state, when we paid for a station-board lookup
        "occupants_ahead": context.get("occupants_ahead"),
        "occupant_max_delay": context.get("occupant_max_delay"),
    }

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"  observation log: could not append ({e})")
        return False

    _seen.add(key)
    _save_seen()
    return True


def stats():
    """How much real history has been collected so far."""
    if not os.path.exists(LOG_PATH):
        return {"rows": 0, "message": "No observations recorded yet."}

    rows, trains, dates, causes = 0, set(), set(), {}
    first = last = None
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            rows += 1
            trains.add(r.get("train_no"))
            dates.add(r.get("run_date"))
            c = r.get("cause")
            causes[c] = causes.get(c, 0) + 1
            ts = r.get("observed_at")
            if ts:
                first = min(first, ts) if first else ts
                last = max(last, ts) if last else ts

    return {
        "rows": rows,
        "trains": len(trains),
        "run_dates": len(dates),
        "first_observed": first,
        "last_observed": last,
        "causes": dict(sorted(causes.items(), key=lambda kv: -kv[1])),
    }


_load_seen()


if __name__ == "__main__":
    s = stats()
    print(json.dumps(s, indent=2))
    if s["rows"]:
        print(f"\nRoughly {s['rows']} real station arrivals recorded.")
        print("A few thousand rows across varied weather and times of day would")
        print("be enough to retrain on measured behaviour rather than simulated.")
        print("Run build_real_dataset.py to convert this into training data.")