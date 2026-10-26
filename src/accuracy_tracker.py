"""
Self-validating prediction accuracy tracker.

Every time a station is in "predicted" state, we remember what we
predicted for it (P10/P50/P90). Once that station later shows up as
"completed" (the train actually reached it), we compare the prediction
against the real outcome and log it. Over time this builds a real,
honest record of how accurate the model actually is — not a claim, a
measurement.

Storage: a simple append-only JSONL file (one JSON object per line) —
no database needed for this scale. Safe to inspect/delete manually.
"""

import json
import os
from datetime import datetime

LOG_PATH = "output/prediction_accuracy_log.jsonl"
PENDING_PATH = "output/pending_predictions.json"

# In-memory: train_no -> { seq -> last-seen prediction dict }
# Updated every refresh cycle for stations still "predicted". When a
# station transitions to "completed", we compare against whatever we
# most recently had here before it completed.
_pending_predictions = {}


def _load_pending():
    """
    Pending predictions must survive a restart. The server is deliberately
    started and stopped to conserve API budget, and a prediction made on
    Monday is usually only settled by an arrival observed on Tuesday. Held
    purely in memory, almost every prediction would be discarded before it
    could be scored, and a multi-day accuracy run would quietly collect
    nothing.
    """
    global _pending_predictions
    if not os.path.exists(PENDING_PATH):
        return
    try:
        with open(PENDING_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        # JSON object keys are strings; sequence numbers must come back as ints
        _pending_predictions = {t: {int(seq): v for seq, v in stops.items()}
                                for t, stops in raw.items()}
        n = sum(len(v) for v in _pending_predictions.values())
        if n:
            print(f"  accuracy tracker: resumed {n} pending prediction(s) from a previous run")
    except Exception as e:
        print(f"  accuracy tracker: could not read {PENDING_PATH} ({e}), starting fresh")
        _pending_predictions = {}


def _save_pending():
    try:
        os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(_pending_predictions, f)
    except Exception as e:
        print(f"  accuracy tracker: could not write {PENDING_PATH} ({e})")


def record_cycle(train_no: str, stations: list):
    """
    Call this once per train per refresh cycle with the current stations
    list (same shape as live_trip_demo.json's "stations"). Detects
    predicted->completed transitions and logs accuracy comparisons.
    """
    pending = _pending_predictions.setdefault(str(train_no), {})
    changed = False

    for s in stations:
        seq = s["seq"]

        if s["status"] == "predicted":
            # remember the latest prediction we have for this station
            if seq not in pending:
                changed = True
            pending[seq] = {
                "station_code": s["station_code"], "station_name": s["station_name"],
                "predicted_delay_p10": s["predicted_delay_p10"],
                "predicted_delay_p50": s["predicted_delay_p50"],
                "predicted_delay_p90": s["predicted_delay_p90"],
                "predicted_at": datetime.now().isoformat(),
                "expected_clock": s.get("predicted_clock_p50"),
            }

        elif s["status"] == "completed" and seq in pending:
            # this station just transitioned from predicted -> completed:
            # compare the last prediction we had against the real outcome
            pred = pending.pop(seq)
            changed = True
            actual = s["actual_delay_min"]
            error_p50 = actual - pred["predicted_delay_p50"]
            within_band = pred["predicted_delay_p10"] <= actual <= pred["predicted_delay_p90"]

            record = {
                "logged_at": datetime.now().isoformat(),
                "train_no": train_no,
                "seq": seq,
                "station_code": pred["station_code"],
                "station_name": pred["station_name"],
                "predicted_at": pred["predicted_at"],
                "predicted_delay_p10": pred["predicted_delay_p10"],
                "predicted_delay_p50": pred["predicted_delay_p50"],
                "predicted_delay_p90": pred["predicted_delay_p90"],
                "actual_delay_min": actual,
                "error_p50_min": round(error_p50, 1),
                "abs_error_p50_min": round(abs(error_p50), 1),
                "within_p10_p90_band": within_band,
            }
            _append_record(record)

    if changed:
        _save_pending()


def _append_record(record: dict):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def get_accuracy_stats(last_n: int = None) -> dict:
    """
    Returns aggregate accuracy stats across all logged predictions.
    If last_n is given, only considers the most recent N records.
    """
    pending_total = sum(len(v) for v in _pending_predictions.values())
    next_up = None
    for stops in _pending_predictions.values():
        for v in stops.values():
            if v.get("expected_clock") and (next_up is None or v["expected_clock"] < next_up[0]):
                next_up = (v["expected_clock"], v.get("station_name"))

    if not os.path.exists(LOG_PATH):
        msg = (f"Watching {pending_total} prediction(s). The first is scored when a train "
               f"reaches {next_up[1]} around {next_up[0]}."
               if next_up else
               "Predictions are scored once a train reaches a station we forecast.")
        return {"count": 0, "pending": pending_total, "message": msg}

    records = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if last_n:
        records = records[-last_n:]

    if not records:
        return {"count": 0, "pending": pending_total,
                "message": f"Watching {pending_total} prediction(s); none have been scored yet."}

    n = len(records)
    mae = sum(r["abs_error_p50_min"] for r in records) / n
    within_band_count = sum(1 for r in records if r["within_p10_p90_band"])
    within_band_pct = 100.0 * within_band_count / n

    # a well-calibrated P10-P90 band should contain the actual outcome
    # roughly 80% of the time (that's what P10/P90 means, by definition)
    calibration_note = (
        "well-calibrated" if 70 <= within_band_pct <= 90
        else ("overconfident — band too narrow" if within_band_pct < 70 else "underconfident — band too wide")
    )

    return {
        "count": n,
        "pending": pending_total,
        "mean_absolute_error_min": round(mae, 1),
        "within_p10_p90_band_pct": round(within_band_pct, 1),
        "calibration": calibration_note,
        "most_recent": records[-5:],
    }


_load_pending()


if __name__ == "__main__":
    stats = get_accuracy_stats()
    print(json.dumps(stats, indent=2))