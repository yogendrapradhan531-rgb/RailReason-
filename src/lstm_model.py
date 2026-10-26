"""
LSTM sequence model for delay-cause prediction.

WHY AN LSTM HERE: delay evolves station-by-station along a journey, so
the problem is genuinely sequential. The sklearn models in model.py see
each leg mostly in isolation (with prev_cause and delay_trend as crude
hand-built proxies for history). An LSTM reads the whole journey so far
and can in principle learn patterns those proxies miss — e.g. "three
consecutive small overruns tend to precede a larger one".

HONEST EXPECTATION: model.py established an information ceiling on this
data — RandomForest, HistGradientBoosting and several configurations all
landed within ~0.005 log loss of each other, because most causes in the
synthetic generator are deliberately near-random. An LSTM is very likely
to land near that same ceiling. It is worth building for the sequential
modelling capability (and it may squeeze out a small gain), but if it
ties the sklearn baseline, that is the expected result, not a failure.
The real unlock is richer data, not a deeper model.

Evaluation deliberately mirrors model.py exactly — same trip-level
split, same log loss / Brier / accuracy metrics, same class-frequency
baseline — so the comparison is apples-to-apples.

Requires: pip install torch
(Note: this file was written without a torch install available to test
against, so if it errors on first run, send the traceback and it can be
fixed quickly.)
"""

import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, accuracy_score, classification_report, f1_score
from sklearn.preprocessing import StandardScaler

SEQ_FEATURES_NUM = [
    "current_delay", "cum_avg_delay_so_far", "delay_trend", "stations_travelled",
    "scheduled_run_min", "scheduled_halt_min", "segment_distance_km", "hour_of_day",
    "route_position_fraction",
]
SEQ_FEATURES_BOOL = ["is_fog_zone", "is_junction", "is_winter", "is_night_or_early"]
PREV_CAUSE_COL = "prev_cause"
TARGET_COL = "target_cause"

MAX_LEN = 120       # longest journey in the data is 111 legs
BATCH_SIZE = 32
EPOCHS = 30
HIDDEN_SIZE = 64
LEARNING_RATE = 1e-3
PAD_LABEL = -100    # ignored by the loss function


def load_and_prepare():
    df = pd.read_csv("output/leg_features.csv")
    for c in SEQ_FEATURES_BOOL:
        df[c] = df[c].astype(int)

    causes = sorted(df[TARGET_COL].unique())
    cause_to_idx = {c: i for i, c in enumerate(causes)}

    # prev_cause is categorical -> one-hot it (a small fixed vocabulary)
    prev_causes = sorted(df[PREV_CAUSE_COL].fillna("NONE").unique())
    prev_to_idx = {c: i for i, c in enumerate(prev_causes)}

    df = df.sort_values(["train_no", "trip_id", "seq_to"]).reset_index(drop=True)
    return df, causes, cause_to_idx, prev_causes, prev_to_idx


def build_sequences(df, cause_to_idx, prev_to_idx, scaler=None, fit_scaler=False):
    """
    Returns:
      X: (n_trips, MAX_LEN, n_features) float32
      y: (n_trips, MAX_LEN) int64, PAD_LABEL where padded
      mask: (n_trips, MAX_LEN) bool, True where real data
    """
    n_prev = len(prev_to_idx)
    n_feat = len(SEQ_FEATURES_NUM) + len(SEQ_FEATURES_BOOL) + n_prev

    groups = list(df.groupby(["train_no", "trip_id"], sort=False))
    n_trips = len(groups)

    X = np.zeros((n_trips, MAX_LEN, n_feat), dtype=np.float32)
    y = np.full((n_trips, MAX_LEN), PAD_LABEL, dtype=np.int64)
    mask = np.zeros((n_trips, MAX_LEN), dtype=bool)

    # fit the scaler on numeric columns across all rows first
    numeric_all = df[SEQ_FEATURES_NUM].values.astype(np.float32)
    if fit_scaler:
        scaler = StandardScaler().fit(numeric_all)

    for gi, (_, g) in enumerate(groups):
        g = g.iloc[:MAX_LEN]
        L = len(g)

        num = scaler.transform(g[SEQ_FEATURES_NUM].values.astype(np.float32))
        boo = g[SEQ_FEATURES_BOOL].values.astype(np.float32)

        prev_oh = np.zeros((L, n_prev), dtype=np.float32)
        for row, pc in enumerate(g[PREV_CAUSE_COL].fillna("NONE").values):
            if pc in prev_to_idx:
                prev_oh[row, prev_to_idx[pc]] = 1.0

        X[gi, :L, :] = np.concatenate([num, boo, prev_oh], axis=1)
        y[gi, :L] = [cause_to_idx[c] for c in g[TARGET_COL].values]
        mask[gi, :L] = True

    return X, y, mask, scaler


def split_trips(df, test_frac=0.2, seed=42):
    """Trip-level split, matching model.py's GroupShuffleSplit approach."""
    keys = df[["train_no", "trip_id"]].drop_duplicates().reset_index(drop=True)
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(keys))
    n_test = int(len(keys) * test_frac)
    test_keys = set(map(tuple, keys.iloc[idx[:n_test]].values))

    is_test = df.apply(lambda r: (r["train_no"], r["trip_id"]) in test_keys, axis=1)
    return df[~is_test].copy(), df[is_test].copy()


