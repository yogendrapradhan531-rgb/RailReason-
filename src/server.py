"""
Live server: polls RailRadar on a timer, runs the data through the
attribution + forecasting engine, and serves a dashboard that auto-
refreshes. Trains can be added dynamically at runtime — type any train
number in the dashboard's search box and the app fetches its schedule
(if not already cached) and starts tracking it live.

Run with:  python server.py
Then open: http://localhost:5000

Requires:  pip install flask requests beautifulsoup4
"""

import os
import re
import json
import threading
import time
import traceback
from datetime import datetime, timedelta

from flask import Flask, jsonify, Response, request

from dashboard_template import render_html, live_bootstrap, CAUSE_META
from basic_template import render_chooser, render_basic
from forecast_engine import load_models, empirical_magnitude_samplers
from live_pipeline import build_live_trip
from railradar_client import fetch_active_run, RailRadarError
from fetch_schedule import fetch_and_build_schedule
import accuracy_tracker
from connection_risk import assess_connection, summarise
import journey_share
import history

# ---- Configuration ----
# Seed with trains we've already confirmed are tracked by RailRadar.
# More can be added at runtime via the dashboard's search box — see
# _tracked_trains below, which is the live, mutable version of this.
SEED_TRAINS = {
    "12951": "data/route_12951_schedule.csv",
    "12952": "data/route_12952_schedule.csv",
    "22691": "data/route_22691_schedule.csv",
}
# Budget math: each refresh cycle calls fetch_live_status (1 call) plus up
# to MAX_JUNCTIONS_TO_CHECK station-board checks (in live_pipeline.py) per
# train. RailRadar's free tier is 1,000 calls/MONTH — every train you add
# increases the per-cycle cost, so be deliberate about what you track.
REFRESH_INTERVAL_SECONDS = 300  # 5 min — fine for active sessions, do not leave running long-term
POLL_SECONDS_CLIENT = 30        # how often the browser tab checks the server for new data

TRAIN_NO_PATTERN = re.compile(r"^\d{4,5}$")  # Indian train numbers are 4-5 digits

app = Flask(__name__)

_state = {
    "data": {},           # last known-good live_trip_demo.json-shaped dict
    "samples": {},        # train -> station -> the 300 simulated outcomes
    "last_updated": None,
    "last_error": None,
}
_tracked_trains = dict(SEED_TRAINS)  # train_no -> schedule CSV path, mutable at runtime
_models = {}  # populated once at startup: reg, clf, feature_cols, samplers
_lock = threading.Lock()


def _schedule_path(train_no: str) -> str:
    return f"data/route_{train_no}_schedule.csv"


def _strip_samples(train_no, result):
    """
    Moves the simulated outcomes out of the browser payload.

    Each forecast station carries 300 sampled delays. Left in place they
    would add roughly a megabyte per refresh across several trains, for
    data the browser never displays. They are kept server-side instead,
    where /api/connection needs them to count how many simulated arrivals
    are too late to make a transfer.
    """
    store = {}
    for st in result.get("stations", []):
        samples = st.pop("_delay_samples", None)
        if samples:
            store[st["station_code"]] = {
                "_delay_samples": samples,
                "scheduled_arr_min": st.get("scheduled_arr_min"),
                "station_name": st.get("station_name"),
                "predicted_clock_p50": st.get("predicted_clock_p50"),
            }
    with _lock:
        _state["samples"][str(train_no)] = store
    return result


def _fetch_one_train(train_no: str, sched_path: str):
    """Fetch + process a single train's live data. Raises on failure."""
    reg, clf, feature_cols, samplers = _models["reg"], _models["clf"], _models["feature_cols"], _models["samplers"]
    live_data = fetch_active_run(train_no)
    result = build_live_trip(live_data, sched_path, reg, clf, feature_cols, samplers)
    return _strip_samples(train_no, result)


