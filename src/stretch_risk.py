"""
Where is the time actually going to go?

A list of per-station delays tells you the total is growing but not where.
A passenger asking "can I sleep for the next three hours" wants the
opposite framing: which stretch of what remains is likely to hurt, and
which is probably quiet.

The forecast already contains the answer and throws it away. Each of the
300 simulated runs carries its own delay through every remaining station,
in order, so the samples at two consecutive stations are paired: sample
number 40 at Kota and sample number 40 at Mathura belong to the same
simulated future. Subtracting them pairwise gives the delay that run added
on that stretch, and 300 of those give a real distribution for the stretch
itself.

That pairing matters. Subtracting the two stations' medians would answer a
different and weaker question — the difference between typical outcomes,
rather than the typical difference — and would hide the variance that makes
one stretch genuinely risky and another merely slow.

Nothing here costs an API call or a new model.
"""

from statistics import median

MIN_MINUTES_WORTH_MENTIONING = 3    # below this a stretch is not news
NOTABLE_ADDITION = 10               # what counts as "a chunk of time" on one stretch


def rank_stretches(predicted_stations, current_delay, limit=3):
    """
    predicted_stations  forecast stations carrying _delay_samples
    current_delay       the delay now, the anchor for the first stretch

    Returns stretches ordered by how much delay they typically add.
    """
    usable = [s for s in predicted_stations if s.get("_delay_samples")]
    if not usable:
        return []

    n = len(usable[0]["_delay_samples"])
    prev_samples = [float(current_delay)] * n
    prev_name = "here"

    stretches = []
    for st in usable:
        cur = st["_delay_samples"]
        if len(cur) != n:
            prev_samples, prev_name = cur, st.get("station_name")
            continue

        # paired: same simulated run at both ends
        added = [cur[i] - prev_samples[i] for i in range(n)]
        typical = median(added)
        chance_notable = sum(1 for a in added if a >= NOTABLE_ADDITION) / n
        worst = sorted(added)[int(0.9 * n)]

        stretches.append({
            "from": prev_name,
            "to": st.get("station_name"),
            "to_code": st.get("station_code"),
            "typical_added_min": round(typical, 1),
            "worst_added_min": round(worst, 1),
            "chance_of_notable_pct": round(chance_notable * 100),
            "expected_clock": st.get("predicted_clock_p50"),
        })
        prev_samples, prev_name = cur, st.get("station_name")

    stretches.sort(key=lambda s: -s["typical_added_min"])
    worth_it = [s for s in stretches if s["typical_added_min"] >= MIN_MINUTES_WORTH_MENTIONING]
    return worth_it[:limit]


def quiet_run(predicted_stations, current_delay):
    """
    The longest run of upcoming stations expected to add little or nothing.

    Useful in its own right: knowing the next four hours are probably calm
    is as actionable as knowing where the trouble is.
    """
    usable = [s for s in predicted_stations if s.get("_delay_samples")]
    if len(usable) < 2:
        return None

    n = len(usable[0]["_delay_samples"])
    prev = [float(current_delay)] * n
    best = cur_run = []

    for st in usable:
        s = st["_delay_samples"]
        if len(s) != n:
            prev = s
            continue
        added = median([s[i] - prev[i] for i in range(n)])
        if added < MIN_MINUTES_WORTH_MENTIONING:
            cur_run = cur_run + [st]
        else:
            if len(cur_run) > len(best):
                best = cur_run
            cur_run = []
        prev = s

    if len(cur_run) > len(best):
        best = cur_run
    if len(best) < 2:
        return None

    return {
        "from": best[0].get("station_name"),
        "to": best[-1].get("station_name"),
        "stations": len(best),
        "until_clock": best[-1].get("predicted_clock_p50"),
    }


def summarise(stretches, quiet=None):
    """A sentence a passenger can act on."""
    if not stretches:
        base = "No single stretch ahead stands out as likely to add much delay."
    else:
        w = stretches[0]
        base = (f"Most of the remaining delay is likely between {w['from']} and "
                f"{w['to']}, typically about {round(w['typical_added_min'])} more minutes")
        if w["chance_of_notable_pct"] >= 25:
            base += (f", with a {w['chance_of_notable_pct']}% chance of losing "
                     f"{NOTABLE_ADDITION} minutes or more there")
        base += "."

    if quiet:
        base += (f" The run from {quiet['from']} to {quiet['to']} looks quiet, "
                 f"so little should change before about {quiet['until_clock']}.")
    return base
