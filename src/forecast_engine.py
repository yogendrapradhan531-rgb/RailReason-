"""
Shared forecasting engine used by both:
  - pipeline.py       (synthetic demo trips)
  - live_pipeline.py  (real scraped NTES data)

Keeping this in one place means the live feed and the demo produce
identically-shaped output, so the dashboard doesn't care which one fed it.
"""

import pandas as pd
import numpy as np
import joblib

N_SIMULATIONS = 300

# Calibration correction, measured rather than assumed.
#
# The simulator's spread turned out to be too tight against real outcomes:
# analyse_calibration.py found only about half of arrivals landing inside a
# range that promises 80%. Rather than guess a multiplier, that script
# computes one from the logged errors, and it is applied here.
#
# 1.0 means no correction. Raise it only on the number the analysis gives
# you, and re-check coverage afterwards — a spread that is too wide is its
# own kind of dishonesty, just a less obvious one.
SPREAD_SCALE = 2.14


def clock(mins):
    mins = int(mins)
    day = mins // 1440
    h = (mins % 1440) // 60
    m = mins % 60
    return f"Day {day+1} {h:02d}:{m:02d}" if day > 0 else f"{h:02d}:{m:02d}"


def load_models():
    bundle = joblib.load("output/models.joblib")
    leg_features = pd.read_csv("output/leg_features.csv")
    return bundle["reg"], bundle["clf"], bundle["feature_cols"], leg_features


def empirical_magnitude_samplers(leg_features):
    """cause -> array of historical delay_increment values for that cause."""
    samplers = {}
    for cause, grp in leg_features.groupby("target_cause"):
        samplers[cause] = grp["target_delay_increment"].values
    return samplers


def identify_junctions_and_fog_zone(sched: pd.DataFrame):
    # Dwell-time heuristic alone doesn't generalize: a premium/fast train
    # (e.g. Rajdhani) can have very short halts even at genuine major
    # junctions, so a >=8min threshold misses them entirely. Station
    # naming is a more reliable signal — real junctions are named "Jn"/
    # "Junction" in Indian Railways data regardless of how long any
    # particular train dwells there. Use both, OR'd together.
    by_halt = set(sched.loc[sched["halt_min"] >= 8, "Station Code"])
    name_upper = sched["Station Name"].astype(str).str.upper()
    by_name = set(sched.loc[name_upper.str.contains(r"\bJN\b|\bJUNCTION\b", regex=True), "Station Code"])
    junctions = by_halt | by_name

    cum = sched["Distance"].astype(float)
    fog_zone = set(sched.loc[cum >= cum.max() * 0.35, "Station Code"])
    return junctions, fog_zone


