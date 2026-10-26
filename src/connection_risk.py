"""
Will I make my connection?

This is the question a single arrival time cannot answer. If an app tells
you the train arrives at 19:26 and your onward train leaves at 19:40, the
only available answer is "yes, you have fourteen minutes" — which is wrong
whenever the arrival lands late, and gives no warning that it might.

The forecast engine simulates the rest of the journey 300 times. Each run
produces its own arrival. Counting how many of those runs arrive too late
to make the transfer gives a probability rather than a guess:

    69 of 300 simulated arrivals leave too little time
    -> roughly a 23% chance of missing the connection

That count is only possible because the full set of outcomes is kept.
Collapsing them to P10/P50/P90 first throws the answer away, which is why
this feature follows from the Monte Carlo design rather than being bolted
onto it.

Nothing here calls an API. It is arithmetic over samples already computed.
"""

MAX_SENSIBLE_WAIT_MIN = 12 * 60   # beyond this we assume the connection is missed, not next-day


def _clock_to_minutes(text: str):
    """Accepts '19:40' or '7:05'. Returns minutes past midnight, or None."""
    if not text:
        return None
    parts = str(text).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h * 60 + m


def assess_connection(station, onward_departure_clock, min_transfer_min=15):
    """
    station                  a predicted station dict carrying _delay_samples
    onward_departure_clock   departure of the onward train, 'HH:MM'
    min_transfer_min         minutes genuinely needed to change trains

    Returns a dict describing the risk, or an 'error' key if it cannot be
    computed. min_transfer_min matters: a cross-platform change at a large
    junction with luggage is not the same as stepping across an island
    platform, so the caller sets it rather than us assuming.
    """
    samples = station.get("_delay_samples")
    if not samples:
        return {"error": "No simulated outcomes available for this station."}

    onward_min = _clock_to_minutes(onward_departure_clock)
    if onward_min is None:
        return {"error": "Give the onward departure as HH:MM, for example 19:40."}

    scheduled = station.get("scheduled_arr_min")
    if scheduled is None:
        return {"error": "This station has no scheduled arrival time."}

    missed = 0
    gaps = []
    for delay in samples:
        arrival_mod = (scheduled + delay) % 1440
        gap = (onward_min - arrival_mod) % 1440
        # A gap of 23 hours means the onward train already left, not that
        # you wait a day for it. Treat anything beyond a sensible wait as
        # missed rather than as an enormous comfortable margin.
        if gap > MAX_SENSIBLE_WAIT_MIN:
            gap = -(1440 - gap)
        gaps.append(gap)
        if gap < min_transfer_min:
            missed += 1

    n = len(samples)
    risk = missed / n
    gaps_sorted = sorted(gaps)
    p10 = gaps_sorted[int(0.10 * n)]
    p50 = gaps_sorted[int(0.50 * n)]
    p90 = gaps_sorted[min(int(0.90 * n), n - 1)]

    # Distinguish "unlikely" from "impossible". If the onward train departs
    # before this one is expected to arrive, there is no risk to weigh: the
    # connection does not exist. Reporting that as "100% chance of missing
    # it" is technically true and practically useless — a percentage implies
    # some chance remains, and the reader is left rechecking a number rather
    # than rechecking the time they typed.
    already_departed = p50 < 0

    if already_departed:
        verdict, tone = "Already departed", "bad"
    elif risk < 0.05:
        verdict, tone = "Comfortable", "good"
    elif risk < 0.20:
        verdict, tone = "Probably fine", "good"
    elif risk < 0.50:
        verdict, tone = "Risky", "warn"
    elif risk < 0.85:
        verdict, tone = "Likely to miss", "bad"
    else:
        verdict, tone = "Almost certainly missed", "bad"

    return {
        "already_departed": already_departed,
        "station_name": station.get("station_name"),
        "station_code": station.get("station_code"),
        "onward_departure": onward_departure_clock,
        "min_transfer_min": min_transfer_min,
        "risk": round(risk, 3),
        "risk_pct": round(risk * 100),
        "missed_runs": missed,
        "total_runs": n,
        "verdict": verdict,
        "tone": tone,
        "typical_gap_min": round(p50),
        "worst_case_gap_min": round(p10),   # 10% of runs leave even less time
        "best_case_gap_min": round(p90),
        "expected_arrival": station.get("predicted_clock_p50"),
    }


def summarise(result):
    """One plain sentence for the interface."""
    if "error" in result:
        return result["error"]

    pct = result["risk_pct"]
    gap = result["typical_gap_min"]
    worst = result["worst_case_gap_min"]

    if result.get("already_departed"):
        early = abs(gap)
        return (f"The {result['onward_departure']} departure leaves about {early} minutes "
                f"before this train is expected at {result['station_name']}, so it is not a "
                f"connection you could make. Check the time, or look for a later departure.")

    base = (f"About a {pct}% chance of missing it. "
            f"Typically you would have {gap} minutes to change")
    if worst < result["min_transfer_min"]:
        base += f", but in the worst tenth of outcomes only {max(worst, 0)}."
    else:
        base += "."
    return base