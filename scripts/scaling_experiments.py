import argparse
import json
import math
import os
import sys
from collections import deque
from dataclasses import dataclass
from itertools import combinations
from statistics import mean
from typing import Any

import matplotlib
matplotlib.use("Agg")
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
from mqns.network.network.timing import TimingModeSyncQCast, complete_cycle_count
from mqns.network.protocol.link_layer import LinkLayer, LinkLayerCounters
from mqns.network.qcast.controller import QCastController, QCastMultiEntController
from mqns.network.qcast.forwarder import QCastForwarder
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
    from simulation_utils import (
        create_simulation_folder,
        mean_ci95,
        print_simulation_summary,
        save_graph,
    )
except ModuleNotFoundError:
    from scripts.simulation_utils import (
        create_simulation_folder,
        mean_ci95,
        print_simulation_summary,
        save_graph,
    )


SIM_TIME = 1000.0
SIM_ACCURACY = 1_000_000
T_PHASE = 1.0
TOTAL_CYCLE_TIME = T_PHASE * 4.0
T_COHERE = 40.0
RESET_MEMORIES_EACH_CYCLE = True
SWAP_POLICY = "l2r"
SWAP_SUCCESS_PROB = 0.85
NETWORK_SIZE_REQUEST_COUNTS = {25: 5, 50: 5, 75: 5, 100: 5}
SD_PAIR_COUNTS = [5, 10, 15, 20, 25]
MIN_NODE_CAPACITY = 16
MAX_NODE_CAPACITY = 25
TARGET_AVG_NODE_DEGREE = 3.6
TOPOLOGY_LINE_FACTOR = TARGET_AVG_NODE_DEGREE / 2.0
SPATIAL_NODE_SPACING = 5.0
SPATIAL_POSITION_JITTER = 0.15
SPATIAL_PAIR_DISTANCE_QUANTILES = (0.80, 1.00)
PHYSICAL_CHANNELS_PER_LINK = 5
MIN_LINK_SUCCESS_PROB = 0.80
MAX_LINK_SUCCESS_PROB = 0.95
ENABLE_QCAST_RECOVERY = True
QCAST_RECOVERY_MODE = "edge_disjoint_e2e_backup"
LOG_LEVEL = "WARN"

