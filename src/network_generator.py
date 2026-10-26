"""
Multi-train network simulator.

WHY THIS EXISTS: generator.py simulates every train in a vacuum. Each
trip is drawn independently, so there is literally no information about
other trains in the dataset — which is exactly why model.py and
lstm_model.py both hit the same information ceiling (log loss ~1.39 for
completely different architectures). No model can learn interaction
effects from data that contains none.

This simulator runs ALL trains on a SHARED network on a shared clock, as
a discrete-event simulation:

  - Stations are nodes; the stretch between two consecutive halts is a
    track SECTION, keyed by its unordered station pair so that trains
    running in opposite directions contend for the same physical rail.
  - Each section holds a limited number of trains (SECTION_CAPACITY).
    A train that wants to enter a full section must WAIT until a slot
    frees. That wait is a real, caused CROSSING_WAIT — produced by the
    simulation, not sampled from a distribution.
  - Priority decides who yields. Rajdhani/Shatabdi/Vande Bharat class
    trains hold precedence; slower trains give way. This is how Indian
    Railways actually dispatches.
  - Because a delayed train occupies its section for longer, delay
    PROPAGATES between trains. That propagation is the new signal.

The output carries per-leg cross-train observations that simply could
not exist before:

    occupants_ahead       how many trains already hold the next section
    occupant_max_delay    worst delay among those trains
    yielding_to_priority  whether this train is giving way to a faster one
    section_pressure      how many trains want this section in this window

Schedules are auto-discovered from data/route_*_schedule.csv, so any
train fetched with fetch_schedule.py joins the simulated network.
"""

import glob
import heapq
import re
import numpy as np
import pandas as pd

from forecast_engine import identify_junctions_and_fog_zone

RNG_SEED = 42
N_DAYS = 300              # one trip per tracked train per simulated day
SECTION_CAPACITY = 2      # trains a section holds at once; the next must wait
BACKGROUND_TRAINS_PER_DAY = 25
HAZARD_SCALE = 1.0        # scales per-leg hazard probabilities and magnitudes

# NOTE ON HAZARD_SCALE: the first version of this simulator (and generator.py
# before it) used per-leg hazard rates that look reasonable in isolation but
# compound badly over a long route. On a 112-halt journey a 5% chance of a
# 10-minute slowdown per leg, plus winter fog firing on more than half of
# fog-corridor legs, drove median end-of-journey delay past 100 minutes —
# far worse than real mail/express services. calibrate_network.py sweeps
# this scale so the simulated delay distribution is plausible instead of
# being whatever the raw rates happened to produce.
HIGH_PRIORITY_PATTERNS = ("RAJDHANI", "SHATABDI", "VANDE", "DURONTO", "TEJAS")

# A real trunk corridor carries hundreds of services a day. Simulating only
# the handful of trains we happen to track is not a network — with nobody
# else on the rail there is nothing to contend with, and the first version
# of this file produced contention on 5 legs out of 51,000. BACKGROUND
# TRAFFIC fixes that: unnamed services run real portions of the same route
# on their own timings and hold sections exactly like tracked trains do.
# They are never recorded as training rows; they exist to make occupancy
# realistic. Crucially, their occupancy is OBSERVABLE to a tracked train
# through occupants_ahead / occupant_max_delay, so the resulting waits are
# genuinely predictable from features rather than being unexplained noise.


# ----------------------------------------------------------------------
# schedule loading
# ----------------------------------------------------------------------

def discover_schedules():
    routes = {}
    for path in sorted(glob.glob("data/route_*_schedule.csv")):
        m = re.search(r"route_(\d+)_schedule\.csv$", path)
        if m:
            routes[int(m.group(1))] = path
    return routes


def unwrap_times(arr_min_series):
    """
    Older schedule CSVs stored clock-of-day minutes, so a journey crossing
    midnight goes ... 1433, 6, 58 ... Newer ones (with day offsets) are
    already monotonic. Detecting and unwrapping here means both formats
    share one absolute-minutes clock, which the simulation requires.
    """
    vals = list(arr_min_series.astype(float))
    out, offset = [], 0
    for i, v in enumerate(vals):
        if i and v + offset < out[-1]:
            offset += 1440
        out.append(v + offset)
    return out


