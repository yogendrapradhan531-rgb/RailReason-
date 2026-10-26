"""
Real network-level cascading risk: checks whether OTHER trains are also
scheduled to be at an upcoming junction around the same time as ours,
and whether those trains are themselves delayed — a genuine precedence/
crossing-conflict signal, not a synthetic guess.

Uses RailRadar's Live Station Board endpoint (/v1/stations/{code}/live).

NOTE ON TESTING: I don't have a confirmed response schema for this
specific endpoint (only a docs summary, not a sample response) — same
situation as fetch_schedule.py originally was. Run the manual test at
the bottom of this file first; if it errors on a missing field, send me
the DEBUG output it prints and I'll fix the field-name mapping, same as
we did for the timetable endpoint.
"""

import requests
from datetime import datetime
from railradar_client import API_BASE, _headers, RailRadarError

CONFLICT_WINDOW_MINUTES = 60   # TEMPORARILY WIDENED for testing — was 20. Narrow back down once confirmed working.
DELAY_THRESHOLD_MINUTES = 2    # TEMPORARILY LOWERED for testing — was 15. Raise back once confirmed working.


def fetch_station_board(station_code: str) -> list[dict]:
    """Returns the list of trains at this station's live board."""
    url = f"{API_BASE}/stations/{station_code}/live"
    resp = requests.get(url, headers=_headers(), timeout=15)

    if resp.status_code == 401:
        raise RailRadarError("Unauthorized — check RAILRADAR_API_KEY.")
    if resp.status_code == 404:
        raise RailRadarError(f"Station {station_code} not found on RailRadar's live board.")
    if resp.status_code == 429:
        raise RailRadarError("Rate limited on the station-board endpoint — back off and retry shortly.")
    resp.raise_for_status()
    body = resp.json()

    if not body.get("success"):
        err = body.get("error", {})
        raise RailRadarError(f"RailRadar API error [{err.get('code')}]: {err.get('message')}")

    data = body["data"]
    # defensive: the board might be a flat list, or nested under a key like "trains"/"board"
    if isinstance(data, list):
        return data
    for key in ("trains", "board", "arrivals", "departures"):
        if key in data:
            return data[key]

    raise RailRadarError(
        f"Couldn't find the train list in the station board response. Top-level keys: {list(data.keys())}\n"
        "Send this back and I'll fix the parsing."
    )


def assess_conflict_risk(our_train_no: str, our_eta_min: int, station_code: str) -> list[dict]:
    """
    Returns a list of conflict-risk dicts for other trains due at this
    station within CONFLICT_WINDOW_MINUTES of our train's ETA, that are
    themselves delayed by more than DELAY_THRESHOLD_MINUTES.
    """
    try:
        board = fetch_station_board(station_code)
    except RailRadarError as e:
        print(f"  (couldn't fetch station board for {station_code}: {e})")
        return []

    risks = []
    checked = 0
    for entry in board:
        train_info = entry.get("train", {})
        stop_info = entry.get("stop", {})
        live_info = entry.get("live", {})

        train_no = str(train_info.get("number") or "")
        if train_no == str(our_train_no) or not train_no:
            continue

        sched_time = stop_info.get("arrival") or stop_info.get("departure")
        delay_min = live_info.get("delayMinutes")
        if delay_min is None or sched_time is None:
            continue

        try:
            entry_min = _time_str_to_minutes(sched_time)
        except Exception:
            continue

        if entry_min is None:
            continue

        checked += 1
        time_gap = abs(entry_min - (our_eta_min % 1440))
        time_gap = min(time_gap, 1440 - time_gap)  # handle midnight wraparound

        if time_gap <= CONFLICT_WINDOW_MINUTES and delay_min >= DELAY_THRESHOLD_MINUTES:
            risks.append({
                "train_no": train_no,
                "train_name": train_info.get("name") or "",
                "delay_min": delay_min,
                "scheduled_time": sched_time,
                "time_gap_min": round(time_gap, 1),
            })

    print(f"    (checked {checked}/{len(board)} board entries with usable time+delay data "
          f"for {station_code}, our_eta={our_eta_min%1440}min-of-day)")
    return risks


def _time_str_to_minutes(t: str) -> int | None:
    import re
    m = re.match(r"^(\d{1,2}):(\d{2})", str(t))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


# ============================================================
# Quick manual test
# ============================================================

if __name__ == "__main__":
    import sys
    station = sys.argv[1] if len(sys.argv) > 1 else "BRC"  # Vadodara Jn, a junction on 12951's route

    print(f"Fetching live board for station {station}...")
    try:
        board = fetch_station_board(station)
        print(f"Got {len(board)} entries.")
        if board:
            print("First entry (raw):", board[0])
    except RailRadarError as e:
        print(f"FAILED: {e}")