def refresh_loop():
    print("Loading models...")
    reg, clf, feature_cols, leg_features = load_models()
    samplers = empirical_magnitude_samplers(leg_features)
    _models.update(reg=reg, clf=clf, feature_cols=feature_cols, samplers=samplers)
    print("Models loaded. Starting refresh loop.")

    while True:
        with _lock:
            trains_snapshot = dict(_tracked_trains)

        outputs = {}
        errors = []
        for train_no, sched_path in trains_snapshot.items():
            try:
                result = _fetch_one_train(train_no, sched_path)
                outputs[train_no] = result
                accuracy_tracker.record_cycle(train_no, result["stations"])
                print(f"[{datetime.now().isoformat()}] train {train_no}: "
                      f"current_delay={result['current_delay']} min")
            except RailRadarError as e:
                msg = f"train {train_no}: {e}"
                errors.append(msg)
                print(f"[{datetime.now().isoformat()}] RailRadar error for {msg}")
            except Exception as e:
                msg = f"train {train_no}: {e}"
                errors.append(msg)
                print(f"[{datetime.now().isoformat()}] ERROR fetching {msg}")
                traceback.print_exc()
            # Pacing is larger than it looks necessary because a single train
            # can now cost several calls: live status, possibly yesterday's run
            # as well, plus a station-board lookup. The burst limiter counts all
            # of them.
            time.sleep(10)

        with _lock:
            if outputs:
                # merge rather than replace, so a train that failed this cycle
                # doesn't vanish from the dashboard — it just goes stale
                _state["data"].update(outputs)
                _state["last_updated"] = datetime.now().isoformat()
                _state["last_error"] = None
            if errors:
                _state["last_error"] = "; ".join(errors)

        time.sleep(REFRESH_INTERVAL_SECONDS)


@app.route("/api/live")
def api_live():
    with _lock:
        if not _state["data"]:
            return jsonify({"error": _state["last_error"] or "no data yet, still fetching..."}), 503
        return jsonify(_state["data"])


@app.route("/api/track", methods=["POST"])
def api_track():
    body = request.get_json(silent=True) or {}
    train_no = str(body.get("train_no", "")).strip()

    if not TRAIN_NO_PATTERN.match(train_no):
        return jsonify({"error": f"'{train_no}' doesn't look like a valid train number (expected 4-5 digits)."}), 400

    with _lock:
        already_tracked = train_no in _tracked_trains

    sched_path = _schedule_path(train_no)

    # Fetch the schedule if we don't already have it cached — this costs
    # one RailRadar API call, only paid once per new train (schedules are
    # static and reused on every future refresh).
    if not os.path.exists(sched_path):
        try:
            print(f"Fetching schedule for new train {train_no}...")
            fetch_and_build_schedule(train_no)
        except RailRadarError as e:
            return jsonify({"error": f"Couldn't fetch schedule for {train_no}: {e}"}), 502
        except Exception as e:
            return jsonify({"error": f"Unexpected error fetching schedule for {train_no}: {e}"}), 500

    # Do an immediate live fetch so the user sees this train right away,
    # rather than waiting for the next 5-minute refresh cycle.
    try:
        if not _models:
            return jsonify({"error": "Models still loading, try again in a few seconds."}), 503
        result = _fetch_one_train(train_no, sched_path)
    except RailRadarError as e:
        return jsonify({"error": f"Couldn't fetch live status for {train_no}: {e}"}), 502
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Unexpected error fetching live status for {train_no}: {e}"}), 500

    with _lock:
        _tracked_trains[train_no] = sched_path
        _state["data"][train_no] = result
        _state["last_updated"] = datetime.now().isoformat()

    accuracy_tracker.record_cycle(train_no, result["stations"])

    print(f"Now tracking train {train_no} ({'was' if already_tracked else 'newly added,'} "
          f"current_delay={result['current_delay']} min)")
    return jsonify({"success": True, "train_no": train_no, "current_delay": result["current_delay"]})


