"""
Two models trained on leg_features.csv:

1. Delay regressor  - predicts minutes of NEW delay accumulated on the
   upcoming leg (target_delay_increment). Used for ETA propagation.
2. Cause classifier - predicts a probability distribution over WHY that
   delay is likely to happen (target_cause), using only features knowable
   in advance. This is the forward-looking half of the explanation layer.

ON METRICS — why plain "accuracy" is the wrong headline number here:

Accuracy asks "was the single most likely cause exactly right?" But this
model's real output is a probability DISTRIBUTION (that's what the
dashboard's "Risk for the next stretch" chips show), and several causes
are deliberately near-random events in the generator — genuinely closer
to coin flips than deterministic outcomes. Judging a probabilistic
forecast by top-1 correctness throws away the useful part and makes a
well-calibrated model look bad.

So we report, in order of relevance:
  - Log loss: penalises confident wrong answers, rewards honest
    uncertainty. The standard metric for probabilistic classification.
  - Brier score (multiclass): mean squared error of the predicted
    probabilities. Lower is better.
  - Accuracy + per-class report: kept for reference/comparison, NOT as
    the headline.

Each is shown against a baseline (predicting the training set's class
frequencies for every row). Beating that baseline is the real test — an
absolute log-loss number means little on its own.

WHAT THE CURRENT NUMBERS ACTUALLY MEAN (measured, not assumed):

  Log loss 1.39 vs 1.57 baseline, Brier 0.69 vs 0.74 baseline. So the
  model IS extracting real signal — it beats a trivial class-frequency
  guesser on both probabilistic metrics.

  But per-class recall is concentrated in NONE and FOG; the model rarely
  commits to CROSSING_WAIT / HALT_OVERRUN / SECTION_SLOW / TECHNICAL.
  That is not a bug to tune away — it reflects the generator, where
  those causes are deliberately near-random events. We verified this is
  an information ceiling, not a model-choice problem: RandomForest,
  HistGradientBoosting, and several depth/regularisation settings all
  land within ~0.005 log loss and ~0.02 macro-F1 of each other. No
  algorithm can extract discrimination that isn't in the data.

  Practical consequence: the model is best read as "usually nothing
  notable, with a genuine fog signal when conditions warrant it" — which
  is exactly how the dashboard uses it (a full probability distribution
  in the risk chips, not a single confident label).

  The real unlock is better DATA, not a better model: more causally
  informative features, and eventually retraining on real observed
  outcomes accumulated by accuracy_tracker.py rather than synthetic ones.
"""

import pandas as pd
import numpy as np
import joblib
from sklearn.ensemble import GradientBoostingRegressor, RandomForestClassifier
from sklearn.model_selection import GroupShuffleSplit
from sklearn.metrics import (
    mean_absolute_error, accuracy_score, classification_report, log_loss,
)
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline

NUM_FEATURES = [
    "current_delay", "cum_avg_delay_so_far", "delay_trend", "stations_travelled",
    "scheduled_run_min", "scheduled_halt_min", "segment_distance_km", "hour_of_day",
    "route_position_fraction",
]
BOOL_FEATURES = ["is_fog_zone", "is_junction", "is_winter", "is_night_or_early"]
CAT_FEATURES = ["day_of_week", "prev_cause"]


def make_preprocessor():
    return ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore"), CAT_FEATURES),
    ], remainder="passthrough")


def split(df):
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    groups = df["train_no"].astype(str) + "_" + df["trip_id"].astype(str)
    train_idx, test_idx = next(gss.split(df, groups=groups))
    return df.iloc[train_idx], df.iloc[test_idx]


def multiclass_brier(y_true, proba, classes):
    """Mean squared error between predicted probabilities and one-hot truth."""
    class_index = {c: i for i, c in enumerate(classes)}
    onehot = np.zeros_like(proba)
    for row, label in enumerate(y_true):
        if label in class_index:
            onehot[row, class_index[label]] = 1.0
    return float(np.mean(np.sum((proba - onehot) ** 2, axis=1)))


def main():
    df = pd.read_csv("output/leg_features.csv")
    for c in BOOL_FEATURES:
        df[c] = df[c].astype(int)

    feature_cols = NUM_FEATURES + BOOL_FEATURES + CAT_FEATURES
    train_df, test_df = split(df)

    # ---- Regressor: predict delay_increment ----
    reg_pipe = Pipeline([
        ("prep", make_preprocessor()),
        ("model", GradientBoostingRegressor(random_state=42, n_estimators=250, max_depth=3, learning_rate=0.05)),
    ])
    reg_pipe.fit(train_df[feature_cols], train_df["target_delay_increment"])
    pred = reg_pipe.predict(test_df[feature_cols])
    mae = mean_absolute_error(test_df["target_delay_increment"], pred)
    baseline_mae = mean_absolute_error(test_df["target_delay_increment"], np.zeros(len(test_df)))
    print(f"[Regressor] MAE = {mae:.2f} min  (naive-zero baseline = {baseline_mae:.2f} min)")

    # ---- Classifier: predict primary_cause ----
    # NOTE: class_weight="balanced" was tried here and made things
    # measurably WORSE — it forces over-prediction of rare classes
    # (e.g. crying "FOG!" far too often), which wrecks probability
    # calibration. Removing it improved log loss 1.64 -> 1.39, Brier
    # 0.77 -> 0.69, AND top-1 accuracy 0.31 -> 0.44. Left unweighted
    # deliberately; don't re-add it without re-checking these metrics.
    clf_pipe = Pipeline([
        ("prep", make_preprocessor()),
        ("model", RandomForestClassifier(random_state=42, n_estimators=300, max_depth=10)),
    ])
    clf_pipe.fit(train_df[feature_cols], train_df["target_cause"])

    classes = clf_pipe.named_steps["model"].classes_
    proba = clf_pipe.predict_proba(test_df[feature_cols])
    y_true = test_df["target_cause"].values

    # baseline: predict the training set's class frequencies for every row
    train_counts = train_df["target_cause"].value_counts(normalize=True)
    baseline_row = np.array([train_counts.get(c, 0.0) for c in classes])
    baseline_row = baseline_row / baseline_row.sum()
    baseline_proba = np.tile(baseline_row, (len(test_df), 1))

    ll_model = log_loss(y_true, proba, labels=list(classes))
    ll_baseline = log_loss(y_true, baseline_proba, labels=list(classes))
    brier_model = multiclass_brier(y_true, proba, classes)
    brier_baseline = multiclass_brier(y_true, baseline_proba, classes)

    print()
    print("[Classifier] PROBABILISTIC METRICS (the ones that matter here):")
    print(f"  Log loss     = {ll_model:.4f}   (class-frequency baseline = {ll_baseline:.4f})"
          f"  -> {'BETTER' if ll_model < ll_baseline else 'WORSE'} than baseline")
    print(f"  Brier score  = {brier_model:.4f}   (class-frequency baseline = {brier_baseline:.4f})"
          f"  -> {'BETTER' if brier_model < brier_baseline else 'WORSE'} than baseline")

    acc = accuracy_score(y_true, clf_pipe.predict(test_df[feature_cols]))
    print()
    print(f"[Classifier] Top-1 accuracy = {acc:.3f}  (for reference only — see note at top of this file)")
    print(classification_report(y_true, clf_pipe.predict(test_df[feature_cols]), zero_division=0))

    joblib.dump({"reg": reg_pipe, "clf": clf_pipe, "feature_cols": feature_cols}, "output/models.joblib")
    print("Saved -> output/models.joblib")


if __name__ == "__main__":
    main()