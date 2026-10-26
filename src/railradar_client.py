"""
RailRadar API client — a real, documented REST API for Indian Railways
live train data (https://railradar.in/docs). This replaces the NTES
scraping approach entirely: no CSRF tokens, no HTML parsing, no
guessing DOM selectors, just a standard REST call with an API key.

Setup:
  1. Sign up free at https://railradar.in/login (no card needed)
  2. Generate an API key at https://railradar.in/developers
  3. Set it as an environment variable before running anything that
     uses this module:

       Windows (PowerShell):  $env:RAILRADAR_API_KEY = "rr_live_..."
       Windows (cmd):         set RAILRADAR_API_KEY=rr_live_...
       Mac/Linux:             export RAILRADAR_API_KEY="rr_live_..."

     (Or just hardcode it in API_KEY below for quick testing — just
     don't commit it anywhere public.)

Free tier is 1,000 requests/month — plenty for a hackathon demo if you
poll every couple of minutes for a couple of trains, but don't poll so
aggressively you burn through it while testing. A poll every 2 minutes
for 2 trains is 2 * 30 * 24 * 60 / 2 ≈ 43,200/month if left running
24/7 — turn it off between demo sessions, or raise REFRESH_INTERVAL in
server.py.
"""

import os
import time
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.railradar.in/v1"
API_KEY = os.environ.get("RAILRADAR_API_KEY", "")
# Never hardcode the key here. It is read from .env, which is gitignored.
# A key committed to a public repo is compromised the moment it is pushed,
# and rotating it is the only fix.


class RailRadarError(Exception):
    pass


def _headers():
    if not API_KEY:
        raise RailRadarError(
            "No API key set. Set the RAILRADAR_API_KEY environment variable, "
            "or hardcode it in railradar_client.py's API_KEY constant."
        )
    return {"Authorization": f"Bearer {API_KEY}"}


def fetch_live_status(train_no: str, date: str = None) -> dict:
    """
    Calls GET /v1/trains/{number}/live and returns the parsed `data`
    object (see https://railradar.in/docs/live-train-status for the
    full schema). Raises RailRadarError on any failure.
    """
    url = f"{API_BASE}/trains/{train_no}/live"
    params = {}
    if date:
        params["date"] = date

    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=15)
    except requests.RequestException as e:
        raise RailRadarError(f"Network error calling RailRadar: {e}") from e

    if resp.status_code == 401:
        raise RailRadarError("Unauthorized — check your RAILRADAR_API_KEY is correct and active.")
    if resp.status_code == 404:
        raise RailRadarError(f"Train {train_no} not found for the given date (not running today?).")
    if resp.status_code == 429:
        raise RailRadarError("Rate limited (429) — likely a short-term burst limit (too many requests in "
                              "a few seconds), not necessarily the monthly quota. Check railradar.in/developers "
                              "for your actual remaining monthly usage before assuming the worst.")
    if resp.status_code == 503:
        raise RailRadarError("RailRadar's upstream data source is temporarily unavailable — try again shortly.")

    resp.raise_for_status()
    body = resp.json()

    if not body.get("success"):
        err = body.get("error", {})
        raise RailRadarError(f"RailRadar API error [{err.get('code')}]: {err.get('message')}")

    return body["data"]


def fetch_active_run(train_no: str) -> dict:
    """
    Returns the run a user actually cares about: the one currently moving.

    A long-distance train can have two runs in play at once. 12951 takes
    about 15 hours, so at 13:00 yesterday's departure is still somewhere
    near Kota while today's has not left Mumbai. RailRadar's date
    auto-detect returns TODAY's run, correctly reporting not-started,
    while other trackers show yesterday's run in motion. Both are right;
    they answer different questions.

    This prefers the run in motion:
      1. ask for today's run
      2. if it has not started, ask for yesterday's
      3. use yesterday's only if it is actually running

    The extra call is only made when today's run has not started, so the
    cost against the monthly quota is small.
    """
    today = fetch_live_status(train_no)
    if today.get("status") != "not-started":
        return today

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    time.sleep(2)  # this is a second call in quick succession; pace it
    try:
        prior = fetch_live_status(train_no, date=yesterday)
    except RailRadarError as e:
        print(f"  (no run found for {train_no} on {yesterday}: {e})")
        return today

    if prior.get("status") == "running":
        loc = (prior.get("currentLocation") or {}).get("stationName", "unknown")
        print(f"  using yesterday's run of {train_no}, still running near {loc} "
              f"(today's departs later and has not started)")
        prior["run_date"] = yesterday
        return prior

    # Say so explicitly. Silence here is indistinguishable from the check
    # never happening, and "yesterday's run already finished" is a real and
    # useful answer rather than a failure.
    print(f"  yesterday's run of {train_no} is {prior.get('status')}, so showing "
          f"today's run, which has not departed yet")
    return today


# ============================================================
# Quick manual test
# ============================================================

if __name__ == "__main__":
    import sys

    train_no = sys.argv[1] if len(sys.argv) > 1 else "13007"
    date = sys.argv[2] if len(sys.argv) > 2 else None  # format: YYYY-MM-DD

    print(f"Fetching live status for train {train_no}" + (f" on {date}" if date else " (auto-detect date)") + "...")
    try:
        data = fetch_live_status(train_no, date=date)
        print(f"Train: {data.get('trainName')} ({data.get('trainNumber')})")
        print(f"Status: {data.get('status')}, delay: {data.get('delayMinutes')} min")
        print(f"Current location: {data.get('currentLocation')}")
        print(f"Route has {len(data.get('route', []))} stations")
        for s in data.get("route", [])[:5]:
            print(f"  seq {s['sequence']}: {s['stationCode']} ({s['stationName']}) — {s['status']}, "
                  f"delay_arr={s.get('delayArrival')}, delay_dep={s.get('delayDeparture')}")
    except RailRadarError as e:
        print(f"FAILED: {e}")