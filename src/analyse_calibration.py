"""
Is our uncertainty honest?

A P10-P90 range claims the real outcome will fall inside it about 80% of
the time. If it does so far less often, the range is too narrow and the
app is promising more precision than it has. That is a worse failure than
being imprecise, because the whole design argument of this project is
that we are honest about what we do not know. Connection risk makes it
load-bearing: those percentages are computed from this spread, so a
spread that is too tight understates every risk we report.

This reads the accuracy log and answers three questions:

  1. Do we have enough scored predictions to say anything at all?
  2. How often does reality actually land inside our range?
  3. If the range is wrong, is it wrong in one direction?

Question 3 matters most. A range can be too narrow at both ends, in which
case widen it evenly. But if the misses are mostly over-predictions — the
train recovered more time than we thought possible — the problem is not
the width, it is that the model cannot represent large recoveries, and
widening both ends would paper over a specific fixable flaw.

    python analyse_calibration.py
"""

import json
import os
import math

LOG_PATH = "output/prediction_accuracy_log.jsonl"
TARGET_COVERAGE = 0.80          # what P10-P90 promises
MIN_FOR_A_VERDICT = 15          # below this, differences are indistinguishable from luck


def load():
    if not os.path.exists(LOG_PATH):
        return []
    rows = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows


def binomial_tail(k, n, p):
    """P(at most k successes in n trials). Used to ask whether a low
    coverage could plausibly be bad luck rather than a real problem."""
    total = 0.0
    for i in range(k + 1):
        total += math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    return total


def main():
    rows = load()
    n = len(rows)
    print(f"Scored predictions: {n}")

    if n == 0:
        print("Nothing logged yet. Leave server.py running while trains are moving.")
        return

    inside = sum(1 for r in rows if r.get("within_p10_p90_band"))
    coverage = inside / n
    errors = [r["error_p50_min"] for r in rows]
    over = [e for e in errors if e < 0]     # predicted more delay than happened
    under = [e for e in errors if e > 0]    # predicted less delay than happened

    print(f"Inside the P10-P90 range: {inside} of {n}  ({coverage*100:.0f}%, we promise about {TARGET_COVERAGE*100:.0f}%)")
    print(f"Average error: {sum(abs(e) for e in errors)/n:.1f} min")
    print()
    print(f"Over-predicted delay (train did better than we said): {len(over)}"
          + (f", average {sum(over)/len(over):+.1f} min" if over else ""))
    print(f"Under-predicted delay (train did worse than we said): {len(under)}"
          + (f", average {sum(under)/len(under):+.1f} min" if under else ""))

    if n < MIN_FOR_A_VERDICT:
        p_luck = binomial_tail(inside, n, TARGET_COVERAGE)
        print()
        print(f"Too few to act on. With {n} predictions, seeing {inside} inside the range")
        print(f"would happen by chance about {p_luck*100:.0f}% of the time even if the")
        print(f"ranges were perfectly calibrated. Collect at least {MIN_FOR_A_VERDICT}")
        print("before changing anything.")
        return

    print()
    if coverage >= 0.70:
        print("Calibration looks reasonable. Leave the spread alone.")
        return

    # What single factor, applied to EVERY range, brings coverage to the
    # promised level?
    #
    # An earlier version of this answered a different and wrong question: it
    # took the ratio of error to half-width over the MISSED rows only. That
    # excludes the rows we got right and lets a single narrow range produce a
    # huge ratio, so it badly overstated the correction — it suggested 4.9x on
    # a sample where the honest answer was closer to 2x. Widening fivefold
    # would have made every range too vague to be useful and flattened the
    # connection-risk percentages into noise.
    #
    # The right calculation uses all rows: for each one, how many half-widths
    # away did the outcome land? The 80th percentile of that ratio is the
    # factor at which 80% of outcomes fall inside. This is the same idea as
    # conformal prediction — set the interval width from observed errors
    # rather than from the model's own optimism.
    ratios = []
    for r in rows:
        half = max((r["predicted_delay_p90"] - r["predicted_delay_p10"]) / 2, 0.1)
        ratios.append(abs(r["error_p50_min"]) / half)
    ratios.sort()
    idx = min(int(TARGET_COVERAGE * len(ratios)), len(ratios) - 1)
    factor = max(1.0, round(ratios[idx], 2))

    median_half = sorted((r["predicted_delay_p90"] - r["predicted_delay_p10"]) / 2 for r in rows)[len(rows)//2]
    print(f"The ranges are too narrow. Scaling every spread by {factor}x would bring")
    print(f"coverage to about {TARGET_COVERAGE*100:.0f}%.")
    print(f"A typical range is currently plus or minus {median_half:.0f} min; that becomes "
          f"plus or minus {median_half*factor:.0f} min.")
    print()
    print(f"To apply it, set SPREAD_SCALE = {factor} in forecast_engine.py and restart.")
    print()

    if len(over) > 2 * max(len(under), 1):
        print("But note the misses are mostly OVER-predictions: the trains recovered")
        print("more time than the model believes possible. generator.py draws RECOVERY")
        print("from gamma(1.2, 3.5), a mean of about 4 minutes. Real services on padded")
        print("sections recover far more than that. Raising the recovery magnitude is a")
        print("better fix than widening both ends, which would only mask the flaw.")
    elif len(under) > 2 * max(len(over), 1):
        print("The misses are mostly UNDER-predictions: delays grew more than expected.")
        print("Raising the hazard magnitudes in generator.py fits the evidence better")
        print("than widening the range symmetrically.")
    else:
        print("Misses fall on both sides, so the spread itself is genuinely too tight.")
        print("Widening it evenly is the right correction.")


if __name__ == "__main__":
    main()