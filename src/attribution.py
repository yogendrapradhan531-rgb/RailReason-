"""
Retrospective delay-attribution engine.

Given what's ACTUALLY observable once a train reaches a station (actual
halt duration, actual running time for the segment just completed), infer
why the delay changed on that leg. This never has access to the
generator's hidden ground-truth cause label - it only uses the same
observable signals a real NTES/GPS feed would give you. We validate
against the (hidden-in-production) ground truth purely to sanity-check
the rule design.

Explanation phrasing: each cause has several hand-written natural-language
variants. Which variant is picked is DETERMINISTIC per station (seeded off
station_code + cause), not random-per-run — so the same leg always reads
the same way on repeated loads/refreshes (important for a live dashboard
that polls every few minutes), while different stations/trips still read
with natural variety instead of an obviously templated repeated sentence.
"""

import random
import pandas as pd
import numpy as np

HALT_OVERRUN_THRESHOLD = 2.0    # min beyond schedule to call it a halt overrun
RUN_OVERRUN_THRESHOLD = 4.0     # min beyond schedule to call it a running/section overrun
FOG_HOURS = set(range(0, 9)) | {22, 23}
WINTER_MONTHS = {11, 0, 1}


def _pick(station_code: str, cause: str, variants: list[str]) -> str:
    """Deterministically pick one phrasing variant, seeded by station+cause
    so repeated calls for the same leg are stable, but different legs vary."""
    rng = random.Random(f"{station_code}:{cause}")
    return rng.choice(variants)


