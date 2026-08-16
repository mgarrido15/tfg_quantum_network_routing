import json
import os
import sys
import argparse
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import matplotlib.pyplot as plt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mqns.entity.memory.memory import QuantumMemory
from mqns.network.fw.routing import RoutingPathStatic
from mqns.network.network.network import QuantumNetwork
from mqns.network.network.reporting import (
    build_request_id,
    construir_resultados_qcast,
    obtener_prob_y_fidelidad_de_ruta,
)
from mqns.network.network.timing import TimingModeSyncQCast
from mqns.network.protocol.link_layer import LinkLayer, LinkLayerCounters
from mqns.network.qcast.controller import QCastController, QCastMultiEntController
from mqns.network.qcast.forwarder import QCastForwarder, QCastMultiEntForwarder
from mqns.network.route import (
    DijkstraDistanceRouteAlgorithm,
    DijkstraRouteAlgorithm,
    assign_dijkstra_routes_with_capacity,
    assign_dijkstra_routes_with_capacity_reserve_all,
)
from mqns.network.topology.randomtopo import RandomTopology
from mqns.simulator import Simulator
from mqns.utils import log, rng
try:
    from simulation_utils import create_simulation_folder, print_simulation_summary, save_graph
except ModuleNotFoundError:
    from scripts.simulation_utils import create_simulation_folder, print_simulation_summary, save_graph


SIM_TIME = 1000.0
SIM_ACCURACY = 1_000_000
T_PHASE = 1.0
TOTAL_CYCLE_TIME = T_PHASE * 4.0
T_COHERE = 10.0
PAIR_RATIO = 0.1
LOG_LEVEL = "WARN"


@dataclass(frozen=True)
class AlgorithmSpec:
    name: str
    controller_class: type
    route_alg: Any | None
    use_capacity: bool
    reserve_all_capacity: bool
    forwarder_class: type | None = None


class StaticQCastForwarder(QCastForwarder):
    def _send_initial_queries(self):
        # Prevent automatic Q-CAST request generation in static-route experiments.
        return


def install_stack(node, controller=None, qcast_queries=True, forwarder_class=None):
    if not hasattr(node, "memory"):
        node.memory = QuantumMemory(name=f"mem_{node.name}", capacity=100, t_cohere=T_COHERE)

    mem = node.memory
    if hasattr(mem, "_t_cohere"):
        mem._t_cohere = T_COHERE

    link_layer = LinkLayer()
    if forwarder_class is not None:
        forwarder = forwarder_class(k_max=2, ps=1.0, purif_enabled=False, swapping_enabled=True)
    elif qcast_queries:
        forwarder = QCastForwarder(k_max=2, ps=1.0, purif_enabled=False, swapping_enabled=True)
    else:
        forwarder = StaticQCastForwarder(k_max=2, ps=1.0, purif_enabled=False, swapping_enabled=True)

    if controller is not None:
        forwarder.controller = controller

    node.add_apps([link_layer, forwarder])
    setattr(node, "forwarder", forwarder)
    return forwarder


def attach_controller(net: QuantumNetwork, ctrl):
    setattr(net, "controller", ctrl)
    setattr(ctrl, "net", net)
    if net.nodes:
        net.nodes[0].add_apps(ctrl)


def build_requests(net: QuantumNetwork, topo_config: dict[str, Any]) -> list[dict[str, Any]]:
    solicitudes = []
    idx = 0
    for req in topo_config.get("solicitudes", []):
        src = net.get_node(req["src"])
        dst = net.get_node(req["dst"])
        req_id = build_request_id(src.name, dst.name, idx)
        net.add_request(src, dst, {"req_id": req_id})
        solicitudes.append({"req_id": req_id, "src": src, "dst": dst})
        idx += 1
    return solicitudes