def train_priority(train_name: str) -> int:
    upper = str(train_name).upper()
    return 1 if any(p in upper for p in HIGH_PRIORITY_PATTERNS) else 2


def load_network():
    """Returns a list of per-train route dicts on a shared absolute clock."""
    trains = []
    for train_no, path in discover_schedules().items():
        sched = pd.read_csv(path)
        junctions, fog_zone = identify_junctions_and_fog_zone(sched)
        abs_arr = unwrap_times(sched["arr_min"])
        halts = sched["halt_min"].fillna(0).astype(float).tolist()
        runs = sched["segment_travel_min"].fillna(0).astype(float).tolist()
        dists = sched["segment_distance_km"].fillna(0).astype(float).tolist()

        trains.append(dict(
            train_no=train_no,
            name=str(sched["Train Name"].iloc[0]).strip(),
            priority=train_priority(sched["Train Name"].iloc[0]),
            codes=sched["Station Code"].tolist(),
            seqs=sched["SEQ"].tolist(),
            abs_arr=abs_arr,
            halt=halts,
            run=runs,
            dist=dists,
            junctions=junctions,
            fog_zone=fog_zone,
        ))
    return trains


def section_key(a, b):
    """Unordered, so opposing trains contend for the same physical rail."""
    return (a, b) if a <= b else (b, a)


# ----------------------------------------------------------------------
# the simulation
# ----------------------------------------------------------------------

def make_background_runners(trains, day_index, rng, n_background=None):
    """Unnamed services sharing the same rail, to make occupancy realistic."""
    day0 = day_index * 1440
    runners = []
    for _ in range(BACKGROUND_TRAINS_PER_DAY if n_background is None else n_background):
        base = trains[int(rng.integers(0, len(trains)))]
        n = len(base["codes"])
        if n < 4:
            continue
        span = int(rng.integers(3, min(16, n)))
        start = int(rng.integers(0, n - span))
        idx = list(range(start, start + span))
        reverse = rng.random() < 0.5

        codes = [base["codes"][i] for i in idx]
        run = [base["run"][i] for i in idx]
        halt = [base["halt"][i] for i in idx]
        if reverse:
            codes, run, halt = codes[::-1], run[::-1], halt[::-1]

        # own departure time and pace, independent of the tracked trains
        depart = day0 + float(rng.integers(0, 1440))
        pace = float(rng.uniform(0.85, 1.35))
        abs_arr, clock = [], depart
        for k in range(len(codes)):
            if k:
                clock += max(2.0, run[k] * pace)
            abs_arr.append(clock)
            clock += halt[k]

        runners.append(dict(
            train_no=-1, name="background", priority=int(rng.integers(1, 4)),
            codes=codes, seqs=list(range(1, len(codes) + 1)),
            abs_arr=abs_arr, halt=halt, run=run,
            dist=[base["dist"][i] for i in idx],
            junctions=base["junctions"], fog_zone=base["fog_zone"],
            tracked=False,
        ))
    return runners