LEGACY_ALGORITHM_LABELS = {
    "Dijkstra Saltos Multicanal": "Dijkstra Saltos Multicircuito",
    "Dijkstra Distancia Multicanal": "Dijkstra Distancia Multicircuito",
}


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

    link_layer = LinkLayer(init_fidelity=None)
    if forwarder_class is not None:
        forwarder = forwarder_class(
            k_max=2,
            ps=SWAP_SUCCESS_PROB,
            purif_enabled=False,
            swapping_enabled=True,
        )
    elif qcast_queries:
        forwarder = QCastForwarder(
            k_max=2,
            ps=SWAP_SUCCESS_PROB,
            purif_enabled=False,
            swapping_enabled=True,
        )
    else:
        forwarder = StaticQCastForwarder(
            k_max=2,
            ps=SWAP_SUCCESS_PROB,
            purif_enabled=False,
            swapping_enabled=True,
        )

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

    width = int(ctrl.request_route_info.get(req_id, {}).get("width", 1))
    ctrl.path_w[path_id] = width
    ctrl.path_requests[path_id] = [req_id]
    ctrl.path_request_history[path_id] = [req_id]
    ctrl.path_route_names[path_id] = list(route)
    ctrl.path_route_history[path_id] = list(route)
    ctrl.main_paths_by_req.setdefault(req_id, []).append(path_id)

    # The static route must keep the original request ID so that destination
    # forwarders can report success against the same logical request. Using the
    # path numeric ID here caused the success accounting bucket to be different
    # from the one populated by the route assignment stage, leaving throughput at 0.
    if width > 1:
        m_v = [(width, width) for _ in range(max(0, len(route) - 1))]
        route_path = RoutingPathStatic(route, req_id=req_id, path_id=path_id, m_v=m_v, swap=SWAP_POLICY)
    else:
        route_path = RoutingPathStatic(route, req_id=req_id, path_id=path_id, swap=SWAP_POLICY)

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
    priority_seed: int = 0,
) -> dict[str, Any]:
    ciclos_totales = complete_cycle_count(SIM_TIME, TOTAL_CYCLE_TIME)
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

    ctrl = controller_class(
        k_max=5,
        enable_recovery_paths=ENABLE_QCAST_RECOVERY,
        max_alloc_width=None,
        swap_policy=SWAP_POLICY,
        q_swap=SWAP_SUCCESS_PROB,
        priority_seed=priority_seed,
    )
    attach_controller(net, ctrl)
    net.simulator = sim

    solicitudes = build_requests(net, topo_config)

    qcast_queries = route_alg is None
    for node in net.nodes:
        install_stack(node, controller=ctrl, qcast_queries=qcast_queries, forwarder_class=forwarder_class)
        node.install(sim)

    net.timing = TimingModeSyncQCast(
        t1=T_PHASE,
        t2=T_PHASE,
        t3=T_PHASE,
        t4=T_PHASE,
        reset_memories_each_cycle=RESET_MEMORIES_EACH_CYCLE,
    )
    net.timing.install(net)

    solicitudes_por_id = {req["req_id"]: req for req in solicitudes}

    def route_quality(network, route):
        return obtener_prob_y_fidelidad_de_ruta(
            network,
            route,
            q_swap=SWAP_SUCCESS_PROB,
        )

    if route_alg is not None:
        def route_cycle(cycle_queries):
            cycle_requests = [solicitudes_por_id[query["req_id"]] for query in cycle_queries]
            net.route = route_alg
            net.build_route()
            if reserve_all_capacity:
                assign_dijkstra_routes_with_capacity_reserve_all(
                    net,
                    ctrl,
                    cycle_requests,
                    route_quality,
                    enforce_capacity=use_capacity,
                )
            else:
                assign_dijkstra_routes_with_capacity(
                    net,
                    ctrl,
                    cycle_requests,
                    route_quality,
                    enforce_capacity=use_capacity,
                )

            for req in cycle_requests:
                req_id = req["req_id"]
                info = ctrl.request_route_info.get(req_id)
                route = info.get("route") if info else None
                if route:
                    install_static_route_on_forwarders(net, ctrl, route, req_id)

        ctrl.configure_cycle_routing(solicitudes, route_cycle)
    else:
        ctrl.configure_cycle_routing(solicitudes)

    sim.run()

    resultados = construir_resultados_qcast(ctrl, solicitudes, ciclos_totales)
    counters = LinkLayerCounters.aggregate(net.nodes)
    forwarder_counters = []
    for node in net.nodes:
        counter = getattr(getattr(node, "forwarder", None), "cnt", None)
        if counter is not None:
            forwarder_counters.append(counter)

    total_exitos = sum(r.get("successes", 0) for r in resultados)
    throughput = total_exitos / SIM_TIME
    route_metrics = []
    for cycle_route_info in ctrl.request_route_history.values():
        for info in cycle_route_info.values():
            route_names = info.get("route")
            if not route_names or len(route_names) < 2:
                continue

            route_prob = float(info.get("route_success_prob", 0.0))
            assigned_width = max(1, int(info.get("w_asignado", info.get("width", 1))))
            physical_width = min(
                len(net.get_qchannels_between(route_names[i], route_names[i + 1]))
                for i in range(len(route_names) - 1)
            )
            effective_width = min(assigned_width, physical_width)
            route_metrics.append({
                "success_prob": route_prob,
                "effective_success_prob": 1.0 - (1.0 - route_prob) ** effective_width,
                "hops": len(route_names) - 1,
                "assigned_width": assigned_width,
                "effective_width": effective_width,
            })

    return {
        "throughput": throughput,
        "total_successes": total_exitos,
        "n_etg": counters.n_etg,
        "n_attempts": counters.n_attempts,
        "n_decohered": counters.n_decoh,
        "n_forwarder_entangled": sum(counter.n_entg for counter in forwarder_counters),
        "n_eligible": sum(counter.n_eligible for counter in forwarder_counters),
        "n_swapped_sequential": sum(counter.n_swapped_s for counter in forwarder_counters),
        "n_swapped_parallel": sum(counter.n_swapped_p for counter in forwarder_counters),
        "n_swap_conflicts": sum(counter.n_swap_conflict for counter in forwarder_counters),
        "n_consumed": sum(counter.n_consumed for counter in forwarder_counters),
        "n_cutoff": sum(sum(counter.n_cutoff) for counter in forwarder_counters),
        "n_invalid_e2e_endpoints": getattr(ctrl, "invalid_e2e_endpoint_count", 0),
        "n_recovery_paths": sum(
            len(paths) for paths in getattr(ctrl, "recovery_paths_info", {}).values()
        ),
        "n_requests": len(solicitudes),
        "n_routed_requests": (
            mean(len(info) for info in ctrl.request_route_history.values())
            if ctrl.request_route_history
            else 0.0
        ),
        "mean_route_success_prob": mean(metric["success_prob"] for metric in route_metrics) if route_metrics else 0.0,
        "mean_effective_route_success_prob": (
            mean(metric["effective_success_prob"] for metric in route_metrics) if route_metrics else 0.0
        ),
        "mean_route_hops": mean(metric["hops"] for metric in route_metrics) if route_metrics else 0.0,
        "mean_assigned_width": mean(metric["assigned_width"] for metric in route_metrics) if route_metrics else 0.0,
        "mean_effective_width": mean(metric["effective_width"] for metric in route_metrics) if route_metrics else 0.0,
        "route_diagnostics": {
            str(cycle): {
                req_id: {
                    "route": list(info.get("route") or []),
                    "hops": int(info.get("hops", 0)),
                    "assigned_width": int(info.get("w_asignado", info.get("width", 0))),
                }
                for req_id, info in cycle_info.items()
            }
            for cycle, cycle_info in ctrl.request_route_history.items()
        },
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


def link_success_probability(length: float, min_length: float, max_length: float) -> float:
    if min_length < 0 or max_length <= min_length:
        raise ValueError("length bounds must satisfy 0 <= min_length < max_length")
    if not min_length <= length <= max_length:
        raise ValueError("link length must be within the configured bounds")
    if not 0 < MIN_LINK_SUCCESS_PROB <= MAX_LINK_SUCCESS_PROB <= 1:
        raise ValueError("link success probabilities must satisfy 0 < min <= max <= 1")

    normalized_length = (length - min_length) / (max_length - min_length)
    return MAX_LINK_SUCCESS_PROB * (
        MIN_LINK_SUCCESS_PROB / MAX_LINK_SUCCESS_PROB
    ) ** normalized_length


def build_base_topology_config(
    n_nodes: int,
    *,
    line_factor: float,
    max_parallel_channels: int,
    min_length: float,
    max_length: float,
) -> dict[str, Any]:
    node_names = [f"n{i + 1}" for i in range(n_nodes)]
    for _ in range(100):
        base_edges = edges_from_random_topology(n_nodes=n_nodes, line_factor=line_factor)
        node_degree = {name: 0 for name in node_names}
        for u, v in base_edges:
            node_degree[u] += 1
            node_degree[v] += 1
        if all(degree <= MAX_NODE_CAPACITY for degree in node_degree.values()):
            break
    else:
        raise ValueError(
            f"Could not generate a topology with maximum degree <= {MAX_NODE_CAPACITY}"
        )

    return build_topology_config_from_edges(
        node_names,
        base_edges,
        max_parallel_channels=max_parallel_channels,
        min_length=min_length,
        max_length=max_length,
    )


def build_topology_config_from_edges(
    node_names: list[str],
    base_edges: list[tuple[str, str]],
    *,
    max_parallel_channels: int,
    min_length: float,
    max_length: float,
) -> dict[str, Any]:
    node_degree = {name: 0 for name in node_names}
    for u, v in base_edges:
        node_degree[u] += 1
        node_degree[v] += 1
    if any(degree > MAX_NODE_CAPACITY for degree in node_degree.values()):
        raise ValueError("Topology degree exceeds the maximum node capacity")

    capacities = {
        name: int(rng.integers(max(MIN_NODE_CAPACITY, degree), MAX_NODE_CAPACITY + 1))
        for name, degree in node_degree.items()
    }

    # Channels are physical opportunities; memory limits how many can be
    # reserved concurrently, not how many exist on each link.
    channel_counts = {edge: max_parallel_channels for edge in base_edges}

    enlaces = []
    for u, v in base_edges:
        length = float(rng.uniform(min_length, max_length))
        enlaces.append(
            {
                "u": u,
                "v": v,
                "length": length,
                "prob": link_success_probability(length, min_length, max_length),
                "channels": channel_counts[(u, v)],
            }
        )
    nodos = [{"id": name, "capacity": capacities[name]} for name in node_names]

    return {"nodos": nodos, "enlaces": enlaces, "solicitudes": []}


def build_spatial_scaling_topology_config(
    n_nodes: int,
    *,
    pair_count: int,
    max_parallel_channels: int,
    min_length: float,
    max_length: float,
) -> dict[str, Any]:
    if n_nodes < 2:
        raise ValueError("Spatial topology requires at least two nodes")
    if pair_count < 1 or pair_count * 2 > n_nodes:
        raise ValueError("Spatial topology requires two distinct endpoints per S-D pair")

    node_names = [f"n{i + 1}" for i in range(n_nodes)]
    columns = math.ceil(math.sqrt(n_nodes))
    rows = math.ceil(n_nodes / columns)
    jitter = SPATIAL_NODE_SPACING * SPATIAL_POSITION_JITTER
    positions = {
        name: (
            (index % columns) * SPATIAL_NODE_SPACING + float(rng.uniform(-jitter, jitter)),
            (index // columns) * SPATIAL_NODE_SPACING + float(rng.uniform(-jitter, jitter)),
        )
        for index, name in enumerate(node_names)
    }

    candidate_edges = []
    for left_index, left in enumerate(node_names):
        left_x, left_y = positions[left]
        for right in node_names[left_index + 1 :]:
            right_x, right_y = positions[right]
            distance = math.hypot(right_x - left_x, right_y - left_y)
            candidate_edges.append((distance, left, right))
    candidate_edges.sort()

    parent = {name: name for name in node_names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> bool:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return False
        parent[right_root] = left_root
        return True

    selected_edges: dict[tuple[str, str], float] = {}
    for distance, left, right in candidate_edges:
        if union(left, right):
            selected_edges[(left, right)] = distance
            if len(selected_edges) == n_nodes - 1:
                break

    target_edge_count = min(
        len(candidate_edges),
        max(n_nodes - 1, int(round(n_nodes * TARGET_AVG_NODE_DEGREE / 2.0))),
    )
    for distance, left, right in candidate_edges:
        if len(selected_edges) >= target_edge_count:
            break
        selected_edges.setdefault((left, right), distance)

    degree = {name: 0 for name in node_names}
    for left, right in selected_edges:
        degree[left] += 1
        degree[right] += 1

    capacities = {
        name: int(rng.integers(max(MIN_NODE_CAPACITY, degree[name]), MAX_NODE_CAPACITY + 1))
        for name in node_names
    }
    enlaces = []
    for (u, v), spatial_distance in sorted(selected_edges.items()):
        length = min(max_length, max(min_length, spatial_distance))
        enlaces.append({
            "u": u,
            "v": v,
            "length": length,
            "prob": link_success_probability(length, min_length, max_length),
            "channels": max_parallel_channels,
            "profile": "spatial_local",
        })

    requests = select_spatial_sd_pairs(
        positions,
        pair_count,
        min_quantile=SPATIAL_PAIR_DISTANCE_QUANTILES[0],
        max_quantile=SPATIAL_PAIR_DISTANCE_QUANTILES[1],
    )
    config = {
        "nodos": [
            {
                "id": name,
                "capacity": capacities[name],
                "x": positions[name][0],
                "y": positions[name][1],
            }
            for name in node_names
        ],
        "enlaces": enlaces,
        "solicitudes": requests,
        "scaling_profile": {
            "model": "constant_density_spatial",
            "layout": "jittered_grid",
            "rows": rows,
            "columns": columns,
            "node_spacing": SPATIAL_NODE_SPACING,
            "position_jitter_fraction": SPATIAL_POSITION_JITTER,
            "target_average_node_degree": TARGET_AVG_NODE_DEGREE,
            "pair_distance_quantiles": list(SPATIAL_PAIR_DISTANCE_QUANTILES),
        },
    }
    return config


def select_spatial_sd_pairs(
    positions: dict[str, tuple[float, float]],
    n_pairs: int,
    *,
    min_quantile: float,
    max_quantile: float,
) -> list[dict[str, str]]:
    if not 0 <= min_quantile < max_quantile <= 1:
        raise ValueError("S-D distance quantiles must satisfy 0 <= min < max <= 1")

    candidates = []
    names = list(positions)
    for left_index, left in enumerate(names):
        left_x, left_y = positions[left]
        for right in names[left_index + 1 :]:
            right_x, right_y = positions[right]
            candidates.append((math.hypot(right_x - left_x, right_y - left_y), left, right))
    candidates.sort()

    lower_index = int(math.floor((len(candidates) - 1) * min_quantile))
    upper_index = int(math.ceil((len(candidates) - 1) * max_quantile))
    distance_band = candidates[lower_index : upper_index + 1]
    rng.shuffle(distance_band)

    selected: list[dict[str, str]] = []
    used_nodes: set[str] = set()
    for _, src, dst in distance_band:
        if src in used_nodes or dst in used_nodes:
            continue
        if bool(rng.integers(0, 2)):
            src, dst = dst, src
        selected.append({"src": src, "dst": dst})
        used_nodes.update((src, dst))
        if len(selected) == n_pairs:
            return selected

    raise ValueError(
        f"Spatial distance band contains only {len(selected)} node-disjoint pairs; "
        f"{n_pairs} are required"
    )


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


def topology_diagnostics(
    base_config: dict[str, Any],
    solicitudes: list[dict[str, str]],
) -> dict[str, float | int]:
    adjacency = {node["id"]: set() for node in base_config["nodos"]}
    for edge in base_config["enlaces"]:
        adjacency[edge["u"]].add(edge["v"])
        adjacency[edge["v"]].add(edge["u"])

    distances_by_source: dict[str, dict[str, int]] = {}
    all_distances: list[int] = []
    diameter = 0
    for src in adjacency:
        distances = {src: 0}
        queue = deque([src])
        while queue:
            current = queue.popleft()
            for neighbor in adjacency[current]:
                if neighbor in distances:
                    continue
                distances[neighbor] = distances[current] + 1
                queue.append(neighbor)
        if len(distances) != len(adjacency):
            raise ValueError("Generated topology is not connected")
        distances_by_source[src] = distances
        for dst, distance in distances.items():
            if src < dst:
                all_distances.append(distance)
                diameter = max(diameter, distance)

    selected_distances = [
        distances_by_source[request["src"]][request["dst"]]
        for request in solicitudes
    ]
    positions = {
        node["id"]: (float(node["x"]), float(node["y"]))
        for node in base_config["nodos"]
        if "x" in node and "y" in node
    }
    link_lengths = [float(edge["length"]) for edge in base_config["enlaces"]]
    node_count = len(adjacency)
    edge_count = len(base_config["enlaces"])
    diagnostics: dict[str, float | int] = {
        "node_count": node_count,
        "edge_count": edge_count,
        "average_node_degree": 2.0 * edge_count / node_count,
        "diameter_hops": diameter,
        "mean_shortest_path_hops": mean(all_distances),
        "mean_selected_pair_hops": mean(selected_distances),
        "min_selected_pair_hops": min(selected_distances),
        "max_selected_pair_hops": max(selected_distances),
    }
    if positions:
        selected_spatial_distances = [
            math.dist(positions[request["src"]], positions[request["dst"]])
            for request in solicitudes
        ]
        x_values = [position[0] for position in positions.values()]
        y_values = [position[1] for position in positions.values()]
        deployment_area = (
            (max(x_values) - min(x_values) + SPATIAL_NODE_SPACING)
            * (max(y_values) - min(y_values) + SPATIAL_NODE_SPACING)
        )
        diagnostics.update({
            "deployment_area": deployment_area,
            "node_density": node_count / deployment_area,
            "mean_link_length": mean(link_lengths),
            "mean_selected_pair_spatial_distance": mean(selected_spatial_distances),
        })
    return diagnostics


def write_scenario(path: str, config: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def plot_throughput_vs_network_size(
    output_dir: str,
    sizes: list[int],
    results: dict[str, list[float]],
    ci95_half_widths: dict[str, list[float]] | None = None,
    ylabel: str = "Throughput (EPR/s)",
):
    fig, ax = plt.subplots(figsize=(10.5, 6.4))
    markers = ["o", "s", "^", "d", "x", "P", "*"]
    linestyles = ["-", "--", "-.", ":", "-", "--", "-."]

    for i, (alg_name, y_values) in enumerate(results.items()):
        ax.errorbar(
            sizes,
            y_values,
            yerr=ci95_half_widths.get(alg_name) if ci95_half_widths else None,
            marker=markers[i % len(markers)],
            linestyle=linestyles[i % len(linestyles)],
            linewidth=2.4,
            markersize=8,
            capsize=3,
            label=LEGACY_ALGORITHM_LABELS.get(alg_name, alg_name),
        )

    ax.set_xlabel("|V| (nodos)", fontsize=16)
    ax.set_ylabel(ylabel, fontsize=15)
    ax.tick_params(axis="both", labelsize=14)
    ax.grid(True, linestyle="--", alpha=0.35)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.18),
        ncol=2,
        fontsize=12,
        frameon=False,
    )
    fig.subplots_adjust(bottom=0.32)
    save_graph(fig, output_dir, "01_throughput_vs_network_size")


def plot_throughput_vs_sd_pairs(
    output_dir: str,
    pair_counts: list[int],
    results: dict[str, list[float]],
    ci95_half_widths: dict[str, list[float]] | None = None,
):
    fig, ax = plt.subplots(figsize=(10.5, 6.4))
    markers = ["o", "s", "^", "d", "x", "P", "*"]
    linestyles = ["-", "--", "-.", ":", "-", "--", "-."]

    for i, (alg_name, y_values) in enumerate(results.items()):
        ax.errorbar(
            pair_counts,
            y_values,
            yerr=ci95_half_widths.get(alg_name) if ci95_half_widths else None,
            marker=markers[i % len(markers)],
            linestyle=linestyles[i % len(linestyles)],
            linewidth=2.4,
            markersize=8,
            capsize=3,
            label=LEGACY_ALGORITHM_LABELS.get(alg_name, alg_name),
        )

    ax.set_xlabel("Número de parejas S-D", fontsize=16)
    ax.set_ylabel("Throughput (EPR/s)", fontsize=16)
    ax.tick_params(axis="both", labelsize=14)
    ax.grid(True, linestyle="--", alpha=0.35)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.18),
        ncol=2,
        fontsize=12,
        frameon=False,
    )
    fig.subplots_adjust(bottom=0.32)
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
        for walk_root, _, files in os.walk(outputs_dir):
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
    size_ci95 = size_section.get("ci95_half_width")
    pair_counts = pair_section.get("x")
    pair_series = pair_section.get("series")
    pair_ci95 = pair_section.get("ci95_half_width")

    if not isinstance(network_sizes, list) or not isinstance(size_series, dict):
        raise ValueError("Invalid results file: missing throughput_vs_network_size.x or .series")
    if not isinstance(pair_counts, list) or not isinstance(pair_series, dict):
        raise ValueError("Invalid results file: missing throughput_vs_sd_pairs_on_100.x or .series")

    target_output_dir = output_dir or os.path.dirname(os.path.abspath(resolved_results_path))
    os.makedirs(target_output_dir, exist_ok=True)

    plot_throughput_vs_network_size(
        target_output_dir,
        network_sizes,
        size_series,
        ci95_half_widths=size_ci95 if isinstance(size_ci95, dict) else None,
        ylabel=(
            "Throughput medio por pareja S-D (EPR/s)"
            if size_section.get("metric") == "mean_throughput_per_sd_pair_eps"
            else "Throughput (EPR/s)"
        ),
    )
    plot_throughput_vs_sd_pairs(
        target_output_dir,
        pair_counts,
        pair_series,
        ci95_half_widths=pair_ci95 if isinstance(pair_ci95, dict) else None,
    )
    print(f"Using results file: {resolved_results_path}")
    return target_output_dir