def multiclass_brier(y_true_idx, proba, n_classes):
    onehot = np.zeros_like(proba)
    onehot[np.arange(len(y_true_idx)), y_true_idx] = 1.0
    return float(np.mean(np.sum((proba - onehot) ** 2, axis=1)))


def main():
    import torch
    import torch.nn as nn

    torch.manual_seed(42)

    df, causes, cause_to_idx, prev_causes, prev_to_idx = load_and_prepare()
    train_df, test_df = split_trips(df)
    print(f"Trips: {df.groupby(['train_no','trip_id']).ngroups} total "
          f"({train_df.groupby(['train_no','trip_id']).ngroups} train / "
          f"{test_df.groupby(['train_no','trip_id']).ngroups} test)")

    Xtr, ytr, mtr, scaler = build_sequences(train_df, cause_to_idx, prev_to_idx, fit_scaler=True)
    Xte, yte, mte, _ = build_sequences(test_df, cause_to_idx, prev_to_idx, scaler=scaler)
    print(f"Sequence tensors: train {Xtr.shape}, test {Xte.shape}")

    n_classes = len(causes)
    n_feat = Xtr.shape[2]

    class DelayLSTM(nn.Module):
        def __init__(self):
            super().__init__()
            # bidirectional would leak the future into the past, which is
            # cheating for a forecasting task — deliberately unidirectional
            self.lstm = nn.LSTM(n_feat, HIDDEN_SIZE, num_layers=2,
                                batch_first=True, dropout=0.2)
            self.head = nn.Linear(HIDDEN_SIZE, n_classes)

        def forward(self, x):
            out, _ = self.lstm(x)
            return self.head(out)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")
    model = DelayLSTM().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
    loss_fn = nn.CrossEntropyLoss(ignore_index=PAD_LABEL)

    Xtr_t = torch.tensor(Xtr)
    ytr_t = torch.tensor(ytr)
    Xte_t = torch.tensor(Xte).to(device)

    n = len(Xtr_t)
    for epoch in range(1, EPOCHS + 1):
        model.train()
        perm = torch.randperm(n)
        total_loss, batches = 0.0, 0
        for i in range(0, n, BATCH_SIZE):
            b = perm[i:i + BATCH_SIZE]
            xb, yb = Xtr_t[b].to(device), ytr_t[b].to(device)
            opt.zero_grad()
            logits = model(xb)                       # (B, T, C)
            loss = loss_fn(logits.reshape(-1, n_classes), yb.reshape(-1))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total_loss += loss.item()
            batches += 1
        if epoch % 5 == 0 or epoch == 1:
            print(f"  epoch {epoch:3d}  train loss {total_loss / max(batches,1):.4f}")

    # ---- Evaluate, mirroring model.py's metrics exactly ----
    model.eval()
    with torch.no_grad():
        logits = model(Xte_t).cpu().numpy()

    flat_mask = mte.reshape(-1)
    proba = _softmax(logits.reshape(-1, n_classes))[flat_mask]
    y_true_idx = yte.reshape(-1)[flat_mask]

    train_counts = train_df[TARGET_COL].value_counts(normalize=True)
    baseline_row = np.array([train_counts.get(c, 0.0) for c in causes])
    baseline_row = baseline_row / baseline_row.sum()
    baseline_proba = np.tile(baseline_row, (len(y_true_idx), 1))

    ll_model = log_loss(y_true_idx, proba, labels=list(range(n_classes)))
    ll_base = log_loss(y_true_idx, baseline_proba, labels=list(range(n_classes)))
    br_model = multiclass_brier(y_true_idx, proba, n_classes)
    br_base = multiclass_brier(y_true_idx, baseline_proba, n_classes)
    pred_idx = proba.argmax(axis=1)
    acc = accuracy_score(y_true_idx, pred_idx)
    macro_f1 = f1_score(y_true_idx, pred_idx, average="macro", zero_division=0)

    print()
    print("[LSTM] PROBABILISTIC METRICS (compare against model.py's numbers):")
    print(f"  Log loss     = {ll_model:.4f}   (class-frequency baseline = {ll_base:.4f})"
          f"  -> {'BETTER' if ll_model < ll_base else 'WORSE'} than baseline")
    print(f"  Brier score  = {br_model:.4f}   (class-frequency baseline = {br_base:.4f})"
          f"  -> {'BETTER' if br_model < br_base else 'WORSE'} than baseline")
    print()
    print(f"[LSTM] Top-1 accuracy = {acc:.3f}   macro-F1 = {macro_f1:.3f}")
    print(classification_report(y_true_idx, pred_idx, target_names=causes, zero_division=0))

    print("For reference, model.py's RandomForest scored:")
    print("  log loss 1.3915, Brier 0.6893, accuracy 0.441, macro-F1 ~0.195")
    print("If the LSTM lands close to this, that is the expected information-ceiling")
    print("result discussed at the top of this file — not a bug.")

    torch.save({
        "state_dict": model.state_dict(),
        "causes": causes, "prev_causes": prev_causes,
        "scaler_mean": scaler.mean_, "scaler_scale": scaler.scale_,
        "n_feat": n_feat, "hidden_size": HIDDEN_SIZE,
    }, "output/lstm_model.pt")
    print("\nSaved -> output/lstm_model.pt")


def _softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


if __name__ == "__main__":
    main()