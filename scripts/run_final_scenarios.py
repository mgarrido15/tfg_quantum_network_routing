import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
SCENARIOS = {
    "basic": ROOT_DIR / "escenario_basico.json",
    "large": ROOT_DIR / "escenario_grande_final.json",
    "ideal": ROOT_DIR / "escenario_casi_ideal_final.json",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Genera las comparativas y graficas de los tres escenarios finales"
    )
    parser.add_argument(
        "--scenario",
        choices=("all", *SCENARIOS),
        default="all",
    )
    parser.add_argument("--sim-time", type=float, default=1000.0)
    parser.add_argument("--output-root")
    parser.add_argument("--simulation-seed", type=int, default=10007)
    parser.add_argument("--priority-seed", type=int, default=20011)
    parser.add_argument("--log-level", default="ERROR")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.sim_time <= 0 or args.sim_time % 4 != 0:
        raise ValueError("--sim-time must be a positive multiple of four seconds")

    output_root = Path(args.output_root) if args.output_root else (
        ROOT_DIR
        / "outputs"
        / f"final_scenarios_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    selected = SCENARIOS if args.scenario == "all" else {
        args.scenario: SCENARIOS[args.scenario]
    }

    for name, scenario_path in selected.items():
        if not scenario_path.is_file():
            raise FileNotFoundError(
                f"Missing final scenario {scenario_path}. "
                "Run scripts/build_final_scenarios.py first."
            )
        output_dir = output_root / name
        command = [
            sys.executable,
            str(ROOT_DIR / "scripts" / "comparison_algorithms_badlinks_original.py"),
            "--scenario",
            str(scenario_path),
            "--scenario-mode",
            "as-is",
            "--sim-time",
            str(args.sim_time),
            "--simulation-seed",
            str(args.simulation_seed),
            "--priority-seed",
            str(args.priority_seed),
            "--log-level",
            args.log_level,
            "--output-dir",
            str(output_dir),
        ]
        print(f"\n=== {name}: {scenario_path.name} ===", flush=True)
        subprocess.run(command, cwd=ROOT_DIR, check=True)

    print(f"\nResultados finales: {output_root}")


if __name__ == "__main__":
    main()