def summarize_replications(samples: list[dict[str, Any]]) -> dict[str, Any]:
    throughputs = [float(sample["throughput"]) for sample in samples]
    throughput_summary = mean_ci95(throughputs, lower_bound=0.0)
    return {
        "throughput": throughput_summary["mean"],
        "throughput_median": throughput_summary["median"],
        "throughput_stddev": throughput_summary["std"],
        "throughput_ci95_half_width": throughput_summary["ci95_half_width"],
        "throughput_ci95_low": throughput_summary["ci95_low"],
        "throughput_ci95_high": throughput_summary["ci95_high"],
        "throughput_interval_method": throughput_summary["interval_method"],
        "throughput_samples": throughput_summary["samples"],
        "total_successes_mean": mean(float(sample["total_successes"]) for sample in samples),
        "n_etg_mean": mean(float(sample["n_etg"]) for sample in samples),
        "n_attempts_mean": mean(float(sample["n_attempts"]) for sample in samples),
        "n_decohered_mean": mean(float(sample["n_decohered"]) for sample in samples),
        "n_forwarder_entangled_mean": mean(float(sample["n_forwarder_entangled"]) for sample in samples),
        "n_eligible_mean": mean(float(sample["n_eligible"]) for sample in samples),
        "n_swapped_sequential_mean": mean(float(sample["n_swapped_sequential"]) for sample in samples),
        "n_swapped_parallel_mean": mean(float(sample["n_swapped_parallel"]) for sample in samples),
        "n_swap_conflicts_mean": mean(float(sample["n_swap_conflicts"]) for sample in samples),
        "n_consumed_mean": mean(float(sample["n_consumed"]) for sample in samples),
        "n_cutoff_mean": mean(float(sample["n_cutoff"]) for sample in samples),
        "n_invalid_e2e_endpoints_mean": mean(
            float(sample["n_invalid_e2e_endpoints"]) for sample in samples
        ),
        "n_recovery_paths_mean": mean(float(sample["n_recovery_paths"]) for sample in samples),
        "n_requests": samples[0]["n_requests"] if samples else 0,
        "n_routed_requests_mean": mean(float(sample["n_routed_requests"]) for sample in samples),
        "mean_route_success_prob": mean(float(sample["mean_route_success_prob"]) for sample in samples),
        "mean_effective_route_success_prob": mean(
            float(sample["mean_effective_route_success_prob"]) for sample in samples
        ),
        "mean_route_hops": mean(float(sample["mean_route_hops"]) for sample in samples),
        "mean_assigned_width": mean(float(sample["mean_assigned_width"]) for sample in samples),
        "mean_effective_width": mean(float(sample["mean_effective_width"]) for sample in samples),
        "samples": samples,
    }