def simulate_day(trains, day_index, rng, records, capacity=None, n_background=None, hazard=None):
    """
    Event-driven simulation of one calendar day across every train.

    Event tuple: (time, tie_break, priority, train_index, leg_index)
    We pop the earliest event; ties are broken by priority so a faster
    train claims a contested section first, which is the whole point.
    """
    day0 = day_index * 1440
    month = int(rng.integers(0, 12))
    is_winter = month in (11, 0, 1)
    dow = day_index % 7

    # section -> list of release times currently held
    occupancy = {}
    # section -> count of trains that requested it today (pressure proxy)
    demand = {}
    # per-train running state
    state = {}

    cap = SECTION_CAPACITY if capacity is None else capacity
    hz = HAZARD_SCALE if hazard is None else hazard
    runners = [dict(t, tracked=True) for t in trains] + make_background_runners(trains, day_index, rng, n_background)

    events = []
    counter = 0

    for ti, t in enumerate(runners):
        if not t["tracked"]:
            st0 = float(rng.gamma(2.0, 6.0)) if rng.random() < 0.35 else 0.0
            state[ti] = dict(delay=st0, prev_cause="NONE")
            heapq.heappush(events, (t["abs_arr"][0] + st0, counter, t["priority"], ti, 0))
            counter += 1
            continue

        origin_delay = float(rng.gamma(2.0, 8.0)) if rng.random() < 0.30 else 0.0
        state[ti] = dict(delay=origin_delay, prev_cause="CASCADING" if origin_delay > 0.5 else "NONE")
        depart_at = day0 + t["abs_arr"][0] + origin_delay
        heapq.heappush(events, (depart_at, counter, t["priority"], ti, 0))
        counter += 1

        records.append(dict(
            train_no=t["train_no"], trip_id=day_index, seq=t["seqs"][0], station_code=t["codes"][0],
            day_of_week=dow, month=month, hour_of_day=int((depart_at // 60) % 24),
            scheduled_arr_min=t["abs_arr"][0],
            halt_scheduled=t["halt"][0], halt_actual=t["halt"][0],
            run_scheduled=0.0, run_actual=0.0,
            delay_increment=round(origin_delay, 1) if origin_delay else 0.0,
            actual_delay_min=round(origin_delay, 1),
            primary_cause=state[ti]["prev_cause"],
            is_fog_zone=t["codes"][0] in t["fog_zone"],
            is_junction=t["codes"][0] in t["junctions"],
            occupants_ahead=0, occupant_max_delay=0.0,
            yielding_to_priority=0, section_pressure=0,
        ))

    while events:
        now, _, _, ti, leg = heapq.heappop(events)
        t = runners[ti]
        if leg + 1 >= len(t["codes"]):
            continue

        st = state[ti]
        frm, to = t["codes"][leg], t["codes"][leg + 1]
        key = section_key(frm, to)

        # ---- contend for the section ----
        held = [r for r in occupancy.get(key, []) if r[0] > now]
        occupancy[key] = held
        demand[key] = demand.get(key, 0) + 1

        occupants_ahead = len(held)
        occupant_max_delay = max([h[1] for h in held], default=0.0)
        yielding = int(any(h[2] < t["priority"] for h in held))

        wait = 0.0
        if occupants_ahead >= cap:
            # must wait for the earliest slot to free
            earliest_free = min(h[0] for h in held)
            wait = max(0.0, earliest_free - now)
            now += wait

        # ---- independent hazards on this leg ----
        causes = {}
        if wait > 0.5:
            causes["CROSSING_WAIT"] = wait

        hour = int((now // 60) % 24)
        in_fog = t["codes"][leg + 1] in t["fog_zone"]
        if is_winter and in_fog and (hour <= 8 or hour >= 22) and rng.random() < 0.55 * hz:
            causes["FOG"] = float(rng.gamma(2.2, 9.0 * hz))

        p_slow = (0.05 + 0.02 * min(t["dist"][leg + 1] / 20.0, 1.5)) * hz
        if st["prev_cause"] == "SECTION_SLOW":
            p_slow += 0.08 * hz
        if rng.random() < p_slow:
            causes["SECTION_SLOW"] = float(rng.gamma(1.6, 6.0 * hz))

        if rng.random() < 0.005 * hz:
            causes["TECHNICAL"] = float(rng.uniform(20, 75))

        run_extra = (causes.get("FOG", 0.0) + causes.get("SECTION_SLOW", 0.0)
                     + causes.get("TECHNICAL", 0.0))
        run_actual = t["run"][leg + 1] + run_extra
        arrive_at = now + run_actual

        # hold the section until arrival
        occupancy[key].append((arrive_at, st["delay"], t["priority"]))

        # ---- halt at the arriving station ----
        halt_sched = t["halt"][leg + 1]
        halt_actual = halt_sched
        if halt_sched > 0:
            p_over = (0.18 if t["codes"][leg + 1] in t["junctions"] else 0.10) * hz
            if rng.random() < p_over:
                over = float(rng.gamma(1.3, 4.0 * hz))
                causes["HALT_OVERRUN"] = over
                halt_actual = halt_sched + over

        new_delay = arrive_at - (day0 + t["abs_arr"][leg + 1])

        # recovery: padded schedule lets time be clawed back
        if not causes and new_delay > 2:
            p_rec = 0.35 + (0.25 if st["prev_cause"] == "TECHNICAL" else 0.0)
            if rng.random() < p_rec:
                give = min(float(rng.gamma(1.2, 3.5)), new_delay)
                causes["RECOVERY"] = -give
                new_delay -= give
                arrive_at -= give

        new_delay = max(0.0, new_delay)
        increment = new_delay - st["delay"]
        primary = max(causes.items(), key=lambda kv: abs(kv[1]))[0] if causes else "NONE"

        if t["tracked"]:
            records.append(dict(
                train_no=t["train_no"], trip_id=day_index, seq=t["seqs"][leg + 1],
                station_code=t["codes"][leg + 1],
                day_of_week=dow, month=month, hour_of_day=hour,
                scheduled_arr_min=t["abs_arr"][leg + 1],
                halt_scheduled=halt_sched, halt_actual=round(halt_actual, 1),
                run_scheduled=t["run"][leg + 1], run_actual=round(run_actual, 1),
                delay_increment=round(increment, 1),
                actual_delay_min=round(new_delay, 1),
                primary_cause=primary,
                is_fog_zone=in_fog,
                is_junction=t["codes"][leg + 1] in t["junctions"],
                # ---- the cross-train observations that did not exist before ----
                occupants_ahead=occupants_ahead,
                occupant_max_delay=round(occupant_max_delay, 1),
                yielding_to_priority=yielding,
                section_pressure=demand[key],
            ))

        st["delay"] = new_delay
        st["prev_cause"] = primary

        if leg + 2 < len(t["codes"]):
            heapq.heappush(events, (arrive_at + halt_actual, counter, t["priority"], ti, leg + 1))
            counter += 1


def run_simulation(capacity=None, n_background=None, hazard=None, n_days=N_DAYS, seed=RNG_SEED, quiet=False):
    """Runs the network simulation and returns the record DataFrame."""
    rng = np.random.default_rng(seed)
    trains = load_network()
    if not trains:
        raise RuntimeError("No schedules found in data/.")
    records = []
    for day in range(n_days):
        simulate_day(trains, day, rng, records, capacity=capacity, n_background=n_background, hazard=hazard)
    return pd.DataFrame.from_records(records)


def main():
    rng = np.random.default_rng(RNG_SEED)
    trains = load_network()
    if not trains:
        print("No schedules found in data/. Run fetch_schedule.py first.")
        return

    print(f"Network: {len(trains)} train(s)")
    for t in trains:
        print(f"  {t['train_no']}  priority {t['priority']}  {len(t['codes'])} halts  {t['name'][:44]}")

    # how much physical rail is actually shared?
    all_sections = [set(section_key(t["codes"][i], t["codes"][i+1]) for i in range(len(t["codes"])-1))
                    for t in trains]
    shared = set()
    for i in range(len(all_sections)):
        for j in range(i + 1, len(all_sections)):
            shared |= (all_sections[i] & all_sections[j])
    print(f"Shared track sections between trains: {len(shared)}")
    if not shared:
        print("  WARNING: no shared sections, so no contention can occur and this")
        print("  simulation will produce no cross-train signal. Add trains whose")
        print("  routes overlap before relying on the network features.")

    records = []
    for day in range(N_DAYS):
        simulate_day(trains, day, rng, records)

    out = pd.DataFrame.from_records(records)
    out.to_csv("output/network_trips.csv", index=False)

    print(f"\nRows: {len(out)}")
    print("\nCause distribution:")
    print(out["primary_cause"].value_counts())
    print("\nCross-train signal present:")
    print(f"  legs entering an occupied section : {(out.occupants_ahead > 0).sum()}")
    print(f"  legs that yielded to a faster train: {(out.yielding_to_priority == 1).sum()}")
    print(f"  mean delay increment, clear section : {out.loc[out.occupants_ahead == 0, 'delay_increment'].mean():.2f} min")
    print(f"  mean delay increment, busy section  : {out.loc[out.occupants_ahead > 0, 'delay_increment'].mean():.2f} min")
    print("\nSaved -> output/network_trips.csv")


if __name__ == "__main__":
    main()