def monte_carlo_forecast(current_delay, cum_avg, trend, stations_travelled,
                          day_of_week, month, future_sched: pd.DataFrame,
                          reg, clf, feature_cols, samplers, junctions, fog_zone,
                          n_simulations=N_SIMULATIONS, total_distance_km=None,
                          initial_prev_cause="NONE", occupancy_by_station=None):
    """
    Runs N Monte Carlo paths forward from the current state through every
    remaining station in `future_sched`. Returns a list of per-station dicts
    (P10/P50/P90 delay + ETA) and the mean cause-risk distribution for the
    very next leg.

    Each simulation path carries its OWN prev_cause forward (the cause it
    sampled on the previous leg), which is what makes the model's
    prev_cause feature meaningful here rather than a constant.

    occupancy_by_station optionally maps a station code to real observed
    track occupancy ahead of it, e.g.
        {"RTM": {"occupants_ahead": 2, "occupant_max_delay": 41.0,
                 "yielding_to_priority": 1, "section_pressure": 3}}
    These are only added to the feature frame when the loaded model was
    actually trained with them, so a model trained without network
    features keeps working untouched. Stations with no observation fall
    back to zero, which the model reads as a clear section — an honest
    default, but it does mean the feature is inert wherever we have not
    paid for a station-board lookup.
    """
    net_cols = ["occupants_ahead", "occupant_max_delay",
                "yielding_to_priority", "section_pressure"]
    uses_network = any(c in feature_cols for c in net_cols)
    occupancy_by_station = occupancy_by_station or {}
    n_future = len(future_sched)
    sim_delays = np.zeros((n_simulations, n_future))
    classes = clf.named_steps["model"].classes_
    cause_risk_first_leg = None

    if total_distance_km is None or total_distance_km <= 0:
        total_distance_km = float(future_sched["Distance"].max()) if len(future_sched) else 1.0

    delay = np.full(n_simulations, current_delay)
    avg = np.full(n_simulations, cum_avg)
    tr = np.full(n_simulations, trend)
    traveled = np.full(n_simulations, stations_travelled)
    prev_cause = np.full(n_simulations, initial_prev_cause, dtype=object)

    for j, srow in future_sched.reset_index(drop=True).iterrows():
        hour = int((srow["arr_min"] // 60 + float(np.mean(delay)) // 60) % 24) if pd.notna(srow["arr_min"]) else 0
        is_fz = srow["Station Code"] in fog_zone
        is_j = srow["Station Code"] in junctions
        route_frac = round(float(srow["Distance"]) / total_distance_km, 3) if total_distance_km > 0 else 0.0

        batch = pd.DataFrame({
            "current_delay": delay, "cum_avg_delay_so_far": avg, "delay_trend": tr,
            "stations_travelled": traveled,
            "scheduled_run_min": srow["segment_travel_min"], "scheduled_halt_min": srow["halt_min"],
            "segment_distance_km": srow["segment_distance_km"], "hour_of_day": hour,
            "route_position_fraction": route_frac,
            "prev_cause": prev_cause,
            "is_fog_zone": int(is_fz), "is_junction": int(is_j), "is_winter": int(month in (11, 0, 1)),
            "is_night_or_early": int(hour <= 8 or hour >= 22), "day_of_week": day_of_week,
        })

        if uses_network:
            obs = occupancy_by_station.get(srow["Station Code"], {})
            for c in net_cols:
                batch[c] = obs.get(c, 0)

        batch = batch[feature_cols]

        probs = clf.predict_proba(batch)
        if j == 0:
            cause_risk_first_leg = dict(zip(classes.tolist(), np.round(probs.mean(axis=0), 3).tolist()))

        cum_probs = probs.cumsum(axis=1)
        draws = np.random.random(n_simulations)[:, None]
        cause_idx = (draws < cum_probs).argmax(axis=1)
        sampled_causes = classes[cause_idx]

        mags = np.empty(n_simulations)
        for c in np.unique(sampled_causes):
            mask = sampled_causes == c
            pool = samplers.get(c, np.array([0.0]))
            mags[mask] = np.random.choice(pool, size=mask.sum())

        delay = np.maximum(0.0, delay + mags)
        traveled = traveled + 1
        avg = (avg * traveled + delay) / (traveled + 1)
        tr = mags
        prev_cause = sampled_causes.astype(object)
        sim_delays[:, j] = delay

    stations_out = []
    for j, srow in future_sched.reset_index(drop=True).iterrows():
        dist = sim_delays[:, j]
        sched_min = int(srow["arr_min"])
        p50 = float(np.percentile(dist, 50))

        # Stretch the simulated outcomes around their own median. Scaling the
        # samples rather than just the reported percentiles matters: connection
        # risk counts these samples directly, so correcting the headline range
        # while leaving the samples untouched would leave that feature exactly
        # as overconfident as before, in a way nothing would reveal.
        if SPREAD_SCALE != 1.0:
            dist = np.maximum(0.0, p50 + (dist - p50) * SPREAD_SCALE)
        entry = dict(
            seq=int(srow["SEQ"]), station_code=srow["Station Code"],
            station_name=srow["Station Name"].strip(), distance_km=float(srow["Distance"]),
            status="predicted",
            scheduled_arr_min=sched_min, scheduled_clock=clock(sched_min),
            predicted_delay_p10=round(float(np.percentile(dist, 10)), 1),
            predicted_delay_p50=round(p50, 1),
            predicted_delay_p90=round(float(np.percentile(dist, 90)), 1),
            predicted_eta_min_p50=int(sched_min + p50),
            predicted_clock_p50=clock(sched_min + p50),
            # The 300 simulated outcomes, kept rather than discarded.
            # Percentiles answer "when will it arrive"; the full set answers
            # "how often does it arrive later than X", which is what a
            # connection risk is. That question cannot be recovered from
            # P10/P50/P90 alone, so the samples travel with the station and
            # are stripped out server-side before reaching the browser.
            _delay_samples=[round(float(v), 1) for v in dist],
        )
        if "Lat" in srow.index and "Lng" in srow.index and pd.notna(srow["Lat"]) and pd.notna(srow["Lng"]) and srow["Lat"] != "":
            entry["lat"] = float(srow["Lat"])
            entry["lng"] = float(srow["Lng"])
        stations_out.append(entry)

    return stations_out, cause_risk_first_leg