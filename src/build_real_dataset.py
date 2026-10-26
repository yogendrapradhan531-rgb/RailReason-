"""
Turning recorded reality into training data.

observation_log.py accumulates real station arrivals. This converts that
log into the same shape generator.py produces, so features.py and model.py
can consume it without modification — the only change is that the numbers
are measured rather than invented.

    python build_real_dataset.py          # check what has been collected
    python build_real_dataset.py --build  # write output/real_trips.csv

Then, to train on it:
    copy output\\real_trips.csv output\\synthetic_trips_causal.csv
    python features.py
    python model.py

HOW MUCH IS ENOUGH: the synthetic set has around 50,000 legs. Real data
does not need to match that to be worth using, because it carries no
made-up assumptions, but a few thousand legs spanning different times of
day and weather is a reasonable floor. Below roughly a thousand the rarer
causes will barely appear and the model will mostly learn "nothing
happens". This script says plainly where the log currently stands.
"""

import json
import os
import sys
import pandas as pd

from observation_log import LOG_PATH

MIN_USEFUL_ROWS = 1000
OUT_PATH = "output/real_trips.csv"


def load_rows():
    if not os.path.exists(LOG_PATH):
        return []
    rows = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows


def report(rows):
    if not rows:
        print("No observations recorded yet.")
        print("Leave server.py running while trains are in motion and the log fills up.")
        return

    df = pd.DataFrame(rows)
    print(f"Recorded rows            : {len(df)}")
    print(f"Distinct trains          : {df.train_no.nunique()}")
    print(f"Distinct runs            : {df.groupby(['train_no', 'run_date']).ngroups}")
    print(f"First observation        : {df.observed_at.min()}")
    print(f"Last observation         : {df.observed_at.max()}")

    print("\nCauses observed:")
    for cause, n in df.cause.value_counts().items():
        print(f"  {str(cause):16s} {n:6d}   {n/len(df)*100:5.1f}%")

    if "is_foggy" in df:
        fog = df.is_foggy.fillna(False)
        print(f"\nArrivals with real fog recorded: {int(fog.sum())}")

    print()
    if len(df) < MIN_USEFUL_ROWS:
        need = MIN_USEFUL_ROWS - len(df)
        print(f"About {need} more rows before this is worth training on.")
        print("Rarer causes such as technical faults need time to appear at all.")
    else:
        print("There is enough here to try training on real data.")
        print("Run with --build to write the training file.")


def build(rows):
    df = pd.DataFrame(rows)
    if df.empty:
        print("Nothing to build from.")
        return

    # one "trip" is one run of one train, matching generator.py's structure
    df["trip_id"] = df.groupby(["train_no", "run_date"]).ngroup()
    df = df.sort_values(["train_no", "trip_id", "seq"]).reset_index(drop=True)

    out = pd.DataFrame({
        "train_no": df.train_no,
        "trip_id": df.trip_id,
        "seq": df.seq,
        "station_code": df.station_code,
        "day_of_week": pd.to_datetime(df.run_date, errors="coerce").dt.dayofweek.fillna(0).astype(int),
        # months are zero-indexed here to match the generator's convention
        "month": (pd.to_datetime(df.run_date, errors="coerce").dt.month.fillna(1).astype(int) - 1),
        "hour_of_day": pd.to_datetime(df.raw_actual_arrival, errors="coerce", utc=True)
                         .dt.tz_convert("Asia/Kolkata").dt.hour.fillna(12).astype(int),
        "scheduled_arr_min": 0,   # filled below, per run
        "halt_scheduled": df.halt_scheduled.fillna(0),
        "halt_actual": df.halt_actual.fillna(0),
        "run_scheduled": df.run_scheduled.fillna(0),
        "run_actual": df.run_actual.fillna(0),
        "actual_delay_min": df.delay_min.fillna(0),
        "primary_cause": df.cause.fillna("NONE"),
        "is_fog_zone": df.is_fog_zone.fillna(False),
        "is_junction": df.is_junction.fillna(False),
    })

    # scheduled arrival in minutes from each run's own start
    sched = pd.to_datetime(df.raw_scheduled_arrival, errors="coerce", utc=True)
    base = sched.groupby(df.trip_id).transform("min")
    out["scheduled_arr_min"] = ((sched - base).dt.total_seconds() / 60).fillna(0).round().astype(int)

    # delay change from one station to the next, within a run
    out["delay_increment"] = (out.groupby("trip_id")["actual_delay_min"]
                                .diff().fillna(out["actual_delay_min"]).round(1))

    out.to_csv(OUT_PATH, index=False)
    print(f"Wrote {OUT_PATH}  ({len(out)} legs, {out.trip_id.nunique()} runs)")
    print("\nTo train on it:")
    print(f"  copy output\\synthetic_trips_causal.csv output\\synthetic_trips_backup.csv")
    print(f"  copy {OUT_PATH.replace('/', chr(92))} output\\synthetic_trips_causal.csv")
    print("  python features.py")
    print("  python model.py")
    print("\nKeep the backup. Real data will be far smaller at first, and a model")
    print("trained on a few hundred legs may well be worse than the simulated one.")
    print("Compare the metrics before deciding which to keep.")


if __name__ == "__main__":
    rows = load_rows()
    if "--build" in sys.argv:
        build(rows)
    else:
        report(rows)