@app.route("/api/accuracy")
def api_accuracy():
    return jsonify(accuracy_tracker.get_accuracy_stats())


@app.route("/api/share", methods=["POST"])
def api_share():
    """Creates a read-only link for whoever is waiting at the other end."""
    body = request.get_json(silent=True) or {}
    train_no = str(body.get("train_no", "")).strip()

    with _lock:
        trip = _state["data"].get(train_no)
    if not trip:
        return jsonify({"error": "That train is not being tracked."}), 404

    stations = trip.get("stations", [])
    destination = stations[-1]["station_name"] if stations else ""
    ahead = [s for s in stations if s.get("status") == "predicted"]

    # Expire the link a little after the train is due, so it stops working
    # when it stops being useful rather than lingering for a fixed day.
    expected_iso = None
    if ahead:
        try:
            eta_min = ahead[-1].get("predicted_eta_min_p50")
            first_sched = stations[0].get("scheduled_arr_min", 0)
            hours_out = max(0, (eta_min - first_sched)) / 60.0
            expected_iso = (datetime.now() + timedelta(hours=min(hours_out, 30))).isoformat()
        except Exception:
            expected_iso = None

    token, expires = journey_share.create_share(
        train_no=train_no, train_name=trip.get("train_name", ""),
        destination=destination, expected_arrival_iso=expected_iso)

    return jsonify({
        "token": token,
        "url": f"/share/{token}",
        "expires_at": expires.isoformat(),
        "note": "Anyone with this link can see the train's progress until it expires.",
    })


@app.route("/api/share/<token>", methods=["DELETE"])
def api_share_revoke(token):
    return jsonify({"revoked": journey_share.revoke(token)})


@app.route("/share/<token>")
def share_view(token):
    share = journey_share.get_share(token)
    if not share:
        return Response(_share_page(None, None), mimetype="text/html", status=404)
    with _lock:
        trip = _state["data"].get(share["train_no"])
    if not trip:
        return Response(_share_page(share, None), mimetype="text/html")
    return Response(_share_page(share, journey_share.public_view(share, trip)),
                    mimetype="text/html")


@app.route("/api/share/<token>/status")
def api_share_status(token):
    share = journey_share.get_share(token)
    if not share:
        return jsonify({"error": "This link has expired or been stopped."}), 404
    with _lock:
        trip = _state["data"].get(share["train_no"])
    if not trip:
        return jsonify({"error": "No current reading for this train."}), 503
    return jsonify(journey_share.public_view(share, trip))


@app.route("/api/history/<train_no>")
def api_history(train_no):
    """
    What we have actually observed this train do, as opposed to what we
    predict it will do. Read straight from the observation log, no model
    involved, with sample sizes attached so a handful of arrivals is not
    mistaken for a pattern.
    """
    h = history.train_history(train_no)
    h["summary"] = history.summarise(h)
    return jsonify(h)


@app.route("/api/history")
def api_history_overall():
    return jsonify(history.overall())


@app.route("/api/connection", methods=["POST"])
def api_connection():
    """
    How likely is it that this train misses a given onward departure?

    Answered by counting simulated arrivals rather than comparing a single
    predicted time, so the result is a probability instead of a yes or no.
    Costs no API calls; it is arithmetic over outcomes already computed.
    """
    body = request.get_json(silent=True) or {}
    train_no = str(body.get("train_no", "")).strip()
    station_code = str(body.get("station_code", "")).strip()
    onward = str(body.get("onward_departure", "")).strip()
    try:
        min_transfer = int(body.get("min_transfer_min", 15))
    except (TypeError, ValueError):
        min_transfer = 15
    min_transfer = max(0, min(min_transfer, 240))

    with _lock:
        station = (_state["samples"].get(train_no) or {}).get(station_code)

    if not station:
        return jsonify({"error": "No forecast is available for that station yet. "
                                 "Pick a station the train has not reached."}), 404

    result = assess_connection(station, onward, min_transfer)
    if "error" in result:
        return jsonify(result), 400
    result["summary"] = summarise(result)
    return jsonify(result)


