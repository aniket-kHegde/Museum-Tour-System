#!/usr/bin/env python3
"""
scripts/analyze_results.py

Post-hoc analysis of a benchmark run. Reads an existing results CSV produced by
scripts/benchmark.py (default: results/latency.csv) and derives the reported
KPIs. It does NOT run the pipeline or touch the broker — it only reads the file,
so results are reproducible after the fact.

It runs up to N=40 per-datapoint checks (each exhibit-narration sample is
validated against a latency target) and then prints the aggregate metrics.

Usage:
    .venv/bin/python scripts/analyze_results.py
    .venv/bin/python scripts/analyze_results.py --csv results/scale_2.csv --limit 40
    .venv/bin/python scripts/analyze_results.py --target-ms 3000

Only metrics that the CSV can actually support are emitted as numbers. Metrics
that this benchmark does not measure (BLE detection rate, speech-recognition
accuracy, RAG faithfulness, uptime) are reported as N/A with the reason, so the
output can't be mistaken for something it isn't.
"""

import argparse
import csv
import statistics
import sys


def load(csv_path):
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def measured(rows, interaction):
    """Non-warmup, non-timeout rows for one interaction (same filter the
    benchmark's summarise() uses)."""
    out = []
    for r in rows:
        if r.get("interaction") != interaction:
            continue
        if str(r.get("warmup")) == "True":
            continue
        if str(r.get("timed_out")) == "True":
            continue
        out.append(r)
    return out


def pct(values, q):
    s = sorted(values)
    if not s:
        return float("nan")
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="results/latency.csv", help="results CSV to analyze")
    ap.add_argument("--limit", type=int, default=40, help="max per-datapoint checks to run")
    ap.add_argument("--target-ms", type=float, default=3000.0,
                    help="latency target each narration sample is checked against (Rf-1)")
    args = ap.parse_args()

    try:
        rows = load(args.csv)
    except FileNotFoundError:
        print(f"ERROR: {args.csv} not found. Run scripts/benchmark.py first.", file=sys.stderr)
        sys.exit(1)

    print("=" * 70)
    print(f"  RESULTS ANALYSIS  —  {args.csv}")
    print(f"  total rows in file: {len(rows)}   check limit: {args.limit}")
    print("=" * 70)

    # ── Per-datapoint checks: exhibit narration (beacon) latency ──────────────
    narration = measured(rows, "beacon")
    sample = narration[: args.limit]
    print(f"\n[1] EXHIBIT-NARRATION LATENCY CHECKS  (target < {args.target_ms:.0f} ms)")
    print(f"    validating first {len(sample)} of {len(narration)} measured samples\n")

    passed = 0
    lat = []
    for i, r in enumerate(sample, 1):
        e2e = fnum(r.get("e2e_ms"))
        ok = e2e is not None and e2e < args.target_ms
        passed += ok
        if e2e is not None:
            lat.append(e2e)
        print(f"    check {i:>2}/{len(sample)}  device={r.get('device_id')}  "
              f"iter={r.get('iter')}  e2e={e2e:>7.2f} ms  "
              f"[{'PASS' if ok else 'FAIL'}]")

    print(f"\n    -> {passed}/{len(sample)} checks passed")

    # ── Aggregate metrics derivable from this file ────────────────────────────
    print("\n[2] AGGREGATE METRICS (derived from the CSV)\n")
    if lat:
        print(f"    Average exhibit-narration latency : {statistics.mean(lat):7.2f} ms "
              f"(n={len(lat)})")
        print(f"      median / p95 / max              : "
              f"{statistics.median(lat):.2f} / {pct(lat,95):.2f} / {max(lat):.2f} ms")

    # Request success rate over the same sample window (delivery, not BLE radio).
    sent = [r for r in rows
            if r.get("interaction") == "beacon" and str(r.get("warmup")) == "False"][: args.limit]
    ok_cnt = sum(1 for r in sent if str(r.get("timed_out")) != "True")
    if sent:
        print(f"    Beacon-event response success rate: "
              f"{100.0*ok_cnt/len(sent):6.2f} %  ({ok_cnt}/{len(sent)})")

    # Retrieval similarity for voice (this is cosine similarity, NOT faithfulness).
    voice = measured(rows, "voice")[: args.limit]
    scores = [fnum(r.get("top_score")) for r in voice if fnum(r.get("top_score")) is not None]
    rl = sum(1 for r in voice if str(r.get("rate_limited")) == "True")
    if scores:
        print(f"    Mean retrieval similarity (top_score): {statistics.mean(scores):6.4f} "
              f"(cosine — NOT a faithfulness score)")
    if voice:
        print(f"    Voice calls rate-limited (HTTP 429)  : {rl}/{len(voice)}")

    # ── Metrics this benchmark does NOT measure ───────────────────────────────
    print("\n[3] NOT MEASURED BY THIS FILE  (need a dedicated eval — reported N/A)\n")
    for name, why in [
        ("BLE detection rate",
         "benchmark injects beacon events over MQTT; no radio scan is exercised"),
        ("Speech-recognition accuracy",
         "voice path uses text transcripts (input=text); no STT runs, no WER"),
        ("RAG faithfulness score",
         "no grounding/LLM-judge eval; top_score is retrieval similarity only"),
        ("Uptime / availability",
         "single short burst; no long-window availability monitoring"),
    ]:
        print(f"    {name:<30} : N/A  — {why}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