def install_static_route_on_forwarders(net: QuantumNetwork, ctrl, route: list[str], req_id: str):
    path_id = ctrl.next_path_id
    ctrl.next_path_id += 1
    route_req_numeric = path_id

    width = int(ctrl.request_route_info.get(req_id, {}).get("width", 1))
    ctrl.path_w[path_id] = width
    ctrl.path_requests[path_id] = [req_id]

    if width > 1:
        m_v = [(width, width) for _ in range(max(0, len(route) - 1))]
        route_path = RoutingPathStatic(route, req_id=route_req_numeric, path_id=path_id, m_v=m_v)
    else:
        route_path = RoutingPathStatic(route, req_id=route_req_numeric, path_id=path_id)

    instructions = next(route_path.compute_paths(net))
    install_msg = {"cmd": "INSTALL_PATH", "path_id": path_id, "instructions": instructions}
    for node_name in route:
        qnode = net.get_node(node_name)
        forwarder = getattr(qnode, "forwarder", None)
        if forwarder is not None and hasattr(forwarder, "handle_classic_packet"):
            forwarder.handle_classic_packet(qnode, install_msg)


def run_single_simulation(
    scenario_path: str,
    controller_class,
    route_alg=None,
    use_capacity=True,
    reserve_all_capacity=False,
    forwarder_class=None,
) -> dict[str, Any]:
    with open(scenario_path, "r", encoding="utf-8") as f:
        topo_config = json.load(f)

    sim = Simulator(0, SIM_TIME, accuracy=SIM_ACCURACY)
    net = QuantumNetwork(None)
    net.build_topology_from_json(scenario_path)
    net.all_nodes = list(net.nodes)
    net.requests.clear()

    if route_alg is not None:
        net.route = route_alg
        net.build_route()

    ctrl = controller_class(k_max=5)
    attach_controller(net, ctrl)
    net.simulator = sim

    solicitudes = build_requests(net, topo_config)

    qcast_queries = route_alg is None
    for node in net.nodes:
        install_stack(node, controller=ctrl, qcast_queries=qcast_queries, forwarder_class=forwarder_class)
        node.install(sim)

    net.timing = TimingModeSyncQCast(t1=T_PHASE, t2=T_PHASE, t3=T_PHASE, t4=T_PHASE)
    net.timing.install(net)

    if route_alg is not None:
        if reserve_all_capacity:
            assign_dijkstra_routes_with_capacity_reserve_all(
                net,
                ctrl,
                solicitudes,
                obtener_prob_y_fidelidad_de_ruta,
                enforce_capacity=use_capacity,
            )
        else:
            assign_dijkstra_routes_with_capacity(
                net,
                ctrl,
                solicitudes,
                obtener_prob_y_fidelidad_de_ruta,
                enforce_capacity=use_capacity,
            )

        for req in solicitudes:
            req_id = req["req_id"]
            info = ctrl.request_route_info.get(req_id)
            if not info:
                continue
            route = info.get("route")
            if not route:
                continue
            install_static_route_on_forwarders(net, ctrl, route, req_id)

    ciclos_totales = int(SIM_TIME / TOTAL_CYCLE_TIME)
    sim.run()

    resultados = construir_resultados_qcast(ctrl, solicitudes, ciclos_totales)
    counters = LinkLayerCounters.aggregate(net.nodes)

    total_exitos = sum(r.get("successes", 0) for r in resultados)
    throughput = total_exitos / SIM_TIME

    return {
        "throughput": throughput,
        "total_successes": total_exitos,
        "n_etg": counters.n_etg,
        "n_attempts": counters.n_attempts,
        "n_requests": len(solicitudes),
    }


def edges_from_random_topology(n_nodes: int, line_factor: float = 1.8) -> list[tuple[str, str]]:
    lines_number = max(n_nodes - 1, int(round(n_nodes * line_factor)))
    topo = RandomTopology(nodes_number=n_nodes, lines_number=lines_number)
    nodes, qchannels = topo.build()

    node_by_obj = {id(node): node.name for node in nodes}
    edges = set()

    for ch in qchannels:
        if hasattr(ch, "node_list") and len(ch.node_list) == 2:
            a = node_by_obj.get(id(ch.node_list[0]), ch.node_list[0].name)
            b = node_by_obj.get(id(ch.node_list[1]), ch.node_list[1].name)
            if a != b:
                edges.add(tuple(sorted((a, b))))

    return sorted(edges)