def summarize_paired_throughput(
    samples_by_algorithm: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    comparisons: dict[str, dict[str, Any]] = {}
    for first, second in combinations(samples_by_algorithm, 2):
        first_samples = samples_by_algorithm[first]
        second_samples = samples_by_algorithm[second]
        if len(first_samples) != len(second_samples):
            raise ValueError(
                f"Cannot pair {first} and {second}: different sample counts"
            )
        deltas = [
            float(first_sample["throughput"]) - float(second_sample["throughput"])
            for first_sample, second_sample in zip(first_samples, second_samples)
        ]
        comparisons[f"{first} - {second}"] = mean_ci95(deltas)
    return comparisons


def run_campaigns(seed: int = 7, repetitions: int = 10):
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")

    log.set_default_level(LOG_LEVEL)

    algorithms = [
        AlgorithmSpec("Dijkstra Saltos", QCastController, DijkstraRouteAlgorithm(), True, False),
        AlgorithmSpec("Dijkstra Distancia", QCastController, DijkstraDistanceRouteAlgorithm(), True, False),
        AlgorithmSpec("Dijkstra Saltos Multicircuito", QCastController, DijkstraRouteAlgorithm(), True, True),
        AlgorithmSpec("Dijkstra Distancia Multicircuito", QCastController, DijkstraDistanceRouteAlgorithm(), True, True),
        AlgorithmSpec("Q-CAST", QCastController, None, False, False),
        AlgorithmSpec("Q-CAST Multientrelazamiento", QCastMultiEntController, None, False, False),
    ]

    output_dir = create_simulation_folder()
    scenario_dir = os.path.join(output_dir, "scenarios")
    os.makedirs(scenario_dir, exist_ok=True)

    network_sizes = [25, 50, 75, 100]
    size_experiment_results: dict[str, list[float]] = {alg.name: [] for alg in algorithms}
    size_experiment_stddev: dict[str, list[float]] = {alg.name: [] for alg in algorithms}
    size_experiment_ci95: dict[str, list[float]] = {alg.name: [] for alg in algorithms}
    size_experiment_details = []
    replication_seeds = [seed + i for i in range(repetitions)]
    simulation_seeds = [seed + 10_000 + i for i in range(repetitions)]
    priority_seeds = [seed + 20_000 + i for i in range(repetitions)]

    print(f"Running throughput vs network size experiment ({repetitions} repetitions per point)...")
    for n_nodes in network_sizes:
        request_count = NETWORK_SIZE_REQUEST_COUNTS[n_nodes]
        samples_by_algorithm: dict[str, list[dict[str, Any]]] = {alg.name: [] for alg in algorithms}
        replications = []
        print(f"  |V|={n_nodes}, S-D pairs={request_count}")

        for rep_index, (scenario_seed, simulation_seed, priority_seed) in enumerate(
            zip(replication_seeds, simulation_seeds, priority_seeds),
            start=1,
        ):
            rng.reseed(scenario_seed)
            scenario_config = build_spatial_scaling_topology_config(
                n_nodes=n_nodes,
                pair_count=request_count,
                max_parallel_channels=PHYSICAL_CHANNELS_PER_LINK,
                min_length=1.0,
                max_length=10.0,
            )
            solicitudes = scenario_config["solicitudes"]
            topology_metrics = topology_diagnostics(scenario_config, solicitudes)
            scenario_path = os.path.join(scenario_dir, f"size_{n_nodes}_rep_{rep_index}.json")
            write_scenario(scenario_path, scenario_config)

            replication_info = {
                "replication": rep_index,
                "scenario_seed": scenario_seed,
                "simulation_seed": simulation_seed,
                "priority_seed": priority_seed,
                "scenario": os.path.relpath(scenario_path, output_dir),
                "topology": topology_metrics,
                "throughput": {},
            }
            for alg in algorithms:
                # Common random numbers: every algorithm starts this replication
                # from the same simulator RNG state.
                rng.reseed(simulation_seed)
                metrics = run_single_simulation(
                    scenario_path=scenario_path,
                    controller_class=alg.controller_class,
                    route_alg=alg.route_alg,
                    use_capacity=alg.use_capacity,
                    reserve_all_capacity=alg.reserve_all_capacity,
                    forwarder_class=alg.forwarder_class,
                    priority_seed=priority_seed,
                )
                samples_by_algorithm[alg.name].append(metrics)
                replication_info["throughput"][alg.name] = metrics
            replications.append(replication_info)

        run_info = {
            "network_size": n_nodes,
            "sd_pairs": request_count,
            "throughput": {},
            "replications": replications,
        }
        for alg in algorithms:
            summary = summarize_replications(samples_by_algorithm[alg.name])
            size_experiment_results[alg.name].append(summary["throughput"])
            size_experiment_stddev[alg.name].append(summary["throughput_stddev"])
            size_experiment_ci95[alg.name].append(
                summary["throughput_ci95_half_width"]
            )
            run_info["throughput"][alg.name] = summary
            print(
                f"    {alg.name:<28} throughput="
                f"{summary['throughput']:.4f} +/- "
                f"{summary['throughput_ci95_half_width']:.4f} EPR/s (95% CI)"
            )
        run_info["paired_throughput_differences"] = summarize_paired_throughput(
            samples_by_algorithm
        )

        size_experiment_details.append(run_info)

    plot_throughput_vs_network_size(
        output_dir,
        network_sizes,
        size_experiment_results,
        ci95_half_widths=size_experiment_ci95,
    )

    print(f"Running throughput vs S-D pairs experiment on |V|=100 ({repetitions} repetitions per point)...")
    pair_counts = SD_PAIR_COUNTS
    pair_experiment_results: dict[str, list[float]] = {alg.name: [] for alg in algorithms}
    pair_experiment_stddev: dict[str, list[float]] = {alg.name: [] for alg in algorithms}
    pair_experiment_ci95: dict[str, list[float]] = {alg.name: [] for alg in algorithms}
    pair_experiment_details = []
    pair_samples: dict[int, dict[str, list[dict[str, Any]]]] = {
        count: {alg.name: [] for alg in algorithms} for count in pair_counts
    }
    pair_replications: dict[int, list[dict[str, Any]]] = {count: [] for count in pair_counts}

    for rep_index, (scenario_seed, simulation_seed, priority_seed) in enumerate(
        zip(replication_seeds, simulation_seeds, priority_seeds),
        start=1,
    ):
        rng.reseed(scenario_seed)
        base_100 = build_base_topology_config(
            n_nodes=100,
            line_factor=TOPOLOGY_LINE_FACTOR,
            max_parallel_channels=PHYSICAL_CHANNELS_PER_LINK,
            min_length=1.0,
            max_length=10.0,
        )
        shuffled_candidates = list(list_non_adjacent_pairs(base_100))
        rng.shuffle(shuffled_candidates)
        if max(pair_counts) > len(shuffled_candidates):
            raise ValueError(
                f"Not enough non-adjacent candidate pairs for |V|=100. "
                f"Available: {len(shuffled_candidates)}"
            )
        oriented_candidates = []
        for src, dst in shuffled_candidates[:max(pair_counts)]:
            if bool(rng.integers(0, 2)):
                src, dst = dst, src
            oriented_candidates.append({"src": src, "dst": dst})

        scenarios: dict[int, str] = {}
        for n_pairs in pair_counts:
            scenario_config = dict(base_100)
            scenario_config["solicitudes"] = oriented_candidates[:n_pairs]
            scenario_path = os.path.join(
                scenario_dir,
                f"pairs_100_nodes_{n_pairs}_rep_{rep_index}.json",
            )
            write_scenario(scenario_path, scenario_config)
            scenarios[n_pairs] = scenario_path

        for n_pairs, scenario_path in scenarios.items():
            replication_info = {
                "replication": rep_index,
                "scenario_seed": scenario_seed,
                "simulation_seed": simulation_seed,
                "priority_seed": priority_seed,
                "scenario": os.path.relpath(scenario_path, output_dir),
                "throughput": {},
            }
            for alg in algorithms:
                rng.reseed(simulation_seed)
                metrics = run_single_simulation(
                    scenario_path=scenario_path,
                    controller_class=alg.controller_class,
                    route_alg=alg.route_alg,
                    use_capacity=alg.use_capacity,
                    reserve_all_capacity=alg.reserve_all_capacity,
                    forwarder_class=alg.forwarder_class,
                    priority_seed=priority_seed,
                )
                pair_samples[n_pairs][alg.name].append(metrics)
                replication_info["throughput"][alg.name] = metrics
            pair_replications[n_pairs].append(replication_info)

    for n_pairs in pair_counts:
        print(f"  |V|=100, S-D pairs={n_pairs}")
        run_info = {
            "network_size": 100,
            "sd_pairs": n_pairs,
            "throughput": {},
            "replications": pair_replications[n_pairs],
        }
        for alg in algorithms:
            summary = summarize_replications(pair_samples[n_pairs][alg.name])
            pair_experiment_results[alg.name].append(summary["throughput"])
            pair_experiment_stddev[alg.name].append(summary["throughput_stddev"])
            pair_experiment_ci95[alg.name].append(
                summary["throughput_ci95_half_width"]
            )
            run_info["throughput"][alg.name] = summary
            print(
                f"    {alg.name:<28} throughput="
                f"{summary['throughput']:.4f} +/- "
                f"{summary['throughput_ci95_half_width']:.4f} EPR/s (95% CI)"
            )
        run_info["paired_throughput_differences"] = summarize_paired_throughput(
            pair_samples[n_pairs]
        )
        pair_experiment_details.append(run_info)

    plot_throughput_vs_sd_pairs(
        output_dir,
        pair_counts,
        pair_experiment_results,
        ci95_half_widths=pair_experiment_ci95,
    )

    results_payload = {
        "schema_version": "2.0",
        "config": {
            "seed": seed,
            "repetitions": repetitions,
            "scenario_seeds": replication_seeds,
            "simulation_seeds": simulation_seeds,
            "priority_seeds": priority_seeds,
            "sim_time": SIM_TIME,
            "t_cohere": T_COHERE,
            "reset_memories_each_cycle": RESET_MEMORIES_EACH_CYCLE,
            "swap_policy": SWAP_POLICY,
            "swap_success_probability": SWAP_SUCCESS_PROB,
            "qcast_recovery_enabled": ENABLE_QCAST_RECOVERY,
            "qcast_recovery_mode": QCAST_RECOVERY_MODE,
            "link_success_probability_range": [
                MIN_LINK_SUCCESS_PROB,
                MAX_LINK_SUCCESS_PROB,
            ],
            "link_success_probability_model": "exponential_decay_with_length",
            "node_capacity_range": [MIN_NODE_CAPACITY, MAX_NODE_CAPACITY],
            "target_average_node_degree": TARGET_AVG_NODE_DEGREE,
            "topology_line_factor": TOPOLOGY_LINE_FACTOR,
            "physical_channels_per_link": PHYSICAL_CHANNELS_PER_LINK,
            "network_size_request_counts": NETWORK_SIZE_REQUEST_COUNTS,
            "network_size_topology_model": "constant_density_spatial",
            "spatial_layout": "jittered_grid",
            "spatial_node_spacing": SPATIAL_NODE_SPACING,
            "spatial_position_jitter_fraction": SPATIAL_POSITION_JITTER,
            "spatial_pair_distance_quantiles": SPATIAL_PAIR_DISTANCE_QUANTILES,
            "network_sizes": network_sizes,
            "pair_counts_on_100": pair_counts,
        },
        "throughput_vs_network_size": {
            "x": network_sizes,
            "metric": "mean_throughput_eps",
            "series": size_experiment_results,
            "stddev": size_experiment_stddev,
            "ci95_half_width": size_experiment_ci95,
            "interval_method": "student_t",
            "details": size_experiment_details,
        },
        "throughput_vs_sd_pairs_on_100": {
            "x": pair_counts,
            "series": pair_experiment_results,
            "stddev": pair_experiment_stddev,
            "ci95_half_width": pair_experiment_ci95,
            "interval_method": "student_t",
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
        "--repetitions",
        type=int,
        default=10,
        help="Independent seeded repetitions per experimental point (default: 10)",
    )
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
        run_campaigns(seed=args.seed, repetitions=args.repetitions)