@app.route("/")
def index():
    """
    Ask once which way in, rather than opening the full dashboard on
    everyone. Most people arrive with a single question — how late is my
    train and why — and a map, a connection calculator and a calibration
    report are a cost on the way to that answer, not a feature.

    The choice is remembered in the browser if the person asks for it, so
    this screen appears once rather than every visit.
    """
    return Response(render_chooser(), mimetype="text/html")


@app.route("/basic")
def basic():
    return Response(render_basic(CAUSE_META), mimetype="text/html")


@app.route("/advanced")
def advanced():
    html = render_html(live_bootstrap(poll_seconds=POLL_SECONDS_CLIENT))
    return Response(html, mimetype="text/html")


SHARE_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 :root{--ink:#0B1218;--panel:#131E27;--lift:#1A2832;--line:#23343F;--text:#EDE6D6;--dim:#93A8B4;
       --faint:#5F7482;--green:#4FAE73;--amber:#E7A33E;--red:#E35D42;}
 *{box-sizing:border-box}
 body{margin:0;background:var(--ink);color:var(--text);font-family:'IBM Plex Sans',system-ui,sans-serif;
      display:flex;justify-content:center;padding:18px 14px 60px;-webkit-font-smoothing:antialiased}
 .wrap{width:100%;max-width:520px}
 .num{font-family:'Space Grotesk',sans-serif;font-variant-numeric:tabular-nums}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:12px}
 .train{font-size:12.5px;color:var(--faint)}
 .name{font-family:'Space Grotesk',sans-serif;font-size:19px;font-weight:600;margin:3px 0 4px;line-height:1.25}
 .to{font-size:13px;color:var(--dim)}
 .big{font-family:'Space Grotesk',sans-serif;font-size:40px;font-weight:700;line-height:1.05}
 .lab{font-size:11.5px;color:var(--faint);margin-top:4px}
 .row{display:flex;gap:14px}.row>div{flex:1}
 .val{font-family:'Space Grotesk',sans-serif;font-size:18px;font-weight:600}
 #m{height:230px;border-radius:14px 14px 0 0;background:var(--panel)}
 .mapwrap{border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:12px}
 .mapfoot{font-size:10.5px;color:var(--faint);padding:7px 12px;border-top:1px solid var(--line)}
 .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(.86) saturate(.85)}
 .head{font-size:12.5px;color:var(--dim);font-weight:600;margin-bottom:11px}
 .stop{display:flex;gap:11px;align-items:flex-start;padding:7px 0}
 .dot{width:11px;height:11px;border-radius:50%;margin-top:4px;flex-shrink:0;border:2px solid var(--panel)}
 .sname{font-size:13.5px;font-weight:500}
 .smeta{font-size:11.5px;color:var(--faint);margin-top:1px}
 .sdel{margin-left:auto;font-family:'Space Grotesk',sans-serif;font-size:12.5px;font-weight:600;white-space:nowrap}
 .now{display:flex;align-items:center;gap:8px;padding:7px 10px;margin:4px 0;border-radius:8px;
      background:var(--lift);border:1px solid var(--line);font-size:11.5px}
 .more{font-size:12px;color:var(--faint);text-align:center;padding:8px 0;cursor:pointer}
 .foot{font-size:11.5px;color:var(--faint);line-height:1.6;text-align:center;margin-top:16px}
 .gone{text-align:center;padding:50px 20px;color:var(--dim);font-size:15px;line-height:1.6}
