"""
Does cross-train information break the information ceiling?

model.py and lstm_model.py both landed at log loss ~1.39 on data where
every train was simulated in isolation. Two very different architectures
agreeing that closely pointed at a data limit rather than a model limit.

network_generator.py produces the same kind of data, except trains now
share track sections and block each other, and each leg carries what was
observable about the section it was about to enter.

This script trains the SAME model twice on the SAME split — once on the
original feature set, once with the network features added — so any
difference is attributable to the information, not the model.

On leakage: the network features are all observable at the moment the
train requests the section, i.e. before it travels the leg being
predicted. occupants_ahead is who is already in the section; it is not
derived from what happens afterwards.
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import GroupShuffleSplit
from sklearn.metrics import log_loss, accuracy_score, f1_score
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline

BASE_NUM = [
    "current_delay", "cum_avg_delay_so_far", "delay_trend", "stations_travelled",
    "scheduled_run_min", "scheduled_halt_min", "segment_distance_km", "hour_of_day",
    "route_position_fraction",
]
BASE_BOOL = ["is_fog_zone", "is_junction", "is_winter", "is_night_or_early"]
BASE_CAT = ["day_of_week", "prev_cause"]

NETWORK_NUM = ["occupants_ahead", "occupant_max_delay", "section_pressure"]
NETWORK_BOOL = ["yielding_to_priority"]


def build_leg_table(trips: pd.DataFrame) -> pd.DataFrame:
    trips = trips.sort_values(["train_no", "trip_id", "seq"]).reset_index(drop=True)
    rows = []
    for (train_no, trip_id), g in trips.groupby(["train_no", "trip_id"]):
        g = g.reset_index(drop=True)
        total = g["scheduled_arr_min"].max() - g["scheduled_arr_min"].min()
        for i in range(1, len(g)):
            cur, nxt = g.iloc[i - 1], g.iloc[i]
            win = g.iloc[max(0, i - 4):i]
            trend = ((win["actual_delay_min"].iloc[-1] - win["actual_delay_min"].iloc[0]) / len(win)
                     if len(win) >= 2 else 0.0)
            frac = ((nxt.scheduled_arr_min - g["scheduled_arr_min"].min()) / total) if total else 0.0
            rows.append(dict(
                train_no=train_no, trip_id=trip_id,
                current_delay=cur.actual_delay_min,
                cum_avg_delay_so_far=g.iloc[:i]["actual_delay_min"].mean(),
                delay_trend=round(trend, 2),
                stations_travelled=i,
                scheduled_run_min=nxt.run_scheduled,
                scheduled_halt_min=nxt.halt_scheduled,
                segment_distance_km=0.0,
                hour_of_day=nxt.hour_of_day,
                route_position_fraction=round(frac, 3),
                prev_cause=cur.primary_cause,
                day_of_week=nxt.day_of_week,
                is_fog_zone=int(bool(nxt.is_fog_zone)),
                is_junction=int(bool(nxt.is_junction)),
                is_winter=int(nxt.month in (11, 0, 1)),
                is_night_or_early=int(nxt.hour_of_day <= 8 or nxt.hour_of_day >= 22),
                # observable at the moment the section is requested
                occupants_ahead=nxt.occupants_ahead,
                occupant_max_delay=nxt.occupant_max_delay,
                section_pressure=nxt.section_pressure,
                yielding_to_priority=nxt.yielding_to_priority,
                target_cause=nxt.primary_cause,
            ))
    return pd.DataFrame.from_records(rows)


def brier(y_idx, proba):
    oh = np.zeros_like(proba)
    oh[np.arange(len(y_idx)), y_idx] = 1.0
    return float(np.mean(np.sum((proba - oh) ** 2, axis=1)))


def evaluate(name, df, num, boo, cat, train_idx, test_idx):
    cols = num + boo + cat
    tr, te = df.iloc[train_idx], df.iloc[test_idx]
    pipe = Pipeline([
        ("prep", ColumnTransformer([("cat", OneHotEncoder(handle_unknown="ignore"), cat)],
                                   remainder="passthrough")),
        ("model", RandomForestClassifier(random_state=42, n_estimators=300, max_depth=10)),
    ])
    pipe.fit(tr[cols], tr["target_cause"])
    classes = list(pipe.named_steps["model"].classes_)
    proba = pipe.predict_proba(te[cols])
    y_true = te["target_cause"].values
    y_idx = np.array([classes.index(c) for c in y_true])

    counts = tr["target_cause"].value_counts(normalize=True)
    base_row = np.array([counts.get(c, 0.0) for c in classes])
    base_row = base_row / base_row.sum()
    base = np.tile(base_row, (len(te), 1))

    ll = log_loss(y_true, proba, labels=classes)
    llb = log_loss(y_true, base, labels=classes)
    br = brier(y_idx, proba)
    pred = pipe.predict(te[cols])
    acc = accuracy_score(y_true, pred)
    mf1 = f1_score(y_true, pred, average="macro", zero_division=0)

    print(f"{name:34s} logloss={ll:.4f}  brier={br:.4f}  acc={acc:.3f}  macroF1={mf1:.3f}")
    return dict(name=name, ll=ll, llb=llb, brier=br, acc=acc, mf1=mf1, pipe=pipe, cols=cols)


def main():
    trips = pd.read_csv("output/network_trips.csv")
    print(f"Network trips: {len(trips)} rows, {trips.groupby(['train_no','trip_id']).ngroups} journeys")

    df = build_leg_table(trips)
    print(f"Leg table: {df.shape}")
    print(f"Legs entering an occupied section: {(df.occupants_ahead > 0).sum()}")

    groups = df["train_no"].astype(str) + "_" + df["trip_id"].astype(str)
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    train_idx, test_idx = next(gss.split(df, groups=groups))

    print()
    without = evaluate("without network features", df, BASE_NUM, BASE_BOOL, BASE_CAT, train_idx, test_idx)
    with_net = evaluate("with network features", df,
                        BASE_NUM + NETWORK_NUM, BASE_BOOL + NETWORK_BOOL, BASE_CAT,
                        train_idx, test_idx)
    print(f"{'class-frequency baseline':34s} logloss={without['llb']:.4f}")

    d_ll = without["ll"] - with_net["ll"]
    print()
    print(f"Log loss improvement from network features: {d_ll:+.4f}")
    if d_ll > 0.02:
        print("The ceiling moved. Cross-train information is genuinely learnable here.")
    elif d_ll > 0.005:
        print("A small but real improvement.")
    else:
        print("No meaningful improvement — the ceiling held.")

    # which features the model actually leaned on
    rf = with_net["pipe"].named_steps["model"]
    enc = with_net["pipe"].named_steps["prep"].named_transformers_["cat"]
    names = list(enc.get_feature_names_out(BASE_CAT)) + \
            [c for c in with_net["cols"] if c not in BASE_CAT]
    imp = sorted(zip(names, rf.feature_importances_), key=lambda x: -x[1])[:10]
    print("\nMost useful features, with network information available:")
    for n, v in imp:
        mark = "  <-- network" if n in (NETWORK_NUM + NETWORK_BOOL) else ""
        print(f"  {n:26s} {v:.4f}{mark}")

    # is the crossing-wait class specifically better predicted?
    print("\nCrossing-wait recall, the class network information should help most:")
    for res in (without, with_net):
        te = df.iloc[test_idx]
        pred = res["pipe"].predict(te[res["cols"]])
        mask = te["target_cause"].values == "CROSSING_WAIT"
        if mask.sum():
            rec = (pred[mask] == "CROSSING_WAIT").mean()
            print(f"  {res['name']:32s} {rec:.3f}  ({int(mask.sum())} cases)")


if __name__ == "__main__":
    main()