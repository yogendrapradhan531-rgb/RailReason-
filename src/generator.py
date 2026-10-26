"""
Causal synthetic trip generator for railway delay simulation.

Unlike a naive random-walk generator, every minute of delay added here is
attributed to one of seven real-world causes:

    HALT_OVERRUN   - train sits at a station longer than scheduled
    SECTION_SLOW   - train runs a segment slower than scheduled (speed
                     restriction / track work / single-line congestion)
    CROSSING_WAIT  - unscheduled stop outside a station waiting for another
                     train to pass / platform to free up (precedence)
    FOG            - visibility-driven slowdown on fog-prone segments
                     during winter night/early-morning hours
    CASCADING      - train originates late from its source station and
                     carries that delay forward
    TECHNICAL      - rare large single-event delay (signal failure /
                     loco trouble / rake issue)
    RECOVERY       - negative delay contribution: train claws back time on
                     padded / less congested stretches

Ground-truth cause + magnitude is stored per (trip, station) so we can
later check the rule-based attribution engine against something real,
and so the prediction model has genuine causal signal to learn from.

Two things worth knowing about this version:
  - Junction/fog-zone detection now reuses forecast_engine.py's
    identify_junctions_and_fog_zone() instead of a separate, older,
    dwell-time-only heuristic. That old heuristic silently mis-detected
    junctions on fast trains (Rajdhanis etc. have short halts even at
    real junctions) — using the same fixed logic here means the
    TRAINING data now matches the LIVE forecasting logic.
  - Consecutive legs' causes are now weakly correlated (a previous
    TECHNICAL fault raises the chance of a RECOVERY push next; a
    previous SECTION_SLOW raises the chance of another one, since track
    conditions tend to persist locally; etc). Real delays cluster — a
    naive independent-per-leg model was leaving that signal on the
    table, and it's also what makes a "previous cause" feature
    meaningful to a classifier rather than cosmetic.
  - Schedule CSVs are now auto-discovered from data/route_*_schedule.csv
    instead of a hardcoded list — run fetch_schedule.py for any new
    train first, and it's automatically included here.
"""

import glob
import re
import pandas as pd
import numpy as np
from forecast_engine import identify_junctions_and_fog_zone

RNG_SEED = 42


def load_schedule(path):
    return pd.read_csv(path)