</style></head>
<body><div class="wrap">__BODY__</div>
<script>
 const D = __DATA__;
 if (D && D.stations) {
   const pts = D.stations.filter(s => s.lat !== undefined && s.lng !== undefined);
   if (pts.length && document.getElementById('m')) {
     const map = L.map('m', {zoomControl:false, attributionControl:false});
     L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:15}).addTo(map);
     const line = pts.map(s => [s.lat, s.lng]);
     L.polyline(line,{color:'#5B9DD9',weight:3,opacity:.75}).addTo(map);
     pts.forEach(s => L.circleMarker([s.lat,s.lng],{radius:4,weight:1,
        color:s.status==='completed'?'#4FAE73':'#5F7482',
        fillColor:s.status==='completed'?'#4FAE73':'#5F7482',fillOpacity:1})
        .bindPopup('<b>'+s.name+'</b>').addTo(map));
     let bounds = L.latLngBounds(line);
     if (D.live_position) {
       const icon = L.divIcon({className:'',iconSize:[16,16],iconAnchor:[8,8],
         html:'<div style="position:relative;width:16px;height:16px;">'+
              '<div style="width:16px;height:16px;border-radius:50%;background:#4FAE73;opacity:.5;position:absolute;"></div>'+
              '<div style="width:12px;height:12px;border-radius:50%;background:#4FAE73;border:2px solid #0B1218;position:absolute;top:2px;left:2px;"></div></div>'});
       L.marker([D.live_position.lat, D.live_position.lng],{icon}).addTo(map);
       bounds.extend([D.live_position.lat, D.live_position.lng]);
     }
     map.fitBounds(bounds,{padding:[24,24]});
   }
 }
 function showAll(){ document.querySelectorAll('.hidden-stop').forEach(e=>e.style.display='flex');
                     const b=document.getElementById('more'); if(b) b.style.display='none'; }
 setInterval(async () => {
   try { const r = await fetch(window.location.pathname.replace('/share/','/api/share/')+'/status');
         if (r.ok) location.reload(); } catch (e) {}
 }, 120000);