def build_base_topology_config(
    n_nodes: int,
    *,
    line_factor: float,
    max_parallel_channels: int,
    min_length: float,
    max_length: float,
) -> dict[str, Any]:
    node_names = [f"n{i + 1}" for i in range(n_nodes)]
    base_edges = edges_from_random_topology(n_nodes=n_nodes, line_factor=line_factor)

    enlaces = []
    required_capacity = {name: 0 for name in node_names}

    for u, v in base_edges:
        channels = int(rng.integers(1, max_parallel_channels + 1))
        length = float(rng.uniform(min_length, max_length))
        enlaces.append({"u": u, "v": v, "length": length, "channels": channels})
        required_capacity[u] += channels
        required_capacity[v] += channels

    nodos = []
    for name in node_names:
        extra_capacity = int(rng.integers(1, 5))
        nodos.append({"id": name, "capacity": required_capacity[name] + extra_capacity})

    return {"nodos": nodos, "enlaces": enlaces, "solicitudes": []}


def list_non_adjacent_pairs(base_config: dict[str, Any]) -> list[tuple[str, str]]:
    names = [n["id"] for n in base_config["nodos"]]
    adjacent = set()
    for e in base_config["enlaces"]:
        a, b = sorted((e["u"], e["v"]))
        adjacent.add((a, b))

    candidates = []
    for a, b in combinations(names, 2):
        key = tuple(sorted((a, b)))
        if key not in adjacent:
            candidates.append((a, b))
    return candidates


def sample_sd_pairs(non_adjacent_pairs: list[tuple[str, str]], n_pairs: int) -> list[dict[str, str]]:
    if n_pairs > len(non_adjacent_pairs):
        raise ValueError(
            f"Requested {n_pairs} S-D pairs, but only {len(non_adjacent_pairs)} non-adjacent pairs are available"
        )

    idxs = rng.choice(len(non_adjacent_pairs), size=n_pairs, replace=False)
    solicitudes = []
    for idx in idxs:
        src, dst = non_adjacent_pairs[int(idx)]
        if bool(rng.integers(0, 2)):
            src, dst = dst, src
        solicitudes.append({"src": src, "dst": dst})
    return solicitudes


