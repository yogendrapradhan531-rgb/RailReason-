"""
Fetches a train's static timetable from RailRadar's /v1/trains/{number}
endpoint and writes it as a schedule CSV matching the exact column
format the rest of the pipeline expects (same shape as your originally
uploaded route_*_schedule.csv files) — so live_pipeline.py, attribution.py,
and forecast_engine.py all work unchanged, for ANY train RailRadar tracks,
not just the two you originally uploaded schedules for.

Usage:
  python fetch_schedule.py 12951
  -> writes data/route_12951_schedule.csv

Run this once per new train before using it in live_pipeline.py/server.py.
The static timetable rarely changes, so you don't need to re-run this
often — unlike the live status, which changes every few minutes.
"""

import sys
import csv
import re
from railradar_client import API_BASE, _headers, RailRadarError
import requests


def _parse_time_with_day(time_str, day_num) -> int | None:
    """
    RailRadar gives 'HH:MM' plus a separate day number (1, 2, 3...) for
    multi-day journeys. Converts to true continuous minutes-from-journey-
    start, e.g. day 2 03:15 -> 1*1440 + 3*60+15 = 1635. This is more
    precise than guessing day-rollovers from clock time alone.
    """
    if not time_str:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})", str(time_str))
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    day_offset = (int(day_num) - 1) * 1440 if day_num else 0
    return day_offset + h * 60 + mm


def fetch_and_build_schedule(train_no: str, halts_only: bool = True) -> str:
    url = f"{API_BASE}/trains/{train_no}"
    params = {"haltsOnly": "true"} if halts_only else {}

    resp = requests.get(url, headers=_headers(), params=params, timeout=15)
    if resp.status_code == 401:
        raise RailRadarError("Unauthorized — check RAILRADAR_API_KEY.")
    if resp.status_code == 404:
        raise RailRadarError(f"Train {train_no} not found in RailRadar's timetable database.")
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        err = body.get("error", {})
        raise RailRadarError(f"RailRadar API error [{err.get('code')}]: {err.get('message')}")

    data = body["data"]
    train_info = data.get("train", {})
    route = data.get("route", [])
    if not route:
        raise RailRadarError(f"No route/timetable data returned for train {train_no}.")

    train_name = train_info.get("name", "")
    source_station = route[0]["station"]
    dest_station = route[-1]["station"]

    rows = []
    prev_dep_min = None
    prev_distance = 0.0

    for stop in route:
        seq = stop["sequence"]
        station = stop["station"]
        code = station["code"]
        name = station["name"]
        lat = station.get("lat")
        lng = station.get("lng")
        distance = float(stop.get("distance", 0) or 0)

        arr_raw = stop.get("arrival")
        dep_raw = stop.get("departure")
        arr_min = _parse_time_with_day(arr_raw, stop.get("arrivalDay"))
        dep_min = _parse_time_with_day(dep_raw, stop.get("departureDay"))

        if arr_min is None and dep_min is not None:
            arr_min = dep_min  # origin station often has no arrival
        if dep_min is None and arr_min is not None:
            dep_min = arr_min  # terminus often has no departure

        halt_min = (dep_min - arr_min) if (arr_min is not None and dep_min is not None) else 0

        segment_travel_min = None
        segment_distance_km = None
        if prev_dep_min is not None and arr_min is not None:
            segment_travel_min = arr_min - prev_dep_min  # no longer needs a "crossed midnight" hack — day is explicit
            segment_distance_km = round(distance - prev_distance, 1)

        rows.append({
            "Train No": train_no,
            "Train Name": train_name,
            "SEQ": seq,
            "Station Code": code,
            "Station Name": name,
            "Lat": lat if lat is not None else "",
            "Lng": lng if lng is not None else "",
            "Arrival time": arr_raw or "",
            "Departure Time": dep_raw or "",
            "Distance": distance,
            "Source Station": source_station["code"],
            "Source Station Name": source_station.get("name", ""),
            "Destination Station": dest_station["code"],
            "Destination Station Name": dest_station.get("name", ""),
            "arr_min": arr_min if arr_min is not None else "",
            "dep_min": dep_min if dep_min is not None else "",
            "prev_dep_min": prev_dep_min if prev_dep_min is not None else "",
            "prev_distance": prev_distance,
            "segment_distance_km": segment_distance_km if segment_distance_km is not None else "",
            "segment_travel_min": segment_travel_min if segment_travel_min is not None else "",
            "halt_min": halt_min,
        })

        if dep_min is not None:
            prev_dep_min = dep_min
        prev_distance = distance

    out_path = f"data/route_{train_no}_schedule.csv"
    fieldnames = list(rows[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fetch_schedule.py <train_number>")
        sys.exit(1)

    train_no = sys.argv[1]
    try:
        path = fetch_and_build_schedule(train_no)
        print(f"Wrote {path}")
        import pandas as pd
        df = pd.read_csv(path)
        print(f"{len(df)} stations, {df['Distance'].max()} km total")
        print(df[["SEQ", "Station Code", "Station Name", "arr_min", "halt_min", "segment_travel_min"]].head(10))
    except RailRadarError as e:
        print(f"FAILED: {e}")