</script>
</body></html>"""

CAUSE_COLOURS = {
    "NONE": "#55606C", "HALT_OVERRUN": "#E7A33E", "CROSSING_WAIT": "#E35D42",
    "SECTION_SLOW": "#C97A3D", "FOG": "#7FA3B8", "TECHNICAL": "#C43B3B",
    "CASCADING": "#8B6FD1", "RECOVERY": "#4FAE73",
}


def _route_html(view):
    """
    The remaining journey, which is the part someone waiting actually reads.

    Stops already passed are collapsed behind a tap. Whoever is meeting the
    train cares about where it is now and what is left, not the history.
    """
    stations = view.get("stations") or []
    done = view.get("completed_count", 0)
    rows = []

    hide_before = max(0, done - 3)   # keep the last few passed stops for context
    for i, st in enumerate(stations):
        completed = st.get("status") == "completed"
        colour = CAUSE_COLOURS.get(st.get("cause"), "#55606C") if completed else None
        dot = (f'<div class="dot" style="background:{colour}"></div>' if completed
               else '<div class="dot" style="background:#0B1218;border-color:#5F7482"></div>')

        delay = st.get("delay") or 0
        if completed:
            right = (f'<span style="color:{colour}">+{round(delay)}m</span>'
                     if delay > 0.5 else '<span style="color:var(--faint)">on time</span>')
            meta = f'{st.get("scheduled","")} scheduled, {st.get("actual","")} actual'
        else:
            right = (f'<span style="color:var(--dim)">~+{round(delay)}m</span>'
                     if delay > 0.5 else '<span style="color:var(--faint)">~on time</span>')
            meta = f'{st.get("scheduled","")} scheduled, {st.get("expected","")} expected'

        hidden = ' hidden-stop" style="display:none' if i < hide_before else ''
        rows.append(f'<div class="stop{hidden}">{dot}<div><div class="sname">{st.get("name","")}</div>'
                    f'<div class="smeta">{meta}</div></div><div class="sdel">{right}</div></div>')

        if completed and i == done - 1:
            label = ("Waiting to depart" if view.get("run_status") == "not-started"
                     else f'Train is here now, at {st.get("name","")}')
            rows.append(f'<div class="now"><span class="dot" style="background:var(--green);'
                        f'margin-top:0"></span>{label}</div>')

    more = ('<div class="more" id="more" onclick="showAll()">Show the earlier stops</div>'
            if hide_before > 0 else '')
    return f'<div class="card"><div class="head">Route</div>{more}{"".join(rows)}</div>'


def _share_page(share, view):
    import json as _json

    if share is None:
        body = ('<div class="gone">This link has expired or been stopped.<br>'
                'Ask whoever sent it for a new one.</div>')
        return (SHARE_PAGE.replace("__TITLE__", "Link expired")
                .replace("__BODY__", body).replace("__DATA__", "null"))

    title = f"Train {share['train_no']} to {share['destination']}"

    if view is None:
        body = (f'<div class="card"><div class="train">Train {share["train_no"]}</div>'
                f'<div class="name">{share["train_name"]}</div>'
                f'<div class="to">towards {share["destination"]}</div></div>'
                '<div class="gone">Waiting for the first live reading.</div>')
        return (SHARE_PAGE.replace("__TITLE__", title)
                .replace("__BODY__", body).replace("__DATA__", "null"))

    delay = view.get("delay_min") or 0
    colour = "var(--green)" if delay < 10 else ("var(--amber)" if delay < 45 else "var(--red)")
    not_started = view.get("run_status") == "not-started"

    headline = ("Not started" if not_started
                else (f"+{round(delay)}<span style='font-size:16px;font-weight:500'> min late</span>"
                      if delay > 0.5 else "On time"))

    where = view.get("where") or "-"
    arrival = view.get("arrival_expected") or "-"
    earliest, latest = view.get("arrival_earliest"), view.get("arrival_latest")

    spread = ""
    if earliest and latest and earliest != latest:
        spread = (f'<div class="lab" style="margin-top:10px">Most arrivals in our simulation landed '
                  f'between <b style="color:var(--text)">{earliest}</b> and '
                  f'<b style="color:var(--text)">{latest}</b>.</div>')

    updated = view.get("last_update") or ""
    if "T" in str(updated):
        updated = str(updated).split("T")[1][:5]

    has_map = any("lat" in st for st in (view.get("stations") or []))
    map_html = ('<div class="mapwrap"><div id="m"></div>'
                '<div class="mapfoot">Map tiles by OpenStreetMap. The marker is an estimate '
                'along a straight line between stations, not the real track.</div></div>'
                if has_map else '')

    body = f"""
    <div class="card">
      <div class="train">Train {view['train_no']}</div>
      <div class="name">{view['train_name']}</div>
      <div class="to">towards {view['destination']}</div>
    </div>
    <div class="card">
      <div class="big" style="color:{colour}">{headline}</div>
      <div class="lab">{'waiting to depart' if not_started else 'last seen at ' + where}</div>
    </div>
    <div class="card">
      <div class="row">
        <div><div class="lab">Expected at {view['destination']}</div>
             <div class="val">{arrival}</div></div>
        <div><div class="lab">Reading taken</div>
             <div class="val">{updated or '-'}</div></div>
      </div>
      {spread}
    </div>
    {map_html}
    {_route_html(view)}
    <div class="foot">
      This page updates on its own. Times are estimates, not promises.<br>
      The link stops working after the journey ends.
    </div>"""

    return (SHARE_PAGE.replace("__TITLE__", title).replace("__BODY__", body)
            .replace("__DATA__", _json.dumps(view)))


if __name__ == "__main__":
    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()
    print(f"Serving on http://localhost:5000  (refreshing RailRadar every {REFRESH_INTERVAL_SECONDS}s)")
    app.run(host="0.0.0.0", port=5000, debug=False)