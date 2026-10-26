"""
Calibrating the network simulator against reality.

The first working version of network_generator.py used SECTION_CAPACITY=1
and 45 background trains. That forced contention into existence, which
proved cross-train information is learnable — but it also produced a mean
delay increment of 61 minutes on a busy section, which is far worse than a
real double-line trunk route behaves. A result obtained from settings
chosen to force the effect is not a result you can quote.

This script sweeps the two contention knobs and scores each setting
against published punctuality behaviour for Indian Railways mail/express
services, then re-runs the with/without benchmark at the chosen setting to
confirm the learnable signal survives calibration.

REALISM TARGETS (mail/express, trunk routes):
  - most services finish within about half an hour of schedule
  - a meaningful minority, roughly one in five, finish more than an hour late
  - waiting for a section to clear typically costs minutes, not an hour

These are approximate behavioural targets, not a fitted distribution. The
aim is a simulator that is plausible, not one tuned until it flatters the
model.
"""

import numpy as np
import pandas as pd

from network_generator import run_simulation

# what a plausible trunk route should look like
TARGET_MEDIAN_FINAL_DELAY = (10, 40)    # minutes, median delay at journey end
TARGET_PCT_OVER_60 = (5, 25)            # percent of journeys finishing >1h late
TARGET_MEAN_SECTION_WAIT = (3, 20)      # minutes lost when a section is busy
TARGET_PCT_LEGS_CONTENDED = (2, 15)     # percent of legs that meet an occupied section

# (section capacity, background trains per day, hazard scale)
SWEEP = [
    (1, 45, 1.00),   # the original forced setting, kept for comparison
    (2, 25, 1.00),
    (2, 25, 0.50),
    (2, 25, 0.30),
    (2, 35, 0.30),
    (2, 25, 0.20),
    (2, 35, 0.20),
    (1, 25, 0.20),
    (2, 25, 0.12),
]
N_DAYS = 60   # shorter than the full run; enough to characterise behaviour


def characterise(df: pd.DataFrame) -> dict:
    finals = df.sort_values("seq").groupby(["train_no", "trip_id"])["actual_delay_min"].last()
    contended = df["occupants_ahead"] > 0
    waits = df.loc[contended & (df.primary_cause == "CROSSING_WAIT"), "delay_increment"]
    return dict(
        median_final=float(finals.median()),
        pct_over_60=float((finals > 60).mean() * 100),
        mean_wait=float(waits.mean()) if len(waits) else 0.0,
        pct_contended=float(contended.mean() * 100),
        n_contended=int(contended.sum()),
    )


def score(c: dict) -> tuple:
    """Counts how many realism targets a setting meets, and how far off it is."""
    checks = [
        (c["median_final"], TARGET_MEDIAN_FINAL_DELAY),
        (c["pct_over_60"], TARGET_PCT_OVER_60),
        (c["mean_wait"], TARGET_MEAN_SECTION_WAIT),
        (c["pct_contended"], TARGET_PCT_LEGS_CONTENDED),
    ]
    met = sum(1 for v, (lo, hi) in checks if lo <= v <= hi)
    # distance outside the band, normalised, for tie-breaking
    dist = 0.0
    for v, (lo, hi) in checks:
        if v < lo:   dist += (lo - v) / max(lo, 1)
        elif v > hi: dist += (v - hi) / max(hi, 1)
    return met, -dist


def main():
    print(f"Sweeping contention settings over {N_DAYS} simulated days each.\n")
    header = f"{'capacity':>8} {'background':>11} {'hazard':>7} {'med final':>10} {'>1h %':>7} {'sect wait':>10} {'contended %':>12} {'targets met':>12}"
    print(header)
    print("-" * len(header))

    results = []
    for cap, bg, hz in SWEEP:
        df = run_simulation(capacity=cap, n_background=bg, hazard=hz, n_days=N_DAYS)
        c = characterise(df)
        met, negdist = score(c)
        results.append((met, negdist, cap, bg, hz, c))
        print(f"{cap:>8} {bg:>11} {hz:>7.2f} {c['median_final']:>10.1f} {c['pct_over_60']:>7.1f} "
              f"{c['mean_wait']:>10.1f} {c['pct_contended']:>12.2f} {met:>9}/4")

    results.sort(key=lambda r: (r[0], r[1]), reverse=True)
    met, _, cap, bg, hz, c = results[0]
    print(f"\nBest fit: capacity {cap}, {bg} background trains per day, hazard scale {hz} "
          f"({met} of 4 realism targets met).")
    if c["n_contended"] < 200:
        print("WARNING: very few contended legs at this setting. The signal may be")
        print("too sparse to learn from even if the delays look realistic.")

    print("\nRe-running the with/without benchmark at the calibrated setting.")
    df = run_simulation(capacity=cap, n_background=bg, hazard=hz, n_days=300)
    df.to_csv("output/network_trips.csv", index=False)
    print(f"Wrote output/network_trips.csv ({len(df)} rows) at the calibrated setting.")
    print("Now run: python benchmark_network_features.py")
    print(f"\nIf the improvement holds, set SECTION_CAPACITY = {cap}, "
          f"BACKGROUND_TRAINS_PER_DAY = {bg} and HAZARD_SCALE = {hz} in network_generator.py.")


if __name__ == "__main__":
    main()