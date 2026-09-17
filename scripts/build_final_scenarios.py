import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCE_SCENARIO = ROOT_DIR / "escenario_grande_multicanal_w.json"
OUTPUTS = {
    "large": ROOT_DIR / "escenario_grande_final.json",
    "ideal": ROOT_DIR / "escenario_casi_ideal_final.json",
}
PROBABILITY_RANGES = {
    "large": (0.45, 0.55),
    "ideal": (0.95, 0.99),
}
CHANNELS_PER_LINK = 5
ALPHA_DB_PER_KM = 0.2
ETA_S = 1.0
ETA_D = 1.0
DEPOLAR_RATE_PER_KM = 0.01


def physical_length_for_probability(probability: float) -> float:
    return -10.0 * math.log10(
        probability / (ETA_S * ETA_D**2)
    ) / ALPHA_DB_PER_KM


def link_fidelity(length: float) -> float:
    werner_parameter = math.exp(-DEPOLAR_RATE_PER_KM * length)
    return (3.0 * werner_parameter + 1.0) / 4.0


def logical_topology(source: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in source["enlaces"]:
        key = tuple(sorted((str(edge["u"]), str(edge["v"]))))
        grouped.setdefault(key, []).append(edge)

    logical_edges = []
    for (left, right), entries in sorted(grouped.items()):
        logical_edges.append(
            {
                "u": left,
                "v": right,
                "reference_length": sum(
                    float(edge.get("length", 1.0)) for edge in entries
                ) / len(entries),
            }
        )
    return logical_edges


def build_scenario(
    source: dict[str, Any],
    probability_min: float,
    probability_max: float,
) -> dict[str, Any]:
    logical_edges = logical_topology(source)
    reference_lengths = [edge["reference_length"] for edge in logical_edges]
    min_length = min(reference_lengths)
    max_length = max(reference_lengths)
    span = max_length - min_length

    edges = []
    degrees: Counter[str] = Counter()
    for edge in logical_edges:
        normalized = (
            (edge["reference_length"] - min_length) / span
            if span > 0
            else 0.5
        )
        probability = probability_max - normalized * (
            probability_max - probability_min
        )
        length = physical_length_for_probability(probability)
        edges.append(
            {
                "u": edge["u"],
                "v": edge["v"],
                "length": round(length, 9),
                "alpha": ALPHA_DB_PER_KM,
                "eta_s": ETA_S,
                "eta_d": ETA_D,
                "channels": CHANNELS_PER_LINK,
                "fidelity": round(link_fidelity(length), 6),
                "transfer_error": f"DEPOLAR:{DEPOLAR_RATE_PER_KM}",
            }
        )
        degrees[edge["u"]] += 1
        degrees[edge["v"]] += 1

    nodes = []
    for node in source["nodos"]:
        node_copy = dict(node)
        node_copy["capacity"] = max(
            int(node_copy.get("capacity", 0)),
            degrees[str(node_copy["id"])] * CHANNELS_PER_LINK,
        )
        nodes.append(node_copy)

    return {
        "nodos": nodes,
        "enlaces": edges,
        "solicitudes": source["solicitudes"],
    }


def main() -> None:
    source = json.loads(SOURCE_SCENARIO.read_text(encoding="utf-8"))
    for name, destination in OUTPUTS.items():
        scenario = build_scenario(source, *PROBABILITY_RANGES[name])
        destination.write_text(
            json.dumps(scenario, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"{name}: {destination}")


if __name__ == "__main__":
    main()
