"""
Where does the accuracy gap come from?

Comparing our dashboard against Where Is My Train for train 22691 showed
three differences at once, and they have different causes. This script
separates them by printing, side by side:

  - what RailRadar actually reported (raw API fields)
  - what our pipeline displays after processing

Anything that differs between those two columns is OUR bug and we can fix
it. Anything where RailRadar itself disagrees with another app is an
upstream data difference we cannot fix, only disclose.

A large share of "our app disagrees with some other tracker" reports turn
out to be neither of those — they're two apps looking at two different
CALENDAR RUNS of the same train number. A train that takes >24h, or runs
non-daily (weekly/biweekly services), can have a run still in progress
from a day (or a week) ago while "today's" slot is still not-started.
This script now checks that FIRST, using the exact same fetch_active_run()
logic server.py/live_pipeline.py use, before diving into per-station
comparisons — so a run-selection mismatch shows up immediately instead of
looking like a data-accuracy problem three sections later.

Usage:
    python diagnose_accuracy.py 22691
    python diagnose_accuracy.py 22691 BPQ     # focus on one station
"""

import sys
from datetime import datetime, timedelta

from railradar_client import fetch_live_status, fetch_active_run, RailRadarError


def fmt(ts):
    if not ts:
        return "-"
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except Exception:
        return str(ts)


def _looks_unstarted_placeholder(route):
    """RailRadar pre-fills every station's arrival with the plain schedule
    (delay 0, actual == scheduled) before a run has actually departed. If
    we mistake that for real telemetry, a not-started train reads as
    'right on time so far' when nothing has been measured yet."""
    checked = [s for s in route if s.get("isHalt")]
    if not checked:
        return False
    return all(
        (s.get("delayArrival") in (0, None)) and (s.get("delayDeparture") in (0, None))
        and s.get("actualArrival") == s.get("scheduledArrival")
        for s in checked
    )


def main():
    train_no = sys.argv[1] if len(sys.argv) > 1 else "22691"
    focus = sys.argv[2].upper() if len(sys.argv) > 2 else None

    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print("=" * 78)
    print("WHICH RUN IS 'TODAY'? (checked first — this is the #1 cause of mismatches")
    print("against other trackers, especially for non-daily services)")
    print("=" * 78)

    today = yesterday = None
    try:
        today = fetch_live_status(train_no)
        print(f"  {today_str} (today):     status = {today.get('status')}")
    except RailRadarError as e:
        print(f"  {today_str} (today):     FAILED: {e}")

    try:
        yesterday = fetch_live_status(train_no, date=yesterday_str)
        print(f"  {yesterday_str} (yesterday): status = {yesterday.get('status')}")
    except RailRadarError as e:
        print(f"  {yesterday_str} (yesterday): (no run found: {e})")

    try:
        data = fetch_active_run(train_no)
    except RailRadarError as e:
        print(f"\nFAILED to resolve the active run: {e}")
        return

    picked_date = data.get("run_date", today_str)
    print(f"\n  -> fetch_active_run() picked {picked_date} (status: {data.get('status')})")
    print("     This is the exact run server.py / live_pipeline.py show on the dashboard.")

    if today is not None and today.get("status") == "not-started" and _looks_unstarted_placeholder(today.get("route", [])):
        print("\n  Today's run is 'not-started' and every station in its route still shows the")
        print("  bare schedule (0 delay, actual == scheduled) — that's RailRadar's placeholder")
        print("  pattern for a run that hasn't departed, not real telemetry.")

    if today is not None and yesterday is not None and today.get("status") != yesterday.get("status"):
        print("\n  Today and yesterday disagree on status. If a third-party site shows a picture")
        print("  that doesn't match this dashboard, check whether IT is keyed to a different")
        print("  calendar date's departure before assuming either app has a data bug — this is")
        print("  especially likely for a train that doesn't run daily (e.g. a Weekly Express).")

    print(f"\nTrain {data.get('trainNumber')}  {data.get('trainName')}")
    print(f"Status: {data.get('status')}   headline delay: {data.get('delayMinutes')} min")
    print(f"Data timestamp (lastUpdatedAt): {data.get('lastUpdatedAt')}")

    loc = data.get("currentLocation") or {}
    print(f"\nRailRadar says the train is at:")
    print(f"  {loc.get('stationCode')}  {loc.get('stationName')}")
    print(f"  sequence {loc.get('sequence')}   status {loc.get('status')}   "
          f"delay {loc.get('delayMinutes')} min")
    print(f"  segmentProgress {loc.get('segmentProgress')}   speed {loc.get('speedKmh')}")

    route = data.get("route", [])
    print(f"\nRoute has {len(route)} points in RailRadar's data.")

    # how many of those are actual halts vs intermediate reporting points
    halts = [s for s in route if s.get("isHalt")]
    print(f"  {len(halts)} are halts, {len(route) - len(halts)} are non-halt reporting points.")
    print("  Our schedule CSVs were fetched with haltsOnly=true, so the non-halt")
    print("  points are NOT in our data. If the train is currently at one of them,")
    print("  our dashboard cannot show its true position and will appear stale.")

    print("\n" + "=" * 78)
    print("RAW RAILRADAR versus WHAT WE DISPLAY")
    print("=" * 78)
    header = f"{'code':<8}{'halt':<6}{'sch arr':<9}{'act arr':<9}{'d_arr':<7}{'d_dep':<7}{'we show':<9}{'ok'}"
    print(header)
    print("-" * len(header))

    shown = 0
    for s in route:
        code = s.get("stationCode", "?")
        if focus and code != focus:
            continue
        d_arr = s.get("delayArrival")
        d_dep = s.get("delayDeparture")
        act = s.get("actualArrival")
        sch = s.get("scheduledArrival")
        if not focus and act is None and d_arr is None:
            continue   # not reached yet and nothing reported

        # what live_pipeline.py currently does:
        #   delay used  = delayDeparture if present else delayArrival
        #   clock shown = scheduled + that delay   <-- reconstructed, not reported
        used_delay = d_dep if d_dep is not None else (d_arr or 0)
        reconstructed = None
        if sch:
            try:
                base = datetime.fromisoformat(sch)
                reconstructed = (base.timestamp() + used_delay * 60)
                reconstructed = datetime.fromtimestamp(reconstructed).strftime("%H:%M")
            except Exception:
                pass

        agrees = "" if (reconstructed and fmt(act) != "-" and reconstructed != fmt(act)) else "ok"
        if agrees != "ok":
            agrees = "MISMATCH"

        print(f"{code:<8}{str(bool(s.get('isHalt'))):<6}{fmt(sch):<9}{fmt(act):<9}"
              f"{str(d_arr):<7}{str(d_dep):<7}{str(reconstructed):<9}{agrees}")
        shown += 1
        if shown > 40 and not focus:
            print("... truncated, pass a station code to focus")
            break

    print("\nHow to read this:")
    print("  'act arr' is the timestamp RailRadar actually reported.")
    print("  'we show' is what our dashboard displays, rebuilt from scheduled+delay.")
    print("  Any MISMATCH row is our bug: we should display the reported timestamp.")
    print("  If both columns agree but another app shows something different, that is")
    print("  an upstream disagreement between data providers, not a bug in our code.")


if __name__ == "__main__":
    main()