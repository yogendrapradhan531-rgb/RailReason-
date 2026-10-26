"""
Turns a real RailRadar live-status response into the exact same JSON
schema the dashboard already reads (see forecast_engine.py / pipeline.py)
— so no dashboard changes are needed.

RailRadar gives real actualArrival AND actualDeparture per station, so
unlike the earlier scraper-based draft, we can compute REAL halt and
running-time overruns (not guesses) and feed them straight into the
same attribution.infer_cause() rule engine used for the synthetic demo.

Station codes from RailRadar are standard IR codes, matching the
"Station Code" column in your schedule CSVs directly — no fuzzy name
matching needed (unlike the old NTES-scraper draft).
"""

import pandas as pd
import numpy as np
import json
import time
from datetime import datetime

from attribution import infer_cause
from forecast_engine import (
    clock, load_models, empirical_magnitude_samplers,
    identify_junctions_and_fog_zone, monte_carlo_forecast,
)
from railradar_client import fetch_live_status, fetch_active_run, RailRadarError
from weather_client import get_weather_at, WeatherError
from station_conflict import assess_conflict_risk
import observation_log

# How many upcoming junction stations to check for cross-train conflict
# risk. Reduced to 1 (from 2) to cut API calls during rate-limit pressure.
MAX_JUNCTIONS_TO_CHECK = 1


def _parse_iso(ts):
    if not ts:
        return None
    return datetime.fromisoformat(ts)


def _minutes_between(t1, t2):
    """Minutes from t1 to t2 (t2 - t1), or None if either is missing."""
    if t1 is None or t2 is None:
        return None
    return (t2 - t1).total_seconds() / 60.0


