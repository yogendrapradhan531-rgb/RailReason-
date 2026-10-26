"""
What have we actually seen this train do?

Everything else in the app is a forecast. This is the opposite: a plain
readback of arrivals we recorded ourselves, with no model involved.

The value is that it is ours. Published punctuality figures are national
averages over a year; this is what these specific trains did on the days
we watched them, including the cause we attributed at the time. The cost
is that it is thin — a few days of collection gives a handful of
observations per station, and an average of two numbers is not a fact.

So every figure here carries its sample size, and anything below a
threshold is withheld rather than dressed up. A feature that says "we
have only seen this twice" is more use than one that quietly implies a
pattern that is not there.
"""

import json
import os
from collections import Counter, defaultdict
from statistics import median

LOG_PATH = "output/observations.jsonl"
MIN_FOR_STATION_SUMMARY = 3   # below this, a per-station average is noise
MIN_FOR_TRAIN_SUMMARY = 5


def _load():
    if not os.path.exists(LOG_PATH):
        return []
    rows = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def train_history(train_no):
    """What we have observed about one train, or a reason we cannot say."""
    rows = [r for r in _load() if str(r.get("train_no")) == str(train_no)]
    if not rows:
        return {"train_no": str(train_no), "observations": 0,
                "message": "We have not recorded this train yet. History builds "
                           "up as the app watches it run."}

    runs = {(r.get("run_date")) for r in rows}
    delays = [float(r.get("delay_min") or 0) for r in rows]

    out = {
        "train_no": str(train_no),
        "observations": len(rows),
        "runs_observed": len(runs),
        "first_seen": min(r.get("observed_at", "") for r in rows)[:10],
    }

    if len(rows) < MIN_FOR_TRAIN_SUMMARY:
        out["message"] = (f"Only {len(rows)} arrival"
                          f"{'s' if len(rows) != 1 else ''} recorded so far, "
                          f"too few to describe a pattern.")
        return out

    out["typical_delay_min"] = round(median(delays))
    out["worst_delay_min"] = round(max(delays))
    out["best_delay_min"] = round(min(delays))
    out["on_time_pct"] = round(100 * sum(1 for d in delays if d <= 5) / len(delays))

    # what has actually gone wrong, as attributed at the time
    causes = Counter(r.get("cause") for r in rows if r.get("cause") not in (None, "NONE"))
    out["causes"] = [{"cause": c, "count": n} for c, n in causes.most_common(4)]

    # per-station, but only where we have seen it enough times to mean anything
    by_station = defaultdict(list)
    for r in rows:
        by_station[(r.get("station_code"), r.get("station_name"))].append(
            float(r.get("delay_min") or 0))

    stations = []
    withheld = 0
    for (code, name), ds in by_station.items():
        if len(ds) < MIN_FOR_STATION_SUMMARY:
            withheld += 1
            continue
        stations.append({
            "station_code": code, "station_name": name, "times_seen": len(ds),
            "typical_delay_min": round(median(ds)), "worst_delay_min": round(max(ds)),
        })
    stations.sort(key=lambda s: -s["typical_delay_min"])
    out["stations"] = stations[:8]
    out["stations_withheld"] = withheld

    return out


def summarise(h):
    """One sentence, or an honest statement that we cannot give one."""
    if h.get("observations", 0) == 0 or "typical_delay_min" not in h:
        return h.get("message", "Not enough history yet.")

    typical = h["typical_delay_min"]
    runs = h["runs_observed"]
    basis = (f"across {h['observations']} arrivals we recorded"
             f"{f' over {runs} runs' if runs > 1 else ''}")

    if typical <= 5:
        return f"Usually close to time, {basis}."
    worst = h["worst_delay_min"]
    return (f"Typically about {typical} minutes late, {basis}. "
            f"The worst we have seen is {worst} minutes.")


def overall():
    """A short summary of the whole collection, for the dashboard footer."""
    rows = _load()
    if not rows:
        return {"observations": 0}
    return {
        "observations": len(rows),
        "trains": len({r.get("train_no") for r in rows}),
        "runs": len({(r.get("train_no"), r.get("run_date")) for r in rows}),
        "first_seen": min(r.get("observed_at", "") for r in rows)[:10],
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        h = train_history(sys.argv[1])
        print(json.dumps(h, indent=2))
        print("\n" + summarise(h))
    else:
        print(json.dumps(overall(), indent=2))
        print("\nPass a train number to see its history, e.g. python history.py 22691")
