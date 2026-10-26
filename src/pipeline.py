"""
End-to-end live simulation for one SYNTHETIC demo trip.

For real scraped NTES data, use live_pipeline.py instead — it shares the
same forecasting engine (forecast_engine.py) so output is identically
shaped either way.
"""

import pandas as pd
import json
from attribution import infer_cause
from forecast_engine import (
    clock, load_models, empirical_magnitude_samplers,
    identify_junctions_and_fog_zone, monte_carlo_forecast,
)


def simulate_trip(train_no, trip_id, sched_path, trips_path, up_to_seq,
                   reg, clf, feature_cols, samplers, junctions, fog_zone):
    sched = pd.read_csv(sched_path)
    trips = pd.read_csv(trips_path)
    trip = trips[(trips.train_no == train_no) & (trips.trip_id == trip_id)].sort_values("seq").reset_index(drop=True)
    trip["stations_from_origin"] = range(len(trip))

    sched_by_code = sched.set_index("Station Code")
    stations_out = []
    for i, row in trip.iterrows():
        if row.seq > up_to_seq:
            break
        cause, explanation = ("NONE", "Origin station.") if i == 0 else infer_cause(row)
        srow = sched_by_code.loc[row.station_code]
        sched_min = int(srow["arr_min"])
        stations_out.append(dict(
            seq=int(row.seq), station_code=row.station_code,
            station_name=srow["Station Name"].strip(), distance_km=float(srow["Distance"]),
            status="completed",
            scheduled_arr_min=sched_min, scheduled_clock=clock(sched_min),
            actual_delay_min=round(float(row.actual_delay_min), 1),
            actual_clock=clock(sched_min + row.actual_delay_min),
            cause=cause, explanation=explanation,
        ))

    current = trip[trip.seq == up_to_seq].iloc[0]
    current_delay = float(current.actual_delay_min)
    completed = trip[trip.seq <= up_to_seq]
    cum_avg = completed["actual_delay_min"].mean()
    trend_window = completed.tail(4)["actual_delay_min"].values
    trend = (trend_window[-1] - trend_window[0]) / len(trend_window) if len(trend_window) >= 2 else 0.0

    future_sched = sched[sched["SEQ"] > up_to_seq]
    day_of_week = int(current.day_of_week)
    month = int(current.month)

    predicted_stations, cause_risk_first_leg = monte_carlo_forecast(
        current_delay=current_delay, cum_avg=cum_avg, trend=trend,
        stations_travelled=int(current.stations_from_origin),
        day_of_week=day_of_week, month=month, future_sched=future_sched,
        reg=reg, clf=clf, feature_cols=feature_cols, samplers=samplers,
        junctions=junctions, fog_zone=fog_zone,
        total_distance_km=float(sched["Distance"].max()),
        initial_prev_cause=str(getattr(current, "primary_cause", "NONE")),
    )
    stations_out.extend(predicted_stations)

    train_name = sched["Train Name"].iloc[0].strip()
    total_distance = float(sched["Distance"].max())
    return dict(
        train_no=int(train_no), train_name=train_name, trip_id=int(trip_id), up_to_seq=int(up_to_seq),
        total_distance_km=total_distance,
        current_delay=round(current_delay, 1),
        next_leg_risk=cause_risk_first_leg,
        stations=stations_out,
    )


def main():
    reg, clf, feature_cols, leg_features = load_models()
    samplers = empirical_magnitude_samplers(leg_features)

    demo_trips = {13007: 4, 15483: 5}
    outputs = {}
    for train_no, trip_id in demo_trips.items():
        sched_path = f"data/route_{train_no}_schedule.csv"
        sched = pd.read_csv(sched_path)
        junctions, fog_zone = identify_junctions_and_fog_zone(sched)
        n_stations = sched["SEQ"].max()
        up_to = int(n_stations * 0.45)

        result = simulate_trip(
            train_no=train_no, trip_id=trip_id,
            sched_path=sched_path,
            trips_path="output/trips_with_attribution.csv",
            up_to_seq=up_to, reg=reg, clf=clf, feature_cols=feature_cols,
            samplers=samplers, junctions=junctions, fog_zone=fog_zone,
        )
        outputs[str(train_no)] = result
        print(f"train {train_no} trip {trip_id}: current_delay={result['current_delay']}, "
              f"completed={sum(1 for s in result['stations'] if s['status']=='completed')}, "
              f"predicted={sum(1 for s in result['stations'] if s['status']=='predicted')}")

    with open("output/live_trip_demo.json", "w", encoding="utf-8") as f:
        json.dump(outputs, f, indent=2)
    print("Saved -> output/live_trip_demo.json")


if __name__ == "__main__":
    main()