def build_live_trip(live_data: dict, sched_path: str, reg, clf, feature_cols, samplers) -> dict:
    sched = pd.read_csv(sched_path)
    junctions, fog_zone = identify_junctions_and_fog_zone(sched)
    sched_by_code = sched.set_index("Station Code")

    route = live_data.get("route", [])
    if not route:
        raise ValueError("RailRadar response had no route data.")

    now = _parse_iso(live_data.get("lastUpdatedAt")) or datetime.now()

    # IMPORTANT: don't infer "completed" from timestamp presence alone —
    # RailRadar can pre-populate actualArrival/actualDeparture with
    # projected values (matching an announced delay) even for stations
    # the train hasn't physically reached yet. currentLocation.sequence
    # is the authoritative "where is the train right now" signal, so use
    # that as the cutoff instead.
    current_location = live_data.get("currentLocation") or {}
    current_seq_cutoff = current_location.get("sequence")

    stations_out = []
    completed_delays = []
    last_matched_seq = None
    prev_actual_departure = None
    skipped_count = 0

    for stop in route:
        code = stop["stationCode"]
        if code not in sched_by_code.index:
            skipped_count += 1
            continue

        stop_seq = stop.get("sequence")
        if current_seq_cutoff is not None and stop_seq is not None and stop_seq > current_seq_cutoff:
            continue  # train hasn't reached this station yet — leave it for the forecast, not the completed list

        actual_arr = _parse_iso(stop.get("actualArrival"))
        actual_dep = _parse_iso(stop.get("actualDeparture"))
        sched_arr = _parse_iso(stop.get("scheduledArrival"))
        sched_dep = _parse_iso(stop.get("scheduledDeparture"))

        has_real_data = actual_arr is not None or actual_dep is not None
        if not has_real_data:
            continue  # not reached yet — will be covered by the forecast instead

        srow = sched_by_code.loc[code]
        seq = int(srow["SEQ"])
        sched_min = int(srow["arr_min"])

        delay_arrival = stop.get("delayArrival")
        delay_departure = stop.get("delayDeparture")
        # Use the ARRIVAL delay for a station's headline figure. We previously
        # preferred delayDeparture and then added it to the SCHEDULED ARRIVAL,
        # which mixes two different measurements: at Balharshah that produced
        # 12:10 + 7 = 12:17, matching neither RailRadar's reported 12:20 nor
        # Where Is My Train's 12:22. Arrival delay is also what other trackers
        # display as "N min late", so this keeps us comparable with them.
        current_station_delay = delay_arrival if delay_arrival is not None else (delay_departure or 0)

        halt_scheduled = _minutes_between(sched_arr, sched_dep) if (sched_arr and sched_dep) else float(srow["halt_min"])
        halt_actual = _minutes_between(actual_arr, actual_dep) if (actual_arr and actual_dep) else halt_scheduled

        run_scheduled = float(srow["segment_travel_min"])
        run_actual = _minutes_between(prev_actual_departure, actual_arr) if (prev_actual_departure and actual_arr) else run_scheduled

        delay_increment = current_station_delay - (completed_delays[-1] if completed_delays else 0)

        row_dict = {
            "station_code": code,
            "halt_scheduled": halt_scheduled or 0,
            "halt_actual": halt_actual if halt_actual is not None else (halt_scheduled or 0),
            "run_scheduled": run_scheduled,
            "run_actual": run_actual if run_actual is not None else run_scheduled,
            "delay_increment": delay_increment,
            "is_fog_zone": code in fog_zone,
            "is_junction": code in junctions,
            "month": now.month - 1,
            "hour_of_day": now.hour,
            "stations_from_origin": seq - 1,
        }
        row = pd.Series(row_dict)
        cause, explanation = infer_cause(row) if seq > 1 else ("NONE", "Origin station.")

        # Prefer the timestamp RailRadar actually reported over rebuilding one
        # from scheduled+delay. Those two can disagree, and when they do the
        # reconstructed time is a number no source ever published. Fall back to
        # reconstruction only when no actual timestamp exists.
        if actual_arr is not None:
            actual_clock_str = actual_arr.strftime("%H:%M")
            day_no = (actual_arr - _parse_iso(route[0].get("scheduledArrival") or route[0].get("scheduledDeparture"))).days \
                if route and (route[0].get("scheduledArrival") or route[0].get("scheduledDeparture")) else 0
            if day_no > 0:
                actual_clock_str = f"Day {day_no + 1} {actual_clock_str}"
        else:
            actual_clock_str = clock(sched_min + current_station_delay)
        actual_clock_min = sched_min + current_station_delay
        station_entry = dict(
            seq=seq, station_code=code, station_name=srow["Station Name"].strip(),
            distance_km=float(srow["Distance"]), status="completed",
            scheduled_arr_min=sched_min, scheduled_clock=clock(sched_min),
            actual_delay_min=round(float(current_station_delay), 1),
            actual_clock=actual_clock_str,
            cause=cause, explanation=explanation,
        )
        lat_val, lng_val = srow.get("Lat"), srow.get("Lng")
        if pd.notna(lat_val) and pd.notna(lng_val) and lat_val != "" and lng_val != "":
            station_entry["lat"] = float(lat_val)
            station_entry["lng"] = float(lng_val)
        stations_out.append(station_entry)

        # Persist this arrival as real history. Deduplicated inside, so the
        # five-minute refresh does not rewrite the same row repeatedly.
        observation_log.record_arrival(
            train_no=live_data.get("trainNumber", sched["Train No"].iloc[0]),
            run_date=live_data.get("run_date") or now.strftime("%Y-%m-%d"),
            station_entry=station_entry,
            raw_stop=stop,
            context={
                "halt_scheduled": halt_scheduled, "halt_actual": halt_actual,
                "run_scheduled": run_scheduled, "run_actual": run_actual,
                "is_junction": code in junctions, "is_fog_zone": code in fog_zone,
            },
        )

        completed_delays.append(current_station_delay)
        last_matched_seq = seq
        if actual_dep is not None:
            prev_actual_departure = actual_dep

    if skipped_count:
        print(f"({skipped_count} minor pass-through stations from RailRadar weren't in the schedule CSV — normal, skipped.)")

    if last_matched_seq is None:
        # A train that has not departed reports no actual times anywhere, so
        # the loop above matches nothing. That is a normal state, not a
        # failure: it is exactly what RailRadar says about every train before
        # its start time. Raising here took whole trains off the dashboard
        # for hours at a time.
        #
        # Anchor on the origin instead and let everything ahead be forecast.
        # RailRadar sometimes pre-fills a projected timestamp at the origin
        # and sometimes does not, which is why this used to fail only
        # intermittently for the same train.
        first = sched.sort_values("SEQ").iloc[0]
        origin_delay = float(current_location.get("delayMinutes") or 0)
        sched_min = int(first["arr_min"])
        entry = dict(
            seq=int(first["SEQ"]), station_code=first["Station Code"],
            station_name=str(first["Station Name"]).strip(),
            distance_km=float(first["Distance"]), status="completed",
            scheduled_arr_min=sched_min, scheduled_clock=clock(sched_min),
            actual_delay_min=round(origin_delay, 1),
            actual_clock=clock(sched_min + origin_delay),
            cause="NONE",
            # Two different situations land here. A train before its start
            # time has genuinely not moved. But a train that HAS departed can
            # also reach this point, because our schedules list only booked
            # halts: 12952 leaves New Delhi at 16:55 and its next listed stop
            # is Kota at 21:30, so for several hours it is running with no
            # listed arrival to report. Saying "waiting to depart" in that
            # case would be plainly wrong to anyone sitting on the train.
            explanation=("Waiting to depart; nothing has been measured yet."
                         if live_data.get("status") == "not-started" else
                         f"Departed, but has not yet reached a listed stop. "
                         f"Delay measured en route at "
                         f"{current_location.get('stationName') or 'the current position'}."),
        )
        if pd.notna(first.get("Lat")) and pd.notna(first.get("Lng")) and first.get("Lat") != "":
            entry["lat"] = float(first["Lat"]); entry["lng"] = float(first["Lng"])

        stations_out = [entry]
        completed_delays = [origin_delay]
        last_matched_seq = int(first["SEQ"])
        print(f"  {live_data.get('trainNumber', '')} has no listed-stop arrivals yet "
              f"({live_data.get('status')}, {origin_delay:.0f} min); forecasting from the origin")

    # Real weather at the train's current position. Shown as honest
    # contextual info alongside the attribution engine's output, rather
    # than fed into infer_cause() to force a FOG reclassification — that
    # engine's job is to explain what the REAL timing data says happened,
    # and fabricating numbers to nudge its output would undermine that.
    current_weather = None
    current_srow = sched_by_code.loc[sched_by_code["SEQ"] == last_matched_seq].iloc[0]
    lat, lng = current_srow.get("Lat"), current_srow.get("Lng")
    if pd.notna(lat) and pd.notna(lng) and lat != "" and lng != "":
        try:
            w = get_weather_at(float(lat), float(lng))
            print(f"  weather at {current_srow.name}: {w['condition']} ({w['description']}), "
                  f"visibility {w['visibility_km']}km -> foggy={w['is_foggy']}")
            current_weather = {
                "station_code": current_srow.name,
                "condition": w["condition"], "description": w["description"],
                "visibility_km": w["visibility_km"], "is_foggy": w["is_foggy"],
            }
        except WeatherError as e:
            print(f"  (weather lookup skipped: {e})")
    else:
        print(f"  (weather lookup skipped: no lat/lng for {current_srow.name} in schedule CSV — "
              f"raw values were lat={lat!r}, lng={lng!r})")

    # RailRadar reports position at many points we do not list as stops
    # (outer cabins, block huts, non-stopping stations). Our schedule CSVs
    # are halts-only, so the newest reading often sits at a point absent
    # from our data, and using only matched halts makes the headline look
    # stale — in one comparison we showed 7 minutes at Balharshah while the
    # train had already reached Majri Outer Cabin at 21 minutes.
    #
    # currentLocation is RailRadar's own answer to "where is it and how late
    # is it right now", so trust it for the headline even when that point is
    # not one of our stops. The route spine still shows halts only.
    current_delay = completed_delays[-1]
    current_position_note = None
    live_loc_delay = current_location.get("delayMinutes")
    live_loc_name = current_location.get("stationName")
    if live_loc_delay is not None and live_loc_name:
        if abs(float(live_loc_delay) - float(current_delay)) > 1:
            current_position_note = {
                "station_name": live_loc_name,
                "station_code": current_location.get("stationCode"),
                "delay_min": round(float(live_loc_delay), 1),
                "is_listed_stop": current_location.get("stationCode") in set(sched_by_code.index),
            }
            print(f"  newest reading: {live_loc_name} at {live_loc_delay} min "
                  f"(last listed stop showed {current_delay} min)")
        current_delay = float(live_loc_delay)
    cum_avg = float(np.mean(completed_delays))
    trend_window = completed_delays[-4:]
    trend = (trend_window[-1] - trend_window[0]) / len(trend_window) if len(trend_window) >= 2 else 0.0

    future_sched = sched[sched["SEQ"] > last_matched_seq]
    day_of_week = now.weekday()
    month = now.month - 1

    # If it's genuinely foggy right now near the train, treat the very
    # next station as being in a fog-risk zone for the forecast — a
    # legitimate use of a real current reading to inform a forward
    # prediction (as opposed to rewriting what already happened).
    forecast_fog_zone = set(fog_zone)
    if current_weather and current_weather["is_foggy"] and len(future_sched) > 0:
        next_station_code = future_sched.iloc[0]["Station Code"]
        forecast_fog_zone.add(next_station_code)
        print(f"  (real fog detected — treating next station {next_station_code} as fog-risk in the forecast)")

    def _forecast(occupancy=None):
        return monte_carlo_forecast(
            current_delay=current_delay, cum_avg=cum_avg, trend=trend,
            stations_travelled=last_matched_seq - 1,
            day_of_week=day_of_week, month=month, future_sched=future_sched,
            reg=reg, clf=clf, feature_cols=feature_cols, samplers=samplers,
            junctions=junctions, fog_zone=forecast_fog_zone,
            total_distance_km=float(sched["Distance"].max()),
            initial_prev_cause=(stations_out[-1]["cause"] if stations_out else "NONE"),
            occupancy_by_station=occupancy,
        )

    # first pass tells us which stations are worth querying
    predicted_stations, cause_risk_first_leg = _forecast()

    time.sleep(3)  # pace out before hitting RailRadar again for conflict checks

    # Real network-level cascading risk: for the next couple of upcoming
    # JUNCTION stations, check RailRadar's live board for other delayed
    # trains due around the same time — a genuine cross-train conflict
    # signal, not synthetic. Capped to MAX_JUNCTIONS_TO_CHECK to limit
    # API usage.
    # Real occupancy, observed rather than simulated. station_conflict
    # returns the other trains genuinely due at a junction around the same
    # time as ours; that is the live counterpart of the simulator's
    # occupants_ahead. Only stations we actually query get a reading, so
    # the rest stay absent and forecast_engine treats them as clear.
    occupancy_by_station = {}

    junctions_checked = 0
    for s in predicted_stations:
        if junctions_checked >= MAX_JUNCTIONS_TO_CHECK:
            break
        if s["station_code"] not in junctions:
            continue
        print(f"  checking network conflict risk at {s['station_code']}...")
        try:
            conflicts = assess_conflict_risk(
                our_train_no=live_data.get("trainNumber", ""),
                our_eta_min=s["predicted_eta_min_p50"],
                station_code=s["station_code"],
            )
            if conflicts:
                s["network_risk"] = conflicts
                print(f"    {len(conflicts)} other delayed train(s) due around the same time")
            # record the reading either way: zero conflicts is itself an
            # observation that the section ahead is clear
            occupancy_by_station[s["station_code"]] = {
                "occupants_ahead": len(conflicts),
                "occupant_max_delay": max((c["delay_min"] for c in conflicts), default=0.0),
                "yielding_to_priority": 0,   # RailRadar's board does not expose precedence
                "section_pressure": len(conflicts),
            }
        except RailRadarError as e:
            print(f"    (skipped: {e})")
        junctions_checked += 1
        time.sleep(3)  # pace out calls to avoid tripping a short-term burst limit

    # If the loaded model understands occupancy, redo the forecast now that
    # we have real readings. Costs no extra API calls, only CPU.
    if occupancy_by_station and any(c in feature_cols for c in
            ("occupants_ahead", "occupant_max_delay", "yielding_to_priority", "section_pressure")):
        print(f"  re-forecasting with real occupancy at {len(occupancy_by_station)} junction(s)")
        risk_carry = {s["station_code"]: s.get("network_risk") for s in predicted_stations}
        predicted_stations, cause_risk_first_leg = _forecast(occupancy_by_station)
        for s in predicted_stations:
            if risk_carry.get(s["station_code"]):
                s["network_risk"] = risk_carry[s["station_code"]]

    stations_out.extend(predicted_stations)

    # Interpolated live position, for the map: RailRadar gives
    # segmentProgress (0.0-1.0, how far along the current inter-station
    # segment the train is) and speedKmh. We linearly interpolate between
    # the last completed station's coordinates and the next station's —
    # an approximation (real track curves, this doesn't), but a genuine
    # real-time signal, not a fake animation.
    live_position = None
    last_completed = next((s for s in reversed(stations_out) if s["status"] == "completed"), None)
    next_predicted = next((s for s in stations_out if s["status"] == "predicted"), None)
    if last_completed and "lat" in last_completed:
        progress = current_location.get("segmentProgress")
        speed = current_location.get("speedKmh")
        print(f"  live position inputs: segmentProgress={progress!r}, speedKmh={speed!r}, "
              f"last_completed={last_completed['station_code']}, "
              f"next_predicted={next_predicted['station_code'] if next_predicted else None}")
        if next_predicted and "lat" in next_predicted and progress:
            frac = max(0.0, min(1.0, float(progress)))
            live_position = {
                "lat": last_completed["lat"] + (next_predicted["lat"] - last_completed["lat"]) * frac,
                "lng": last_completed["lng"] + (next_predicted["lng"] - last_completed["lng"]) * frac,
                "speed_kmh": speed,
                "segment_progress": frac,
            }
        else:
            # no progress data or no next station (e.g. train at rest, or at terminus) — sit at the last known station
            live_position = {
                "lat": last_completed["lat"], "lng": last_completed["lng"],
                "speed_kmh": speed, "segment_progress": 0.0,
            }

    # RailRadar reports a run status: not-started, running, terminated.
    # Without it we treat the origin as a completed stop, because RailRadar
    # pre-fills projected timestamps there — so a train that has not left
    # yet was shown as "0 min late, on schedule, train is here now", with a
    # scheduled future time displayed as an actual. That reads as a
    # measurement when nothing has been measured.
    run_status = live_data.get("status")

    train_name = live_data.get("trainName") or sched["Train Name"].iloc[0].strip()
    total_distance = float(sched["Distance"].max())
    return dict(
        train_no=int(live_data.get("trainNumber", sched["Train No"].iloc[0])),
        train_name=train_name,
        up_to_seq=last_matched_seq, total_distance_km=total_distance,
        current_delay=round(float(current_delay), 1),
        next_leg_risk=cause_risk_first_leg,
        current_weather=current_weather,
        current_position_note=current_position_note,
        run_status=run_status,
        live_position=live_position,
        stations=stations_out,
        fetched_at=live_data.get("lastUpdatedAt", now.isoformat()),
    )