def simulate_route(sched, train_no, n_trips, rng):
    junctions, fog_zone = identify_junctions_and_fog_zone(sched)
    stations = sched["Station Code"].tolist()
    seqs = sched["SEQ"].tolist()
    sched_arr = dict(zip(sched["Station Code"], sched["arr_min"]))
    halt_plan = dict(zip(sched["Station Code"], sched["halt_min"].fillna(0)))
    run_plan = dict(zip(sched["Station Code"], sched["segment_travel_min"].fillna(0)))
    seg_dist = dict(zip(sched["Station Code"], sched["segment_distance_km"].fillna(0)))

    records = []

    for trip_id in range(n_trips):
        day_of_week = int(rng.integers(0, 7))
        month = int(rng.integers(0, 12))
        is_winter = month in (11, 0, 1)

        origin_delay = 0.0
        if rng.random() < 0.30:
            origin_delay = float(rng.gamma(shape=2.0, scale=8.0))

        cum_delay = origin_delay
        cum_min = sched_arr[stations[0]]
        prev_cause = "CASCADING" if origin_delay > 0.5 else "NONE"

        for idx, (seq, code) in enumerate(zip(seqs, stations)):
            if idx == 0:
                records.append(dict(
                    train_no=train_no, trip_id=trip_id, seq=seq, station_code=code,
                    day_of_week=day_of_week, month=month,
                    hour_of_day=int((cum_min // 60) % 24),
                    scheduled_arr_min=sched_arr[code],
                    halt_scheduled=halt_plan[code], halt_actual=halt_plan[code],
                    run_scheduled=0.0, run_actual=0.0,
                    delay_increment=round(origin_delay, 1) if origin_delay else 0.0,
                    actual_delay_min=round(cum_delay, 1),
                    primary_cause=prev_cause,
                    is_fog_zone=code in fog_zone, is_junction=code in junctions,
                ))
                continue

            hour = int((cum_min // 60) % 24)
            is_fog_hours = hour <= 8 or hour >= 22
            in_fog_zone = code in fog_zone
            is_junction = code in junctions

            causes = {}

            # --- SECTION_SLOW: baseline + persistence if the previous leg was also slow
            p_slow = 0.06 + 0.02 * min(seg_dist.get(code, 0) / 20, 1.5)
            if prev_cause == "SECTION_SLOW":
                p_slow += 0.08  # track conditions/speed restrictions tend to persist locally
            if rng.random() < p_slow:
                mag = float(rng.gamma(shape=1.6, scale=6.0))
                causes["SECTION_SLOW"] = mag

            # --- FOG: only in winter, only in fog corridor, only at fog hours;
            #     persistence if the previous leg was also foggy (fog covers a corridor, not one station)
            if is_winter and in_fog_zone and is_fog_hours:
                p_fog = 0.55 + (0.15 if prev_cause == "FOG" else 0.0)
                if rng.random() < p_fog:
                    mag = float(rng.gamma(shape=2.2, scale=9.0))
                    causes["FOG"] = causes.get("FOG", 0) + mag

            # --- CROSSING_WAIT: baseline + junction + current delay + persistence
            #     if the previous leg was a halt overrun (buffer already eaten, more
            #     likely to collide with other traffic) or another crossing wait
            p_cross = (0.10 if is_junction else 0.03) + min(cum_delay / 200, 0.15)
            if prev_cause == "HALT_OVERRUN":
                p_cross += 0.05
            if prev_cause == "CROSSING_WAIT":
                p_cross += 0.05
            if rng.random() < p_cross:
                mag = float(rng.gamma(shape=1.4, scale=5.0))
                causes["CROSSING_WAIT"] = causes.get("CROSSING_WAIT", 0) + mag

            # --- HALT_OVERRUN: only possible where a halt is scheduled; junctions overrun more
            scheduled_halt = halt_plan[code]
            halt_actual = scheduled_halt
            if scheduled_halt > 0:
                p_overrun = 0.18 if is_junction else 0.10
                if rng.random() < p_overrun:
                    mag = float(rng.gamma(shape=1.3, scale=4.0))
                    causes["HALT_OVERRUN"] = mag
                    halt_actual = scheduled_halt + mag

            # --- TECHNICAL: rare, large, independent event
            if rng.random() < 0.006:
                mag = float(rng.uniform(20, 75))
                causes["TECHNICAL"] = causes.get("TECHNICAL", 0) + mag

            # --- RECOVERY: baseline + a strong push if the previous leg had a big
            #     event (crew tries to claw back schedule), + mild momentum if
            #     already recovering
            if cum_delay > 2 and not causes:
                p_recovery = 0.35
                if prev_cause == "TECHNICAL":
                    p_recovery += 0.25
                elif prev_cause in ("HALT_OVERRUN", "SECTION_SLOW", "CROSSING_WAIT"):
                    p_recovery += 0.10
                elif prev_cause == "RECOVERY":
                    p_recovery += 0.10
                if rng.random() < p_recovery:
                    mag = -float(rng.gamma(shape=1.2, scale=3.5))
                    mag = max(mag, -cum_delay)
                    causes["RECOVERY"] = mag

            delay_increment = sum(causes.values())
            cum_delay = max(0.0, cum_delay + delay_increment)

            run_actual = run_plan[code] + causes.get("SECTION_SLOW", 0) + causes.get("FOG", 0) + causes.get("CROSSING_WAIT", 0)

            if causes:
                primary_cause = max(causes.items(), key=lambda kv: abs(kv[1]))[0]
            else:
                primary_cause = "NONE"

            cum_min = sched_arr[code] + cum_delay

            records.append(dict(
                train_no=train_no, trip_id=trip_id, seq=seq, station_code=code,
                day_of_week=day_of_week, month=month, hour_of_day=hour,
                scheduled_arr_min=sched_arr[code],
                halt_scheduled=scheduled_halt, halt_actual=round(halt_actual, 1),
                run_scheduled=run_plan[code], run_actual=round(run_actual, 1),
                delay_increment=round(delay_increment, 1),
                actual_delay_min=round(cum_delay, 1),
                primary_cause=primary_cause,
                is_fog_zone=in_fog_zone, is_junction=is_junction,
            ))

            prev_cause = primary_cause

    return pd.DataFrame.from_records(records)


def discover_schedules():
    """Finds every data/route_*_schedule.csv and extracts its train number."""
    paths = sorted(glob.glob("data/route_*_schedule.csv"))
    routes = {}
    for path in paths:
        m = re.search(r"route_(\d+)_schedule\.csv$", path)
        if m:
            routes[int(m.group(1))] = path
    return routes


def main():
    rng = np.random.default_rng(RNG_SEED)
    routes = discover_schedules()
    if not routes:
        print("No schedule CSVs found in data/ (expected data/route_<no>_schedule.csv). Nothing to do.")
        return

    print(f"Found {len(routes)} schedule(s): {sorted(routes.keys())}")
    all_trips = []
    for train_no, path in routes.items():
        sched = load_schedule(path)
        trips = simulate_route(sched, train_no, n_trips=300, rng=rng)
        all_trips.append(trips)
        print(f"train {train_no}: {trips.trip_id.nunique()} trips, {len(trips)} rows")

    out = pd.concat(all_trips, ignore_index=True)
    out.to_csv("output/synthetic_trips_causal.csv", index=False)
    print("\nCause distribution:")
    print(out["primary_cause"].value_counts())
    print("\nSaved -> output/synthetic_trips_causal.csv")


if __name__ == "__main__":
    main()