"""Simulation utilities for saving graphs and results."""

import subprocess
import sys
import hashlib
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
from collections.abc import Sequence
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx


def student_t_critical_95(samples: int) -> float:
    if samples < 2:
        return 0.0
    degrees_of_freedom = samples - 1
    exact_small_sample = {
        1: 12.706204736,
        2: 4.30265273,
        3: 3.182446305,
        4: 2.776445105,
        5: 2.570581836,
    }
    if degrees_of_freedom in exact_small_sample:
        return exact_small_sample[degrees_of_freedom]

    z = 1.959963985
    inverse_df = 1.0 / degrees_of_freedom
    return (
        z
        + (z**3 + z) * inverse_df / 4
        + (5 * z**5 + 16 * z**3 + 3 * z) * inverse_df**2 / 96
        + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) * inverse_df**3 / 384
    )


def mean_ci95(
    values: Sequence[float],
    *,
    lower_bound: float | None = None,
    upper_bound: float | None = None,
) -> dict[str, float | int | str | None]:
    if not values:
        raise ValueError("Cannot compute a confidence interval without samples")
    sample_mean = statistics.fmean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    critical_value = student_t_critical_95(len(values))
    half_width = (
        critical_value * std / math.sqrt(len(values))
        if len(values) > 1
        else 0.0
    )
    low = sample_mean - half_width
    high = sample_mean + half_width
    if lower_bound is not None:
        low = max(lower_bound, low)
    if upper_bound is not None:
        high = min(upper_bound, high)
    return {
        "mean": sample_mean,
        "median": statistics.median(values),
        "std": std,
        "ci95_half_width": half_width,
        "ci95_low": low,
        "ci95_high": high,
        "samples": len(values),
        "interval_method": "student_t",
    }


def optional_mean_ci95(
    values: Sequence[float | None],
    *,
    lower_bound: float | None = None,
    upper_bound: float | None = None,
) -> dict[str, float | int | str | None]:
    observed = [value for value in values if value is not None]
    if not observed:
        return {
            "mean": None,
            "median": None,
            "std": None,
            "ci95_half_width": None,
            "ci95_low": None,
            "ci95_high": None,
            "samples": 0,
            "interval_method": "student_t",
        }
    return mean_ci95(
        observed,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
    )


def create_simulation_folder() -> str:
    """Create output folder for simulation"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = Path(__file__).parent.parent / "outputs" / timestamp
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder)

def save_graph(fig, sim_folder, name) -> str:
    """Save matplotlib figure to PNG file"""
    if sim_folder is None:
        raise ValueError("sim_folder cannot be None")
    if fig is None:
        raise ValueError("fig cannot be None")
    
    filepath = Path(sim_folder) / f"{name}.png"
    try:
        fig.savefig(str(filepath), dpi=150, bbox_inches='tight')
        print(f"  Grafica guardada: {filepath}")
    finally:
        plt.close(fig)
    return str(filepath)

def _channel_endpoints(channel: Any) -> tuple[Any, Any]:
    nodes = getattr(channel, "node_list", None)
    if nodes is None or len(nodes) != 2:
        raise ValueError(f"Channel {getattr(channel, 'name', channel)!r} has no two endpoints")
    return nodes[0], nodes[1]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_topology_diagram(net: Any, sim_folder: str, name: str) -> str:
    """Save a deterministic diagram of the physical topology."""
    graph = nx.Graph()
    for node in net.nodes:
        graph.add_node(node.name, capacity=node.memory.capacity)

    edge_channels: dict[tuple[str, str], list[Any]] = {}
    for channel in net.qchannels:
        left, right = _channel_endpoints(channel)
        edge = tuple(sorted((left.name, right.name)))
        edge_channels.setdefault(edge, []).append(channel)
        graph.add_edge(*edge)

    positions = nx.spring_layout(graph, seed=42)
    figure, axis = plt.subplots(figsize=(12, 8))
    node_labels = {
        node.name: f"{node.name}\nW={node.memory.capacity}"
        for node in net.nodes
    }
    edge_labels = {
        edge: (
            f"{len(channels)} canal(es)\n"
            f"L={sum(float(channel.length) for channel in channels) / len(channels):.3g} km"
        )
        for edge, channels in edge_channels.items()
    }
    nx.draw_networkx(
        graph,
        positions,
        labels=node_labels,
        node_color="lightblue",
        edgecolors="black",
        node_size=2200,
        ax=axis,
    )
    nx.draw_networkx_edge_labels(graph, positions, edge_labels=edge_labels, ax=axis)
    axis.set_title("Topologia fisica de la simulacion")
    axis.axis("off")
    figure.tight_layout()
    return save_graph(figure, sim_folder, name)


def save_link_metadata(net: Any, sim_folder: str, name: str) -> str:
    """Save node and physical-channel parameters used by a simulation."""
    if sim_folder is None:
        raise ValueError("sim_folder cannot be None")

    pair_counts: dict[tuple[str, str], int] = {}
    for channel in net.qchannels:
        left, right = _channel_endpoints(channel)
        edge = tuple(sorted((left.name, right.name)))
        pair_counts[edge] = pair_counts.get(edge, 0) + 1

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "nodes": [
            {
                "id": node.name,
                "capacity": node.memory.capacity,
                "t_cohere": node.memory.t_decohere.sec,
                "node_fidelity": getattr(node, "node_fidelity", None),
            }
            for node in net.nodes
        ],
        "links": [],
    }
    for channel in net.qchannels:
        left, right = _channel_endpoints(channel)
        edge = tuple(sorted((left.name, right.name)))
        payload["links"].append(
            {
                "name": channel.name,
                "u": left.name,
                "v": right.name,
                "length_km": float(channel.length),
                "alpha_db_per_km": float(channel.alpha),
                "success_probability": float(channel.link_arch.success_prob),
                "configured_fidelity": getattr(channel, "_fidelity", None),
                "transfer_survival_probability": float(channel.transfer_error.p_survival),
                "parallel_channels": pair_counts[edge],
            }
        )

    return save_json_results(sim_folder, name, payload)


def print_simulation_summary(sim_folder: str) -> dict[str, Any]:
    """Save and print a manifest describing the generated artifacts and environment."""
    folder = Path(sim_folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"Simulation folder does not exist: {folder}")

    repository = Path(__file__).resolve().parent.parent
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty_files = subprocess.run(
        ["git", "status", "--short"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    artifacts = [
        {
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in sorted(folder.iterdir())
        if path.is_file() and path.name != "simulation_summary.json"
    ]
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "git_revision": revision,
        "git_dirty": bool(dirty_files),
        "git_changed_files": dirty_files,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": sys.platform,
        "command": [sys.executable, *sys.argv],
        "working_directory": str(Path.cwd()),
        "artifacts": artifacts,
    }
    save_json_results(str(folder), "simulation_summary", summary)
    print(
        f"  Resumen: {len(artifacts)} artefactos, revision {revision[:12]}, "
        f"arbol {'modificado' if dirty_files else 'limpio'}"
    )
    return summary


def save_json_results(sim_folder: str, filename: str, data: Any) -> str:
    """Save results to JSON file"""
    if sim_folder is None:
        raise ValueError("sim_folder cannot be None")
    if data is None:
        raise ValueError("data cannot be None")
    
    filepath = Path(sim_folder) / f"{filename}.json"
    with filepath.open('w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    print(f"  Datos guardados: {filepath}")
    return str(filepath)
