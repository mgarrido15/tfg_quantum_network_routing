#!/usr/bin/env python3
"""Benchmark comparison_algorithms.py with variability diagnostics.

This script performs three checks:
1) Count dropped simulator events per run.
2) Compare A/B with Q-CAST aggressive cleanup ON vs OFF.
3) Run 30 repetitions per mode and report mean/median/p90.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from statistics import mean, median, pstdev

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "comparison_algorithms.py"
RUNS = 30
ALG_NAMES = [
    "Dijkstra Clásico",
    "Dijkstra Distancia",
    "Dijkstra Capacidad Reserva",
    "Dijkstra Distancia Reserva",
    "Q-CAST",
]


def parse_throughputs(output: str) -> list[float]:
    matches = re.findall(r"^\s*-\s*Throughput:\s*([0-9.]+)", output, re.MULTILINE)
    if len(matches) < len(ALG_NAMES):
        raise ValueError(f"Could not parse all throughputs ({len(matches)} found)")
    return [float(value) for value in matches[-len(ALG_NAMES):]]


def count_dropped_events(output: str) -> int:
    return len(re.findall(r"dropped:\s*scheduled\s*for", output, re.IGNORECASE))


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def run_one(cleanup_enabled: bool) -> tuple[list[float], int, int, str]:
    # Run comparison script inside a Python wrapper so we can monkeypatch cleanup for A/B test.
    py = (
        "import os, runpy; "
        "from mqns.network.qcast.forwarder import QCastForwarder; "
        "orig = QCastForwarder._aggressive_cleanup; "
        "QCastForwarder._aggressive_cleanup = (orig if os.getenv('MQNS_QCAST_CLEANUP','1')=='1' else (lambda self: None)); "
        f"runpy.run_path(r'{SCRIPT}', run_name='__main__')"
    )

    env = dict(**{})
    env.update({"MQNS_QCAST_CLEANUP": "1" if cleanup_enabled else "0"})
    # Keep parent environment to avoid missing interpreter/site config.
    import os

    full_env = dict(os.environ)
    full_env.update(env)

    completed = subprocess.run(
        [sys.executable, "-c", py],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=full_env,
    )

    output = completed.stdout + completed.stderr
    throughputs = parse_throughputs(output)
    dropped = count_dropped_events(output)
    return throughputs, dropped, completed.returncode, output


def print_mode_summary(mode_name: str, values: dict[str, list[float]], dropped_counts: list[int]) -> None:
    print(f"\n=== Summary ({mode_name}) ===")
    for name in ALG_NAMES:
        series = values[name]
        avg = mean(series)
        med = median(series)
        p90 = percentile(series, 0.9)
        std = pstdev(series) if len(series) > 1 else 0.0
        print(
            f"{name}: mean={avg:.4f}, median={med:.4f}, p90={p90:.4f}, "
            f"std={std:.4f}, min={min(series):.4f}, max={max(series):.4f}"
        )

    print(
        f"Dropped events/run: mean={mean(dropped_counts):.2f}, "
        f"median={median(dropped_counts):.2f}, p90={percentile([float(x) for x in dropped_counts], 0.9):.2f}, "
        f"min={min(dropped_counts)}, max={max(dropped_counts)}"
    )


def main() -> int:
    if not SCRIPT.exists():
        print(f"Missing script: {SCRIPT}", file=sys.stderr)
        return 1

    modes = [("cleanup_on", True), ("cleanup_off", False)]
    all_mode_values: dict[str, dict[str, list[float]]] = {}
    all_mode_dropped: dict[str, list[int]] = {}

    for mode_name, cleanup_enabled in modes:
        print(f"\n===== MODE: {mode_name} =====")
        values: dict[str, list[float]] = {name: [] for name in ALG_NAMES}
        dropped_counts: list[int] = []

        for run_index in range(1, RUNS + 1):
            print(f"=== Run {run_index}/{RUNS} ({mode_name}) ===")
            throughputs, dropped, code, output = run_one(cleanup_enabled)
            if code != 0:
                print(output)
                print(f"Run failed in mode={mode_name}, run={run_index}", file=sys.stderr)
                return code

            dropped_counts.append(dropped)
            for name, throughput in zip(ALG_NAMES, throughputs, strict=True):
                values[name].append(throughput)
                print(f"{name}: {throughput:.4f} EPS")
            print(f"Dropped events: {dropped}")
            print()

        all_mode_values[mode_name] = values
        all_mode_dropped[mode_name] = dropped_counts
        print_mode_summary(mode_name, values, dropped_counts)

    print("\n===== A/B DELTA (cleanup_off - cleanup_on) =====")
    for name in ALG_NAMES:
        on_mean = mean(all_mode_values["cleanup_on"][name])
        off_mean = mean(all_mode_values["cleanup_off"][name])
        print(f"{name}: delta_mean={off_mean - on_mean:+.4f} EPS")

    on_drop = mean(all_mode_dropped["cleanup_on"])
    off_drop = mean(all_mode_dropped["cleanup_off"])
    print(f"Dropped events delta_mean={off_drop - on_drop:+.2f} events/run")

    qcast_on = mean(all_mode_values["cleanup_on"]["Q-CAST"])
    qcast_off = mean(all_mode_values["cleanup_off"]["Q-CAST"])
    best_other_on = max(mean(all_mode_values["cleanup_on"][name]) for name in ALG_NAMES if name != "Q-CAST")
    best_other_off = max(mean(all_mode_values["cleanup_off"][name]) for name in ALG_NAMES if name != "Q-CAST")

    print()
    print(f"Q-CAST wins on average (cleanup_on)? {'yes' if qcast_on > best_other_on else 'no'}")
    print(f"Q-CAST wins on average (cleanup_off)? {'yes' if qcast_off > best_other_off else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