def infer_cause(row) -> tuple[str, str]:
    """Returns (cause_code, human_readable_explanation)."""
    halt_over = row.halt_actual - row.halt_scheduled
    run_over = row.run_actual - row.run_scheduled
    delay_inc = row.delay_increment
    code = row.station_code

    if abs(delay_inc) < 0.5:
        return "NONE", "Right on schedule through this stretch."

    if delay_inc < 0:
        mins = abs(delay_inc)
        variants = [
            f"Clawed back {mins:.0f} min here — the schedule has some padding built in, and the driver's using it.",
            f"Made up {mins:.0f} min on this leg, running ahead of the padded timetable.",
            f"Gained {mins:.0f} min back on this stretch — a clear run let the train close the gap a little.",
        ]
        return "RECOVERY", _pick(code, "RECOVERY", variants)

    # Large single-event spike disproportionate to normal halt/run variance
    if delay_inc > 20 and halt_over < 10 and run_over < 10:
        variants = [
            f"Delay jumped by {delay_inc:.0f} min in one go at {code}, with nothing in the halt or running time to explain it — points to something sudden, like a signal fault or a loco issue.",
            f"A sharp {delay_inc:.0f}-min hit at {code} that doesn't line up with a longer halt or a slower run — most likely an unscheduled stop or technical snag.",
            f"Something abrupt cost {delay_inc:.0f} min at {code}; halt and running times both look normal, so this has the signature of a technical fault rather than routine congestion.",
        ]
        return "TECHNICAL", _pick(code, "TECHNICAL", variants)

    # If real weather data is available (live_pipeline.py sets this from
    # weather_client.py), trust it directly instead of the synthetic
    # season/hour heuristic — a real "it's foggy right now" reading beats
    # a guess based on month and clock time.
    real_weather_fog = getattr(row, "real_weather_fog", None)
    if real_weather_fog is not None:
        is_fog_candidate = real_weather_fog and run_over >= RUN_OVERRUN_THRESHOLD
    else:
        is_fog_candidate = (
            row.is_fog_zone and row.month in WINTER_MONTHS and row.hour_of_day in FOG_HOURS
            and run_over >= RUN_OVERRUN_THRESHOLD
        )
    if is_fog_candidate:
        variants = [
            f"Ran {run_over:.0f} min slower than scheduled through a fog-prone stretch overnight — most likely a fog-driven speed restriction.",
            f"Lost {run_over:.0f} min crawling through low-visibility conditions on this section — classic winter fog slowdown.",
            f"This section is a known fog corridor, and running {run_over:.0f} min over schedule at this hour fits a visibility-related speed restriction.",
        ]
        return "FOG", _pick(code, "FOG", variants)

    if run_over >= RUN_OVERRUN_THRESHOLD and halt_over < HALT_OVERRUN_THRESHOLD:
        if row.is_junction:
            variants = [
                f"Ran {run_over:.0f} min over schedule pulling into {code}, a busy junction — likely held back to let another train cross or clear the platform first.",
                f"Slowed by {run_over:.0f} min approaching {code} — a junction where trains often wait their turn for precedence.",
                f"{run_over:.0f} min lost on the approach to {code}; at a junction this usually means a crossing or precedence hold rather than a genuine speed restriction.",
            ]
            return "CROSSING_WAIT", _pick(code, "CROSSING_WAIT", variants)
        variants = [
            f"Ran {run_over:.0f} min over the scheduled time for this segment — likely a speed restriction or track-work slow order.",
            f"This stretch took {run_over:.0f} min longer than planned, consistent with a temporary speed limit or maintenance work on the line.",
            f"{run_over:.0f} min slower than timetabled here, with no unusual halt — points to a section-specific slow order.",
        ]
        return "SECTION_SLOW", _pick(code, "SECTION_SLOW", variants)

    if halt_over >= HALT_OVERRUN_THRESHOLD:
        variants = [
            f"Halted {halt_over:.0f} min longer than scheduled at {code} — likely a platform, crew, or loading delay.",
            f"Sat at {code} for {halt_over:.0f} min past the scheduled halt, which usually comes down to platform availability or crew handover.",
            f"{halt_over:.0f} extra minutes at the platform in {code} — a longer-than-planned stop, most likely operational rather than anything on the run itself.",
        ]
        return "HALT_OVERRUN", _pick(code, "HALT_OVERRUN", variants)

    if row.stations_from_origin <= 1 and delay_inc > 0.5:
        variants = [
            "Started late from the origin station, and that delay has simply carried forward from there.",
            "This delay traces back to a late departure at the origin — nothing new has gone wrong since, it just hasn't been recovered yet.",
        ]
        return "CASCADING", _pick(code, "CASCADING", variants)

    variants = [
        f"A brief {delay_inc:.0f}-min loss here with no clear halt or running overrun — likely a short signal or crossing hold.",
        f"Small {delay_inc:.0f}-min dip at {code}, too minor to pin on a halt or run overrun — probably just a momentary hold.",
    ]
    return "CROSSING_WAIT", _pick(code, "CROSSING_WAIT", variants)


def main():
    trips = pd.read_csv("output/synthetic_trips_causal.csv")
    trips = trips.sort_values(["train_no", "trip_id", "seq"]).reset_index(drop=True)
    trips["stations_from_origin"] = trips.groupby(["train_no", "trip_id"]).cumcount()

    results = trips.apply(lambda r: infer_cause(r), axis=1)
    trips["inferred_cause"] = [r[0] for r in results]
    trips["explanation"] = [r[1] for r in results]

    # only score legs where a real event happened (skip trivial NONE-vs-NONE agreement inflation check separately)
    acc_all = (trips["inferred_cause"] == trips["primary_cause"]).mean()
    non_none = trips[trips.primary_cause != "NONE"]
    acc_non_none = (non_none["inferred_cause"] == non_none["primary_cause"]).mean()

    print(f"Rule-engine accuracy (all legs, incl. NONE):     {acc_all:.3f}")
    print(f"Rule-engine accuracy (legs with a real cause):   {acc_non_none:.3f}")
    print()
    print("Confusion (ground truth -> inferred), non-NONE legs:")
    print(pd.crosstab(non_none["primary_cause"], non_none["inferred_cause"]))

    trips.to_csv("output/trips_with_attribution.csv", index=False)
    print("\nSaved -> output/trips_with_attribution.csv")


if __name__ == "__main__":
    main()