def main():
    # TRAIN_SCHEDULES maps train number -> local schedule CSV path.
    # 13007/15483 (your original demo trains) are NOT tracked live by
    # RailRadar — confirmed by testing. 12951 IS tracked (Mumbai-Delhi
    # Rajdhani). Run `python fetch_schedule.py <train_no>` once for any
    # new train before adding it here; it auto-generates the matching
    # schedule CSV in data/. Premium trains (Rajdhani/Shatabdi/Duronto/
    # Vande Bharat) are the most reliably tracked — see railradar.in for
    # which trains are in active coverage.
    TRAIN_SCHEDULES = {
        "12951": "data/route_12951_schedule.csv",
        "12952": "data/route_12952_schedule.csv",
        "22691": "data/route_22691_schedule.csv",
    }

    reg, clf, feature_cols, leg_features = load_models()
    samplers = empirical_magnitude_samplers(leg_features)

    outputs = {}
    for train_no, sched_path in TRAIN_SCHEDULES.items():
        print(f"Fetching live status for train {train_no}...")
        try:
            live_data = fetch_active_run(train_no)
            result = build_live_trip(live_data, sched_path, reg, clf, feature_cols, samplers)
            outputs[train_no] = result
            print(f"  current_delay={result['current_delay']} min, "
                  f"completed={sum(1 for s in result['stations'] if s['status']=='completed')}, "
                  f"predicted={sum(1 for s in result['stations'] if s['status']=='predicted')}")
        except RailRadarError as e:
            print(f"  SKIPPED train {train_no}: {e}")

    if not outputs:
        print("No trains fetched successfully — nothing written.")
        return

    with open("output/live_trip_demo.json", "w", encoding="utf-8") as f:
        json.dump(outputs, f, indent=2)
    print("Saved -> output/live_trip_demo.json (real data)")


if __name__ == "__main__":
    main()