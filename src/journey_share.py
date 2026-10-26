"""
Sharing a journey with whoever is waiting at the other end.

The person meeting a train sees nothing. They text "where are you?" every
twenty minutes and get answers from someone who is often asleep or out of
signal. A read-only link solves that without anyone having to reply.

On privacy. The underlying facts are already public: anyone can look up
train 22691 and see where it is. What a share link adds is the inference
that a particular person is on that particular train, so the design keeps
that inference as contained as possible:

  - Tokens are random and long, so links cannot be guessed or enumerated.
    A sequential /share/1, /share/2 would expose every shared journey to
    anyone who could count.

  - No names anywhere. The link says "Train 22691 to Hazrat Nizamuddin",
    never "Subham's journey". Whoever is waiting already knows who they
    are waiting for, and a forwarded link should not tell a stranger.

  - Links expire on their own, by default a little after the train is
    expected to arrive. A share that stops working when it stops being
    useful needs no one to remember to revoke it.

  - Sharing can be stopped at any moment, and the link dies immediately.

Nothing is written to disk. Links live in memory and vanish when the
server stops, which is the right default for something this transient.
"""

import secrets
import threading
from datetime import datetime, timedelta

DEFAULT_EXTRA_HOURS = 2      # how long a link outlives the expected arrival
MAX_LIFETIME_HOURS = 36      # nothing lives longer than this, whatever the journey
TOKEN_BYTES = 16             # 32 hex characters, not guessable

_shares = {}                 # token -> share record
_lock = threading.Lock()


def _prune():
    """Drop anything past its expiry. Called on every access, so expired
    links disappear without needing a background task."""
    now = datetime.now()
    dead = [t for t, s in _shares.items() if s["expires_at"] < now]
    for t in dead:
        del _shares[t]


def create_share(train_no, train_name, destination, expected_arrival_iso=None,
                 extra_hours=DEFAULT_EXTRA_HOURS):
    """Returns (token, expires_at). Stores only what the view needs."""
    expires = None
    if expected_arrival_iso:
        try:
            expires = datetime.fromisoformat(expected_arrival_iso) + timedelta(hours=extra_hours)
        except Exception:
            expires = None
    if expires is None:
        expires = datetime.now() + timedelta(hours=12)

    ceiling = datetime.now() + timedelta(hours=MAX_LIFETIME_HOURS)
    expires = min(expires, ceiling)

    token = secrets.token_hex(TOKEN_BYTES)
    with _lock:
        _prune()
        _shares[token] = {
            "train_no": str(train_no),
            "train_name": train_name,
            "destination": destination,
            "created_at": datetime.now(),
            "expires_at": expires,
        }
    return token, expires


def get_share(token):
    with _lock:
        _prune()
        return _shares.get(token)


def revoke(token):
    with _lock:
        return _shares.pop(token, None) is not None


def public_view(share, trip):
    """
    What a shared link may show.

    An earlier version of this was far more restrictive, on the reasoning
    that less exposure is safer. That was miscalibrated. A train's route,
    position and timings are public: anyone can look up train 12951 without
    a link. The only thing this link discloses is that someone you know is
    aboard, and that is conveyed by the link existing at all, not by how
    much of the train's progress it shows. Withholding the route and map
    bought no privacy and made the page close to useless to the person
    standing on a platform wondering when to set off.

    So the route, the map and the per-station timings are included. What
    stays out is the machinery rather than the journey: the raw simulation
    samples, the risk model and the junction analysis, which are the
    sharer's analytical view and would only clutter this one.
    """
    stations = trip.get("stations", [])
    completed = [s for s in stations if s.get("status") == "completed"]
    ahead = [s for s in stations if s.get("status") == "predicted"]
    last = completed[-1] if completed else None
    final = ahead[-1] if ahead else (completed[-1] if completed else None)

    note = trip.get("current_position_note")
    where = (note or {}).get("station_name") or (last or {}).get("station_name")

    def trim(st):
        out = {
            "name": st.get("station_name"),
            "status": st.get("status"),
            "scheduled": st.get("scheduled_clock"),
        }
        if st.get("status") == "completed":
            out["actual"] = st.get("actual_clock")
            out["delay"] = st.get("actual_delay_min")
            out["cause"] = st.get("cause")
        else:
            out["expected"] = st.get("predicted_clock_p50")
            out["delay"] = st.get("predicted_delay_p50")
        if st.get("lat") is not None and st.get("lng") is not None:
            out["lat"] = st["lat"]
            out["lng"] = st["lng"]
        return out

    view = {
        "train_no": share["train_no"],
        "train_name": share["train_name"],
        "destination": share["destination"],
        "delay_min": trip.get("current_delay"),
        "run_status": trip.get("run_status"),
        "where": where,
        "last_update": trip.get("fetched_at"),
        "expires_at": share["expires_at"].isoformat(),
        "stations": [trim(st) for st in stations],
        "live_position": trip.get("live_position"),
        "completed_count": len(completed),
    }

    if final and final.get("status") == "predicted":
        view["arrival_expected"] = final.get("predicted_clock_p50")
        view["arrival_earliest"] = final.get("predicted_clock_p50")
        p10 = final.get("predicted_delay_p10")
        p90 = final.get("predicted_delay_p90")
        sched = final.get("scheduled_arr_min")
        if None not in (p10, p90, sched):
            from forecast_engine import clock
            view["arrival_earliest"] = clock(sched + p10)
            view["arrival_latest"] = clock(sched + p90)
    elif final:
        view["arrival_expected"] = final.get("actual_clock")

    return view
