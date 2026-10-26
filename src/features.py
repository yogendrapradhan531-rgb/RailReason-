"""
Turns raw causal trip simulation into a leg-level ML table.

Critical design choice: every feature must be KNOWABLE at the moment the
train departs the current station, i.e. BEFORE it reaches the next one.
We deliberately exclude halt_actual/run_actual of the target station from
the feature set (that would be leakage - you can't know the next
station's actual halt time before the train gets there). Those columns
are only used afterwards, retrospectively, by the attribution engine.

Two features added in this version:
  - route_position_fraction: how far into the total journey (by real
    distance) this leg is. Useful because delay dynamics differ near the
    start vs end of a long journey.
  - prev_cause: what caused the PREVIOUS leg's delay change. This is only
    a meaningful feature because generator.py now builds in realistic
    cause-to-cause correlation (e.g. a technical fault raises the chance
    of a recovery push next) — without that correlation, this feature
    would carry no real signal and just be cosmetic.
"""

import glob
import re
import pandas as pd
import numpy as np


def build_leg_table(trips: pd.DataFrame, sched: pd.DataFrame) -> pd.DataFrame:
    sched_by_code = sched.set_index("Station Code")
    total_distance = float(sched["Distance"].max())
    trips = trips.sort_values(["train_no", "trip_id", "seq"]).reset_index(drop=True)

    rows = []
    for (train_no, trip_id), g in trips.groupby(["train_no", "trip_id"]):
        g = g.reset_index(drop=True)
        for i in range(1, len(g)):
            cur = g.iloc[i - 1]
            nxt = g.iloc[i]

            window = g.iloc[max(0, i - 4):i]
            trend = 0.0
            if len(window) >= 2:
                trend = (window["actual_delay_min"].iloc[-1] - window["actual_delay_min"].iloc[0]) / len(window)

            nxt_distance = float(sched_by_code.loc[nxt.station_code, "Distance"])
            route_position_fraction = round(nxt_distance / total_distance, 3) if total_distance > 0 else 0.0

            rows.append(dict(
                train_no=train_no, trip_id=trip_id,
                seq_from=cur.seq, seq_to=nxt.seq,
                station_from=cur.station_code, station_to=nxt.station_code,
                day_of_week=nxt.day_of_week, month=nxt.month,
                hour_of_day=nxt.hour_of_day,
                current_delay=cur.actual_delay_min,
                cum_avg_delay_so_far=g.iloc[:i]["actual_delay_min"].mean(),
                delay_trend=round(trend, 2),
                stations_travelled=i,
                scheduled_run_min=sched_by_code.loc[nxt.station_code, "segment_travel_min"],
                scheduled_halt_min=sched_by_code.loc[nxt.station_code, "halt_min"],
                segment_distance_km=sched_by_code.loc[nxt.station_code, "segment_distance_km"],
                route_position_fraction=route_position_fraction,
                prev_cause=cur.primary_cause,
                is_fog_zone=bool(nxt.is_fog_zone),
                is_junction=bool(nxt.is_junction),
                is_winter=nxt.month in (11, 0, 1),
                is_night_or_early=nxt.hour_of_day <= 8 or nxt.hour_of_day >= 22,
                # targets (only known once the train actually arrives)
                target_delay_increment=nxt.delay_increment,
                target_cause=nxt.primary_cause,
                target_cum_delay=nxt.actual_delay_min,
            ))
    return pd.DataFrame.from_records(rows)


def discover_schedules():
    paths = sorted(glob.glob("data/route_*_schedule.csv"))
    routes = {}
    for path in paths:
        m = re.search(r"route_(\d+)_schedule\.csv$", path)
        if m:
            routes[int(m.group(1))] = path
    return routes


def main():
    trips = pd.read_csv("output/synthetic_trips_causal.csv")
    routes = discover_schedules()
    leg_rows = []
    for train_no, path in routes.items():
        sched = pd.read_csv(path)
        sub = trips[trips.train_no == train_no]
        if sub.empty:
            continue
        leg_rows.append(build_leg_table(sub, sched))
    table = pd.concat(leg_rows, ignore_index=True)
    table.to_csv("output/leg_features.csv", index=False)
    print(table.shape)
    print(table.head(3).T)


if __name__ == "__main__":
    main()