def write_scenario(path: str, config: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def plot_throughput_vs_network_size(output_dir: str, sizes: list[int], results: dict[str, list[float]]):
    fig = plt.figure(figsize=(8.4, 4.8))
    markers = ["o", "s", "^", "d", "x", "P", "*"]
    linestyles = ["-", "--", "-.", ":", "-", "--", "-."]

    for i, (alg_name, y_values) in enumerate(results.items()):
        plt.plot(
            sizes,
            y_values,
            marker=markers[i % len(markers)],
            linestyle=linestyles[i % len(linestyles)],
            linewidth=1.8,
            markersize=5,
            label=alg_name,
        )

    plt.xlabel("|V| (nodos)")
    plt.ylabel("Throughput (eps)")
    plt.title("Throughput vs tamaño de la red")
    plt.grid(True, linestyle="--", alpha=0.35)
    plt.legend(loc="best", fontsize=8)
    plt.tight_layout()
    save_graph(fig, output_dir, "01_throughput_vs_network_size")


def plot_throughput_vs_sd_pairs(output_dir: str, pair_counts: list[int], results: dict[str, list[float]]):
    fig = plt.figure(figsize=(8.4, 4.8))
    markers = ["o", "s", "^", "d", "x", "P", "*"]
    linestyles = ["-", "--", "-.", ":", "-", "--", "-."]

    for i, (alg_name, y_values) in enumerate(results.items()):
        plt.plot(
            pair_counts,
            y_values,
            marker=markers[i % len(markers)],
            linestyle=linestyles[i % len(linestyles)],
            linewidth=1.8,
            markersize=5,
            label=alg_name,
        )

    plt.xlabel("#Parejas S-D")
    plt.ylabel("Throughput (eps)")
    plt.title("Throughput vs numero de parejas S-D (|V| = 100)")
    plt.grid(True, linestyle="--", alpha=0.35)
    plt.legend(loc="best", fontsize=8)
    plt.tight_layout()
    save_graph(fig, output_dir, "02_throughput_vs_sd_pairs_100_nodes")


def resolve_results_path(results_path: str) -> str:
    if os.path.isabs(results_path) and os.path.exists(results_path):
        return results_path

    cwd_candidate = os.path.abspath(results_path)
    if os.path.exists(cwd_candidate):
        return cwd_candidate

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    root_candidate = os.path.join(repo_root, results_path)
    if os.path.exists(root_candidate):
        return root_candidate

    basename = os.path.basename(results_path)
    outputs_dir = os.path.join(repo_root, "outputs")
    if os.path.isdir(outputs_dir):
        matches = []
        for walk_root, _dirs, files in os.walk(outputs_dir):
            if basename in files:
                matches.append(os.path.join(walk_root, basename))

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise FileNotFoundError(
                f"Found multiple files named '{basename}' under outputs/. Use a specific path. Matches: {matches}"
            )

    raise FileNotFoundError(
        f"Results file not found: {results_path}. Try a full path or a path relative to repo root."
    )


def plot_from_results_file(results_path: str, output_dir: str | None = None) -> str:
    resolved_results_path = resolve_results_path(results_path)

    with open(resolved_results_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    size_section = payload.get("throughput_vs_network_size", {})
    pair_section = payload.get("throughput_vs_sd_pairs_on_100", {})

    network_sizes = size_section.get("x")
    size_series = size_section.get("series")
    pair_counts = pair_section.get("x")
    pair_series = pair_section.get("series")

    if not isinstance(network_sizes, list) or not isinstance(size_series, dict):
        raise ValueError("Invalid results file: missing throughput_vs_network_size.x or .series")
    if not isinstance(pair_counts, list) or not isinstance(pair_series, dict):
        raise ValueError("Invalid results file: missing throughput_vs_sd_pairs_on_100.x or .series")

    target_output_dir = output_dir or os.path.dirname(os.path.abspath(resolved_results_path))
    os.makedirs(target_output_dir, exist_ok=True)

    plot_throughput_vs_network_size(target_output_dir, network_sizes, size_series)
    plot_throughput_vs_sd_pairs(target_output_dir, pair_counts, pair_series)
    print(f"Using results file: {resolved_results_path}")
    return target_output_dir


def run_campaigns(seed: int = 7):
    log.set_default_level(LOG_LEVEL)
    rng.reseed(seed)

    algorithms = [
        AlgorithmSpec("Dijkstra Clasico", QCastController, DijkstraRouteAlgorithm(), True, False),
        AlgorithmSpec("Dijkstra Distancia", QCastController, DijkstraDistanceRouteAlgorithm(), True, False),
        AlgorithmSpec("Dijkstra Capacidad Reserva", QCastController, DijkstraRouteAlgorithm(), True, True),
        AlgorithmSpec("Dijkstra Distancia Reserva", QCastController, DijkstraDistanceRouteAlgorithm(), True, True),
        AlgorithmSpec("Q-CAST", QCastController, None, False, False),
        AlgorithmSpec("Q-CAST MultiEnt", QCastMultiEntController, None, False, False, QCastMultiEntForwarder),
    ]

    output_dir = create_simulation_folder()
    scenario_dir = os.path.join(output_dir, "scenarios")
    os.makedirs(scenario_dir, exist_ok=True)

    network_sizes = [25, 50, 75, 100]
    size_experiment_results: dict[str, list[float]] = {alg.name: [] for alg in algorithms}
    size_experiment_details = []

    print("Running throughput vs network size experiment...")
    for n_nodes in network_sizes:
        proportional_pairs = max(2, int(round(n_nodes * PAIR_RATIO)))
        base_config = build_base_topology_config(
            n_nodes=n_nodes,
            line_factor=1.8,
            max_parallel_channels=3,
            min_length=1.0,
            max_length=10.0,
        )
        non_adj_pairs = list_non_adjacent_pairs(base_config)
        solicitudes = sample_sd_pairs(non_adj_pairs, proportional_pairs)

        scenario_config = dict(base_config)
        scenario_config["solicitudes"] = solicitudes

        scenario_path = os.path.join(scenario_dir, f"size_{n_nodes}.json")
        write_scenario(scenario_path, scenario_config)

        run_info = {
            "network_size": n_nodes,
            "sd_pairs": proportional_pairs,
            "scenario": os.path.relpath(scenario_path, output_dir),
            "throughput": {},
        }

        print(f"  |V|={n_nodes}, S-D pairs={proportional_pairs}")
        for alg in algorithms:
            metrics = run_single_simulation(
                scenario_path=scenario_path,
                controller_class=alg.controller_class,
                route_alg=alg.route_alg,
                use_capacity=alg.use_capacity,
                reserve_all_capacity=alg.reserve_all_capacity,
                forwarder_class=alg.forwarder_class,
            )
            size_experiment_results[alg.name].append(metrics["throughput"])
            run_info["throughput"][alg.name] = metrics
            print(f"    {alg.name:<28} throughput={metrics['throughput']:.4f} eps")

        size_experiment_details.append(run_info)

    plot_throughput_vs_network_size(output_dir, network_sizes, size_experiment_results)

    print("Running throughput vs S-D pairs experiment on |V|=100...")
    base_100 = build_base_topology_config(
        n_nodes=100,
        line_factor=1.8,
        max_parallel_channels=3,
        min_length=1.0,
        max_length=10.0,
    )
    non_adj_100 = list_non_adjacent_pairs(base_100)

    shuffled_candidates = list(non_adj_100)
    rng.shuffle(shuffled_candidates)

    pair_counts = [2, 4, 6, 8, 10]
    max_pairs = max(pair_counts)
    if max_pairs > len(shuffled_candidates):
        raise ValueError(f"Not enough non-adjacent candidate pairs for |V|=100. Available: {len(shuffled_candidates)}")

    pair_experiment_results: dict[str, list[float]] = {alg.name: [] for alg in algorithms}
    pair_experiment_details = []

    for n_pairs in pair_counts:
        solicitudes = []
        for src, dst in shuffled_candidates[:n_pairs]:
            if bool(rng.integers(0, 2)):
                src, dst = dst, src
            solicitudes.append({"src": src, "dst": dst})

        scenario_config = dict(base_100)
        scenario_config["solicitudes"] = solicitudes

        scenario_path = os.path.join(scenario_dir, f"pairs_100_nodes_{n_pairs}.json")
        write_scenario(scenario_path, scenario_config)

        run_info = {
            "network_size": 100,
            "sd_pairs": n_pairs,
            "scenario": os.path.relpath(scenario_path, output_dir),
            "throughput": {},
        }

        print(f"  |V|=100, S-D pairs={n_pairs}")
        for alg in algorithms:
            metrics = run_single_simulation(
                scenario_path=scenario_path,
                controller_class=alg.controller_class,
                route_alg=alg.route_alg,
                use_capacity=alg.use_capacity,
                reserve_all_capacity=alg.reserve_all_capacity,
                forwarder_class=alg.forwarder_class,
            )
            pair_experiment_results[alg.name].append(metrics["throughput"])
            run_info["throughput"][alg.name] = metrics
            print(f"    {alg.name:<28} throughput={metrics['throughput']:.4f} eps")

        pair_experiment_details.append(run_info)

    plot_throughput_vs_sd_pairs(output_dir, pair_counts, pair_experiment_results)

    results_payload = {
        "config": {
            "seed": seed,
            "sim_time": SIM_TIME,
            "pair_ratio": PAIR_RATIO,
            "network_sizes": network_sizes,
            "pair_counts_on_100": pair_counts,
        },
        "throughput_vs_network_size": {
            "x": network_sizes,
            "series": size_experiment_results,
            "details": size_experiment_details,
        },
        "throughput_vs_sd_pairs_on_100": {
            "x": pair_counts,
            "series": pair_experiment_results,
            "details": pair_experiment_details,
        },
    }

    results_path = os.path.join(output_dir, "scaling_experiment_results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results_payload, f, indent=2, ensure_ascii=False)

    print("\nSaved:")
    print(f"  {results_path}")
    print_simulation_summary(output_dir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run scaling experiments or regenerate plots from saved results")
    parser.add_argument("--seed", type=int, default=7, help="Random seed used when running full simulations")
    parser.add_argument(
        "--from-results",
        type=str,
        default=None,
        help="Path to scaling_experiment_results.json to regenerate plots without running simulations",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Optional output directory for regenerated plots (defaults to results file directory)",
    )

    args = parser.parse_args()

    if args.from_results:
        out_dir = plot_from_results_file(args.from_results, args.output_dir)
        print("\nSaved plots:")
        print(f"  {out_dir}")
    else:
        run_campaigns(seed=args.seed)
