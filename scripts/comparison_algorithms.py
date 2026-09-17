import argparse
import json
import math
import os
import random
import shutil
import sys
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mqns.simulator import Simulator
from mqns.network.network.timing import TimingModeSyncQCast, complete_cycle_count
from mqns.network.network.network import QuantumNetwork
from mqns.network.network.reporting import (
    build_request_id,
    compute_pair_success_average_by_cycle,
    compute_pair_success_rate_by_cycle,
    compute_request_success_rate_by_cycle,
    construir_resultados_qcast,
    obtener_prob_y_fidelidad_de_ruta,
)
from mqns.network.fw.routing import RoutingPathStatic
from mqns.network.route import (
    DijkstraDistanceRouteAlgorithm,
    DijkstraRouteAlgorithm,
    assign_dijkstra_routes_with_capacity,
    assign_dijkstra_routes_with_capacity_reserve_all,
)
from mqns.network.qcast.controller import QCastController, QCastMultiEntController
from mqns.network.qcast.forwarder import QCastForwarder
from mqns.network.protocol.link_layer import LinkLayer, LinkLayerCounters
from mqns.entity.qchannel.link_arch_sim import LinkArchSim
from mqns.utils import log, rng
from mqns.entity.memory.memory import QuantumMemory
try:
    from simulation_utils import (
        create_simulation_folder,
        save_graph,
        save_topology_diagram,
        save_link_metadata,
        print_simulation_summary,
    )
except ModuleNotFoundError:
    from scripts.simulation_utils import (
        create_simulation_folder,
        save_graph,
        save_topology_diagram,
        save_link_metadata,
        print_simulation_summary,
    )


DEFAULT_SCENARIO_PATH = os.path.join(os.path.dirname(__file__), "..", "escenario_grande_multicanal_w.json")
DEFAULT_SIM_TIME = 1000.0
REQUEST_REPEAT = 1
MEMORY_T_COHERE = 40.0
RESET_MEMORIES_EACH_CYCLE = True
SWAP_POLICY = "l2r"
DEFAULT_CHANNELS_PER_LINK = 5

T_PHASE = 1.0
TOTAL_CYCLE_TIME = T_PHASE * 4
VERBOSE_RUN_DETAILS = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Comparativa algoritmos (logica original) sobre topologia degradada de enlaces"
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default=DEFAULT_SCENARIO_PATH,
        help="Escenario base JSON",
    )
    parser.add_argument(
        "--scenario-mode",
        choices=("as-is", "degraded"),
        default="degraded",
        help="Usa el JSON sin transformarlo o aplica la degradacion configurable",
    )
    parser.add_argument(
        "--output-dir",
        help="Directorio nuevo de salida; por defecto se usa outputs/<timestamp>",
    )
    parser.add_argument(
        "--sim-time",
        type=float,
        default=DEFAULT_SIM_TIME,
        help="Tiempo total de simulacion",
    )
    parser.add_argument(
        "--fidelity-scale",
        type=float,
        default=0.95,
        help="Factor de degradacion para fidelidad de enlace",
    )
    parser.add_argument(
        "--min-link-fidelity",
        type=float,
        default=0.92,
        help="Cota inferior de fidelidad por enlace tras degradar",
    )
    parser.add_argument(
        "--alpha-scale",
        type=float,
        default=1.05,
        help="Factor de degradacion para alpha de atenuacion",
    )
    parser.add_argument(
        "--length-scale",
        type=float,
        default=1.02,
        help="Factor de degradacion para longitud de enlaces",
    )
    parser.add_argument(
        "--channels-per-link",
        type=int,
        default=DEFAULT_CHANNELS_PER_LINK,
        help="Numero de canales fisicos paralelos por enlace",
    )
    parser.add_argument(
        "--preserve-parallel-links",
        action="store_true",
        help="Mantiene cada entrada paralela del escenario como un enlace fisico independiente",
    )
    parser.add_argument(
        "--legacy-fidelity-scaling",
        action="store_true",
        help="Aplica fidelity-scale a la fidelidad original, como en las ejecuciones historicas",
    )
    parser.add_argument(
        "--eta-d",
        type=float,
        default=0.8,
        help="Eficiencia de deteccion usada en la probabilidad fisica de cada enlace",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="WARN",
        choices=["CRITICAL", "FATAL", "ERROR", "WARN", "INFO", "DEBUG"],
        help="Nivel de logging",
    )
    parser.add_argument(
        "--request-fraction",
        type=float,
        default=1.0,
        help="Fraccion de solicitudes a simular (0,1]",
    )
    parser.add_argument(
        "--request-seed",
        type=int,
        default=42,
        help="Semilla para muestreo de solicitudes cuando request-fraction < 1",
    )
    parser.add_argument(
        "--simulation-seed",
        type=int,
        default=10007,
        help="Semilla comun de simulacion para todos los algoritmos",
    )
    parser.add_argument(
        "--priority-seed",
        type=int,
        default=20011,
        help="Semilla independiente para el orden de prioridad de solicitudes",
    )
    parser.add_argument(
        "--swap-policy",
        choices=("l2r", "asap", "r2l", "baln"),
        default=SWAP_POLICY,
        help="Politica de swapping comun para todos los algoritmos",
    )
    parser.add_argument(
        "--swap-success-prob",
        type=float,
        default=0.85,
        help="Probabilidad de exito de cada operacion de swapping",
    )
    return parser.parse_args()


def build_bad_link_scenario(base_scenario_path: str, output_path: str, args: argparse.Namespace) -> str:
    if not 0.0 <= float(args.eta_d) <= 1.0:
        raise ValueError("--eta-d debe estar en el intervalo [0, 1]")

    with open(base_scenario_path, "r", encoding="utf-8") as f:
        topo = json.load(f)

    if args.preserve_parallel_links:
        edges = [dict(edge) for edge in topo.get("enlaces", [])]
    else:
        grouped_edges: dict[tuple[str, str], list[dict]] = {}
        for edge in topo.get("enlaces", []):
            edge_key = tuple(sorted((edge["u"], edge["v"])))
            grouped_edges.setdefault(edge_key, []).append(dict(edge))

        edges = []
        for edge_key, parallel_entries in grouped_edges.items():
            representative = dict(parallel_entries[0])
            for edge in parallel_entries[1:]:
                for field in ("prob", "alpha", "eta_s"):
                    if representative.get(field) != edge.get(field):
                        raise ValueError(
                            f"Metadatos inconsistentes para el enlace {edge_key}: {field}. "
                            "Use --preserve-parallel-links para conservar entradas distintas."
                        )
            representative["length"] = sum(
                float(edge.get("length", 3.0))
                for edge in parallel_entries
            ) / len(parallel_entries)
            edges.append(representative)
    topo["enlaces"] = edges
    scaled_lengths = [
        float(edge.get("length", 3.0)) * float(args.length_scale)
        for edge in edges
    ]
    if not scaled_lengths:
        raise ValueError("El escenario debe contener al menos un enlace")

    link_arch = LinkArchSim()

    for edge, scaled_length in zip(edges, scaled_lengths):
        original_length = float(edge.get("length", 3.0))
        edge["length"] = scaled_length

        base_alpha = float(edge.get("alpha", 0.2))
        scaled_alpha = base_alpha * float(args.alpha_scale)
        edge["alpha"] = scaled_alpha
        eta_s = float(edge.get("eta_s", 0.7))
        eta_d = float(args.eta_d)
        probability = link_arch._compute_success_prob(
            length=scaled_length,
            alpha=scaled_alpha,
            eta_s=eta_s,
            eta_d=eta_d,
        )
        edge["prob"] = probability
        edge["p_s"] = probability
        edge["eta_d"] = eta_d
        edge["channels"] = int(args.channels_per_link)

        if args.legacy_fidelity_scaling:
            base_fidelity = float(edge.get("fidelity", math.exp(-base_alpha * original_length)))
            link_fidelity = max(
                float(args.min_link_fidelity),
                min(0.999, base_fidelity * float(args.fidelity_scale)),
            )
        else:
            link_fidelity = max(
                float(args.min_link_fidelity),
                min(0.999, math.exp(-scaled_alpha * scaled_length)),
            )
        edge["fidelity"] = link_fidelity

        werner_w = (4.0 * link_fidelity - 1.0) / 3.0
        if not 0.0 < werner_w <= 1.0:
            raise ValueError(
                f"La fidelidad {link_fidelity:.6f} del enlace "
                f"{edge['u']}-{edge['v']} no puede representarse mediante "
                "depolarizacion exponencial Werner"
            )
        if scaled_length <= 0.0:
            if not math.isclose(werner_w, 1.0):
                raise ValueError(
                    f"El enlace {edge['u']}-{edge['v']} tiene longitud cero y "
                    "fidelidad no ideal; no se puede derivar una tasa por kilometro"
                )
            depolar_rate = 0.0
        else:
            depolar_rate = -math.log(werner_w) / scaled_length
        edge["transfer_error"] = f"DEPOLAR:{depolar_rate:.12g}"

    solicitudes = topo.get("solicitudes", [])
    if 0 < float(args.request_fraction) < 1 and solicitudes:
        sample_size = max(1, int(round(len(solicitudes) * float(args.request_fraction))))
        rng = random.Random(args.request_seed)
        topo["solicitudes"] = rng.sample(solicitudes, sample_size)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(topo, f, indent=2, ensure_ascii=False)

    return output_path


def configured_link_success_probability(edge: dict) -> float:
    if edge.get("prob") is not None:
        return float(edge["prob"])
    return LinkArchSim()._compute_success_prob(
        length=float(edge.get("length", 3.0)),
        alpha=float(edge.get("alpha", 0.2)),
        eta_s=float(edge.get("eta_s", 0.7)),
        eta_d=float(edge.get("eta_d", 0.8)),
    )


class StaticQCastForwarder(QCastForwarder):
    def _send_initial_queries(self):
        return


def install_stack(
    node,
    controller=None,
    qcast_queries=True,
    forwarder_class=None,
    swap_success_prob=1.0,
):
    if not hasattr(node, "memory"):
        mem = QuantumMemory(name=f"mem_{node.name}", capacity=100, t_cohere=MEMORY_T_COHERE)
        node.memory = mem
    mem = node.memory
    if hasattr(mem, "_t_cohere"):
        mem._t_cohere = MEMORY_T_COHERE

    # Use per-link error/fidelity models from topology instead of a fixed 0.99 seed.
    link_layer = LinkLayer(init_fidelity=None)
    if forwarder_class is not None:
        forwarder = forwarder_class(
            k_max=2,
            ps=swap_success_prob,
            purif_enabled=False,
            swapping_enabled=True,
        )
    elif qcast_queries:
        forwarder = QCastForwarder(
            k_max=2,
            ps=swap_success_prob,
            purif_enabled=False,
            swapping_enabled=True,
        )
    else:
        forwarder = StaticQCastForwarder(
            k_max=2,
            ps=swap_success_prob,
            purif_enabled=False,
            swapping_enabled=True,
        )

    if controller:
        forwarder.controller = controller

    node.add_apps([link_layer, forwarder])
    setattr(node, "forwarder", forwarder)
    return forwarder


def build_requests(net, topo_config):
    solicitudes = []
    idx = 0
    for _round in range(REQUEST_REPEAT):
        for req in topo_config.get("solicitudes", []):
            src = net.get_node(req["src"])
            dst = net.get_node(req["dst"])
            if src and dst:
                req_id = build_request_id(src.name, dst.name, idx)
                net.add_request(src, dst, {"req_id": req_id})
                solicitudes.append({"req_id": req_id, "src": src, "dst": dst})
                idx += 1
    return solicitudes


def attach_controller(net, ctrl):
    setattr(net, "controller", ctrl)
    setattr(ctrl, "net", net)
    if net.nodes:
        net.nodes[0].add_apps(ctrl)


def install_static_route_on_forwarders(net, ctrl, route, req_id):
    path_id = ctrl.next_path_id
    ctrl.next_path_id += 1
    width = ctrl.request_route_info.get(req_id, {}).get("width", 1)
    ctrl.path_w[path_id] = int(width)
    ctrl.path_requests[path_id] = [req_id]
    ctrl.path_request_history[path_id] = [req_id]
    ctrl.path_route_names[path_id] = list(route)
    ctrl.path_route_history[path_id] = list(route)
    ctrl.main_paths_by_req.setdefault(req_id, []).append(path_id)

    if width > 1:
        m_v = [(int(width), int(width)) for _ in range(max(0, len(route) - 1))]
        route_path = RoutingPathStatic(
            route,
            req_id=req_id,
            path_id=path_id,
            m_v=m_v,
            swap=SWAP_POLICY,
        )
    else:
        route_path = RoutingPathStatic(
            route,
            req_id=req_id,
            path_id=path_id,
            swap=SWAP_POLICY,
        )

    instructions = next(route_path.compute_paths(net))
    install_msg = {"cmd": "INSTALL_PATH", "path_id": path_id, "instructions": instructions}
    for node_name in route:
        qnode = net.get_node(node_name)
        if hasattr(qnode, "forwarder"):
            qnode.forwarder.handle_classic_packet(qnode, install_msg)


def ejecutar_simulacion(
    nombre,
    controller_class,
    scenario_path,
    sim_time,
    route_alg=None,
    use_capacity=True,
    reserve_all_capacity=False,
    forwarder_class=None,
    enable_recovery_paths=True,
    collect_validation_history=False,
    swap_success_prob=1.0,
    priority_seed=0,
):
    print(f"\n--- Ejecutando: {nombre} ---")

    ciclos_totales = complete_cycle_count(sim_time, TOTAL_CYCLE_TIME)
    with open(scenario_path, "r", encoding="utf-8") as f:
        topo_config = json.load(f)

    sim = Simulator(0, sim_time, accuracy=1000000)
    net = QuantumNetwork(None)
    net.build_topology_from_json(scenario_path)
    net.all_nodes = list(net.nodes)
    net.requests.clear()

    if VERBOSE_RUN_DETAILS:
        print("--- Verificacion de Hardware ---")
        for node in net.nodes:
            num_canales = len([ch for ch in net.qchannels if node in ch.node_list])
            cap_actual = node.memory.capacity if hasattr(node, "memory") else 0
            print(f"Nodo {node.name} tiene {num_canales} canales y {cap_actual} memoria.")

    if route_alg is not None:
        net.route = route_alg
        net.build_route()

    ctrl = controller_class(
        k_max=5,
        enable_recovery_paths=enable_recovery_paths,
        max_alloc_width=None,
        swap_policy=SWAP_POLICY,
        q_swap=swap_success_prob,
        collect_validation_history=collect_validation_history,
        priority_seed=priority_seed,
    )
    attach_controller(net, ctrl)
    net.simulator = sim

    solicitudes = build_requests(net, topo_config)

    qcast_queries = route_alg is None
    for node in net.nodes:
        install_stack(
            node,
            controller=ctrl,
            qcast_queries=qcast_queries,
            forwarder_class=forwarder_class,
            swap_success_prob=swap_success_prob,
        )
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
            q_swap=swap_success_prob,
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
    tiempo_calculo_rutas = getattr(ctrl, "qcast_route_calc_time_total", None)

    counters = LinkLayerCounters.aggregate(net.nodes)
    resultados = construir_resultados_qcast(ctrl, solicitudes, ciclos_totales, counters)
    return resultados, counters, net, solicitudes, ciclos_totales, tiempo_calculo_rutas


def calcular_fidelidad_media_real(resultados, success_history=None):
    if success_history:
        fidelidades_hist = [
            float(event.get("fidelity"))
            for event in success_history
            if isinstance(event.get("fidelity"), (int, float)) and float(event.get("fidelity")) > 0
        ]
        if fidelidades_hist:
            return sum(fidelidades_hist) / len(fidelidades_hist)

    if not resultados:
        return None

    fidelidades = []
    for r in resultados:
        observed_fidelity = r.get("observed_fidelity", None)
        if observed_fidelity is not None and observed_fidelity > 0:
            fidelidades.append(float(observed_fidelity))

    return sum(fidelidades) / len(fidelidades) if fidelidades else None


def calcular_probabilidad_media_ruta_un_canal(resultados):
    if not resultados:
        return 0.0

    route_probabilities = []
    for r in resultados:
        route_probabilities.append(float(r.get("route_success_prob", 0.0)))

    return sum(route_probabilities) / len(route_probabilities)


def serializar_instrumentacion(ctrl):
    if ctrl is None:
        return {}

    return {
        "eligible_total": getattr(ctrl, "eligible_total", 0),
        "eligible_by_cycle": getattr(ctrl, "eligible_by_cycle", {}),
        "local_entanglement_total": getattr(ctrl, "local_entanglement_total", 0),
        "local_entanglement_by_cycle": getattr(ctrl, "local_entanglement_by_cycle", {}),
        "query_order_by_cycle": getattr(ctrl, "query_order_by_cycle", {}),
        "success_history": getattr(ctrl, "success_history", []),
        "e2e_candidate_history": getattr(ctrl, "e2e_candidate_history", []),
        "e2e_completion_history": getattr(ctrl, "e2e_completion_history", []),
        "swap_history": getattr(ctrl, "swap_history", []),
        "invalid_e2e_endpoint_count": getattr(ctrl, "invalid_e2e_endpoint_count", 0),
        "p4_phase_count": getattr(ctrl, "p4_phase_count", 0),
        "p4_recovery_applied": getattr(ctrl, "p4_recovery_applied", 0),
        "qchannel_activations_by_path": getattr(ctrl, "qchannel_activations_by_path", {}),
        "qchannel_activation_names_by_path": getattr(ctrl, "qchannel_activation_names_by_path", {}),
        "request_route_history": getattr(ctrl, "request_route_history", {}),
        "path_route_history": getattr(ctrl, "path_route_history", {}),
        "recovery_path_history": getattr(ctrl, "recovery_path_history", {}),
        "path_request_history": getattr(ctrl, "path_request_history", {}),
        "local_attempts_by_request": getattr(ctrl, "local_attempts_by_request", {}),
        "local_successes_by_request": getattr(ctrl, "local_successes_by_request", {}),
    }


def request_pair_key(req_id: str) -> str:
    parts = req_id.split("_")
    if len(parts) >= 3 and "TO" in parts:
        idx_to = parts.index("TO")
        return f"{parts[idx_to - 1]}_TO_{parts[idx_to + 1]}"
    return req_id


def route_cycles_by_request(
    request_route_history: dict,
    request_ids: set[str],
) -> dict[str, int]:
    return {
        req_id: sum(
            1
            for cycle_info in request_route_history.values()
            if cycle_info.get(req_id, {}).get("route")
        )
        for req_id in sorted(request_ids)
    }


def save_routing_coverage_plots(
    output_dir: str,
    algorithms: list[dict],
) -> None:
    labels = [
        LEGACY_PLOT_LABELS.get(str(item["name"]), str(item["name"]))
        for item in algorithms
    ]
    mean_routed = [
        float(item["metrics"]["avg_routed_requests_per_cycle"])
        for item in algorithms
    ]
    mean_successful = [
        float(item["metrics"]["avg_successful_requests_per_cycle"])
        for item in algorithms
    ]
    total_requests = max(
        (
            len(item["metrics"].get("route_cycles_by_request", {}))
            for item in algorithms
        ),
        default=0,
    )

    figure, axis = plt.subplots(figsize=(11, 5.5))
    positions = list(range(len(labels)))
    bar_width = 0.38
    route_bars = axis.bar(
        [position - bar_width / 2 for position in positions],
        mean_routed,
        width=bar_width,
        color="forestgreen",
        label="Solicitudes con ruta por ciclo",
    )
    success_bars = axis.bar(
        [position + bar_width / 2 for position in positions],
        mean_successful,
        width=bar_width,
        color="steelblue",
        label="Solicitudes con éxito por ciclo",
    )
    if total_requests:
        axis.axhline(
            total_requests,
            color="darkred",
            linestyle="--",
            linewidth=1.5,
            label=f"Solicitudes totales ({total_requests})",
        )
    axis.set_ylabel("Solicitudes medias por ciclo")
    axis.set_xticks(positions, labels)
    axis.grid(axis="y", linestyle="--", alpha=0.3)
    for bars in (route_bars, success_bars):
        axis.bar_label(bars, fmt="%.2f", padding=2)
    axis.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
    figure.tight_layout()
    save_graph(figure, output_dir, "04_sd_pairs_with_success")

    request_ids = sorted(
        {
            req_id
            for item in algorithms
            for req_id in item["metrics"].get("route_cycles_by_request", {})
        }
    )
    if not request_ids:
        return
    matrix = [
        [
            int(item["metrics"].get("route_cycles_by_request", {}).get(req_id, 0))
            for item in algorithms
        ]
        for req_id in request_ids
    ]
    figure, axis = plt.subplots(
        figsize=(13, max(7.0, 0.36 * len(request_ids)))
    )
    image = axis.imshow(matrix, cmap="YlGn", aspect="auto", vmin=0)
    axis.set_xticks(range(len(labels)), labels, rotation=25, ha="right")
    axis.set_yticks(
        range(len(request_ids)),
        [
            f"{index:02d} {request_pair_key(req_id).replace('_TO_', '→')}"
            for index, req_id in enumerate(request_ids)
        ],
    )
    axis.set_xlabel("Algoritmo")
    axis.set_ylabel("Solicitud")
    axis.set_title("Número de ciclos en los que cada solicitud tuvo ruta")
    colorbar = figure.colorbar(image, ax=axis)
    colorbar.set_label("Ciclos con ruta")
    max_value = max(max(row) for row in matrix)
    threshold = max_value / 2 if max_value else 0
    for row_index, row in enumerate(matrix):
        for column_index, value in enumerate(row):
            axis.text(
                column_index,
                row_index,
                str(value),
                ha="center",
                va="center",
                fontsize=7,
                color="white" if value > threshold else "black",
            )
    figure.tight_layout()
    save_graph(figure, output_dir, "06_route_cycles_by_request")


def export_analysis_json(output_dir: str, resultados_finales: dict, rutas_exportar: dict, instrumentacion_por_algoritmo: dict) -> str:
    payload = {"schema_version": "2.0", "algorithms": []}

    for nombre, metrics in resultados_finales.items():
        payload["algorithms"].append(
            {
                "name": nombre,
                "metrics": metrics,
                "routes": rutas_exportar.get(nombre, []),
                "successful_req_ids_by_pair": [
                    {
                        "base_req": item.get("base_req"),
                        "successful_req_ids": item.get("successful_req_ids", []),
                        "exitos_conseguidos": item.get("exitos_conseguidos", 0),
                    }
                    for item in rutas_exportar.get(nombre, [])
                ],
                "instrumentation": instrumentacion_por_algoritmo.get(nombre, {}),
                "route_calc_time_seconds": rutas_exportar.get(f"{nombre}_tiempo_calculo_rutas_segundos"),
            }
        )

    filename = os.path.join(output_dir, "analysis_results.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4, ensure_ascii=False)

    return filename


LEGACY_PLOT_LABELS = {
    "Dijkstra Clásico": "Dijkstra\nsaltos",
    "Dijkstra Distancia": "Dijkstra\ndistancia",
    "Dijkstra Capacidad Reserva": "Dijkstra\nsaltos\nmulticircuito",
    "Dijkstra Distancia Reserva": "Dijkstra\ndistancia\nmulticircuito",
    "Q-CAST": "Q-CAST",
    "Q-CAST Varios Entrelazamientos": "Q-CAST\nmultientrelazamiento",
}


def main():
    global SWAP_POLICY
    args = parse_args()
    SWAP_POLICY = args.swap_policy
    if not (0 < float(args.request_fraction) <= 1):
        raise ValueError("--request-fraction debe estar en el rango (0, 1]")
    if int(args.channels_per_link) < 1:
        raise ValueError("--channels-per-link debe ser al menos 1")
    if not 0.0 <= float(args.swap_success_prob) <= 1.0:
        raise ValueError("--swap-success-prob debe estar en el intervalo [0, 1]")

    log.set_default_level(args.log_level)

    if args.output_dir:
        sim_folder = os.path.abspath(args.output_dir)
        os.makedirs(sim_folder, exist_ok=False)
    else:
        sim_folder = create_simulation_folder()
    effective_scenario_path = os.path.join(sim_folder, "scenario_effective.json")
    if args.scenario_mode == "as-is":
        shutil.copyfile(args.scenario, effective_scenario_path)
    else:
        build_bad_link_scenario(args.scenario, effective_scenario_path, args)
    with open(effective_scenario_path, "r", encoding="utf-8") as scenario_file:
        generated_scenario = json.load(scenario_file)
    generated_links = generated_scenario.get("enlaces", [])
    mean_configured_link_success_prob = (
        sum(configured_link_success_probability(edge) for edge in generated_links)
        / len(generated_links)
        if generated_links
        else 0.0
    )

    print("Escenario base:", args.scenario)
    print("Modo de escenario:", args.scenario_mode)
    print("Escenario efectivo:", effective_scenario_path)
    print("Fraccion de solicitudes:", args.request_fraction)

    sims = [
        ("Dijkstra Clásico", QCastController, DijkstraRouteAlgorithm(), True, False, None),
        ("Dijkstra Distancia", QCastController, DijkstraDistanceRouteAlgorithm(), True, False, None),
        ("Dijkstra Capacidad Reserva", QCastController, DijkstraRouteAlgorithm(), True, True, None),
        ("Dijkstra Distancia Reserva", QCastController, DijkstraDistanceRouteAlgorithm(), True, True, None),
        ("Q-CAST", QCastController, None, False, False, None),
        ("Q-CAST Varios Entrelazamientos", QCastMultiEntController, None, False, False, None),
    ]

    resultados_finales = {}
    rutas_exportar = {}
    ultima_net = None
    instrumentacion_por_algoritmo = {}
    last_solicitudes = []

    for nombre, ctrl_class, route_alg, use_cap, reserve_all, fw_class in sims:
        rng.reseed(args.simulation_seed)
        resultados, counters, net, solicitudes, intentos_reales, tiempo_calculo_rutas = ejecutar_simulacion(
            nombre,
            ctrl_class,
            effective_scenario_path,
            args.sim_time,
            route_alg,
            use_cap,
            reserve_all,
            forwarder_class=fw_class,
            swap_success_prob=args.swap_success_prob,
            priority_seed=args.priority_seed,
        )
        last_solicitudes = solicitudes
        ultima_net = net

        ctrl = getattr(net, "controller", None)
        instrumentacion = serializar_instrumentacion(ctrl)
        instrumentacion_por_algoritmo[nombre] = instrumentacion

        grouped: dict[str, dict] = {}
        for r in resultados:
            req_id = r.get("req_id", "Desconocido")
            base_key = request_pair_key(req_id)

            camino = r.get("path") or r.get("route") or []
            successes = r.get("successes", 0)
            info_ruta = ctrl.request_route_info.get(req_id, {}) if ctrl else {}
            metrica_eda = info_ruta.get("metric", 0.0)
            w_usado = info_ruta.get("width", info_ruta.get("w_asignado", 1))
            capacidad_final = info_ruta.get("capacidad_residual_final", {})

            recovery_paths_formateados = []
            if ctrl and hasattr(ctrl, "recovery_paths_info") and hasattr(ctrl, "path_requests"):
                for p_id, req_list in ctrl.path_requests.items():
                    if req_id in req_list:
                        desvios = ctrl.recovery_paths_info.get(p_id, [])
                        for desvio in desvios:
                            recovery_paths_formateados.append(
                                f"Fallo en {desvio['segment_src']}-{desvio['segment_dst']} -> Usar desvio: {desvio['route']} (Metrica: {desvio['metric']:.2f})"
                            )

            entry = grouped.get(base_key)
            if entry is None:
                grouped[base_key] = {
                    "base_req": base_key,
                    "examples": [req_id],
                    "ruta_asignada": camino,
                    "metrica_eda_sum": metrica_eda,
                    "metrica_count": 1 if metrica_eda != 0.0 else 0,
                    "w_asignado": w_usado,
                    "capacidad_final": capacidad_final,
                    "rutas_recuperacion": recovery_paths_formateados,
                    "exitos_conseguidos": successes,
                    "successful_req_ids": [req_id] if successes > 0 else [],
                    "total_reqs": 1,
                }
            else:
                entry["examples"].append(req_id)
                if not entry["ruta_asignada"] and camino:
                    entry["ruta_asignada"] = camino
                if metrica_eda != 0.0:
                    entry["metrica_eda_sum"] += metrica_eda
                    entry["metrica_count"] += 1
                entry["exitos_conseguidos"] += successes
                if successes > 0:
                    entry["successful_req_ids"].append(req_id)
                entry["total_reqs"] += 1

        lista_rutas_agrupada = []
        for _k, v in grouped.items():
            avg_metric = v["metrica_eda_sum"] / v["metrica_count"] if v["metrica_count"] > 0 else 0.0
            lista_rutas_agrupada.append(
                {
                    "base_req": v["base_req"],
                    "example_req_ids": v["examples"],
                    "ruta_asignada": v["ruta_asignada"],
                    "metrica_eda_promedio": avg_metric,
                    "w_asignado": v["w_asignado"],
                    "capacidad_final": v["capacidad_final"],
                    "rutas_recuperacion": v["rutas_recuperacion"],
                    "exitos_conseguidos": v["exitos_conseguidos"],
                    "successful_req_ids": v.get("successful_req_ids", []),
                    "total_reqs": v["total_reqs"],
                }
            )

        pares_sd_con_exito = sum(1 for v in lista_rutas_agrupada if v["exitos_conseguidos"] > 0)
        pares_sd_con_ruta = sum(1 for v in lista_rutas_agrupada if v["ruta_asignada"])
        rutas_exportar[nombre] = lista_rutas_agrupada
        rutas_exportar[f"{nombre}_instrumentacion"] = instrumentacion
        rutas_exportar[f"{nombre}_tiempo_calculo_rutas_segundos"] = tiempo_calculo_rutas

        total_exitos = sum(r.get("successes", 0) for r in resultados)
        total_cycles = int(args.sim_time / TOTAL_CYCLE_TIME) if args.sim_time > 0 else 0
        throughput = total_exitos / args.sim_time
        request_ids = {str(req["req_id"]) for req in solicitudes}
        request_route_history = getattr(ctrl, "request_route_history", {})
        routed_cycles = route_cycles_by_request(
            request_route_history,
            request_ids,
        )
        admitted_pair_cycles = {
            (int(cycle), request_pair_key(str(req_id)))
            for cycle, cycle_info in request_route_history.items()
            for req_id, info in cycle_info.items()
            if info.get("route")
        }
        admitted_pairs = {pair for _, pair in admitted_pair_cycles}
        success_history = instrumentacion.get("success_history", [])
        successful_pair_cycles = {
            (int(event["cycle"]), request_pair_key(str(event["req_id"])))
            for event in success_history
            if event.get("cycle") is not None and event.get("req_id") is not None
        }
        successful_admitted_pair_cycles = len(
            successful_pair_cycles & admitted_pair_cycles
        )
        avg_successful_pairs_per_cycle = (
            successful_admitted_pair_cycles / max(1, total_cycles)
        )
        admitted_pair_cycle_success_rate = (
            successful_admitted_pair_cycles / len(admitted_pair_cycles)
            if admitted_pair_cycles
            else 0.0
        )
        admitted_pairs_ever_success_rate = (
            pares_sd_con_exito / len(admitted_pairs)
            if admitted_pairs
            else 0.0
        )
        redundant_entries = [
            item
            for item in lista_rutas_agrupada
            if item["ruta_asignada"] and int(item.get("w_asignado", 1)) > 1
        ]
        redundant_pairs_with_success = sum(
            1 for item in redundant_entries if item["exitos_conseguidos"] > 0
        )
        redundant_pairs_ever_success_rate = (
            redundant_pairs_with_success / len(redundant_entries)
            if redundant_entries
            else None
        )
        
        total_attempts = sum(int(r.get("attempts", 0)) for r in resultados)
        app_level_success_prob = compute_request_success_rate_by_cycle(
            success_history,
            request_ids,
            total_cycles,
        )
        avg_per_pair_success_prob = compute_pair_success_rate_by_cycle(
            success_history,
            request_ids,
            total_cycles,
        )
        link_attempt_efficiency = total_exitos / total_attempts if total_attempts > 0 else 0.0
        avg_successful_requests_per_cycle = (
            len({
                (int(event["cycle"]), str(event["req_id"]))
                for event in success_history
                if event.get("cycle") is not None and event.get("req_id") is not None
            }) / max(1, total_cycles)
        )
        avg_e2e_deliveries_per_cycle = total_exitos / max(1, total_cycles)
        
        mean_single_channel_route_success_prob = calcular_probabilidad_media_ruta_un_canal(
            resultados
        )

        resultados_finales[nombre] = {
            "throughput": throughput,
            "app_level_success_prob": app_level_success_prob,
            "avg_per_pair_success_prob": avg_per_pair_success_prob,
            "link_attempt_efficiency": link_attempt_efficiency,
            "mean_single_channel_route_success_prob": mean_single_channel_route_success_prob,
            "mean_configured_link_success_prob": mean_configured_link_success_prob,
            "n_etg": counters.n_etg,
            "n_attempts": counters.n_attempts,
            "n_success_attempts": total_exitos,
            "fidelity": calcular_fidelidad_media_real(resultados, instrumentacion.get("success_history", [])),
            "sd_pairs_with_route": pares_sd_con_ruta,
            "sd_pairs_with_success": pares_sd_con_exito,
            "admitted_pairs_ever_success_rate": admitted_pairs_ever_success_rate,
            "admitted_pair_cycle_success_rate": admitted_pair_cycle_success_rate,
            "sd_pairs_with_redundancy": len(redundant_entries),
            "redundant_pairs_with_success": redundant_pairs_with_success,
            "redundant_pairs_ever_success_rate": redundant_pairs_ever_success_rate,
            "avg_successful_requests_per_cycle": avg_successful_requests_per_cycle,
            "avg_successful_pairs_per_cycle": avg_successful_pairs_per_cycle,
            "avg_e2e_deliveries_per_cycle": avg_e2e_deliveries_per_cycle,
            "avg_admitted_pairs_per_cycle": (
                len(admitted_pair_cycles) / max(1, total_cycles)
            ),
            "avg_routed_requests_per_cycle": (
                sum(routed_cycles.values()) / max(1, total_cycles)
            ),
            "route_cycles_by_request": routed_cycles,
        }

    print("\n========================================")
    print("METRICAS GLOBALES (TODOS LOS ALGORITMOS)")
    print("========================================")
    for nombre, data in resultados_finales.items():
        print(f"{nombre}:")
        print(f"  - Throughput: {data['throughput']:.4f} EPS")
        print(f"  - Probabilidad de exito a nivel de aplicacion (correcta): {data['app_level_success_prob']:.4f}")
        print(f"  - Probabilidad de exito promedio por pareja (robusta): {data['avg_per_pair_success_prob']:.4f}")
        print(f"  - Eficiencia E2E por intento elemental: {data['link_attempt_efficiency']:.4f}")
        print(
            "  - Probabilidad estimada media de ruta (un canal): "
            f"{data['mean_single_channel_route_success_prob']:.4f}"
        )
        print(f"  - Probabilidad media configurada de los enlaces: {data['mean_configured_link_success_prob']:.4f}")
        print(f"  - Peticiones finales completadas (App): {data['n_success_attempts']}")
        print(
            f"  - Fidelidad real media observada: {data['fidelity']:.4f}"
            if data["fidelity"] is not None
            else "  - Fidelidad real media observada: N/D (sin entregas)"
        )
        print(f"  - Parejas S-D con ruta: {data['sd_pairs_with_route']}")
        print(f"  - Parejas admitidas con algun exito: {data['admitted_pairs_ever_success_rate']:.4f}")
        print(f"  - Tasa de exito por ciclo entre parejas admitidas: {data['admitted_pair_cycle_success_rate']:.4f}")
        if data["redundant_pairs_ever_success_rate"] is not None:
            print(
                "  - Parejas redundantes con algun exito: "
                f"{data['redundant_pairs_with_success']}/{data['sd_pairs_with_redundancy']} "
                f"({data['redundant_pairs_ever_success_rate']:.4f})"
            )
        print(f"  - Solicitudes distintas con exito por ciclo (media): {data['avg_successful_requests_per_cycle']:.4f}")
        print(f"  - Parejas S-D con exito por ciclo (media): {data['avg_successful_pairs_per_cycle']:.4f}")
        print(f"  - Entrelazamientos E2E entregados por ciclo (media): {data['avg_e2e_deliveries_per_cycle']:.4f}")
        print(f"  - Parejas S-D con exito (total acumulado): {data['sd_pairs_with_success']}")

    if ultima_net is not None:
        ctrl = getattr(ultima_net, "controller", None)
        if ctrl is not None:
            print("\n========================================")
            print("INSTRUMENTACION")
            print("========================================")
            print(f"Canales activados por path_id: {getattr(ctrl, 'qchannel_activations_by_path', {})}")
            print(f"Canales activados por path_id (nombres): {getattr(ctrl, 'qchannel_activation_names_by_path', {})}")
            print(f"Qubits que llegan a ELIGIBLE: {getattr(ctrl, 'eligible_total', 0)}")
            print(f"Qubits ELIGIBLE por ciclo: {getattr(ctrl, 'eligible_by_cycle', {})}")
            print(f"Entradas a P4 de recuperacion: {getattr(ctrl, 'p4_phase_count', 0)}")
            print(f"Recuperaciones P4 aplicadas: {getattr(ctrl, 'p4_recovery_applied', 0)}")

    algoritmos = [
        "Dijkstra\nsaltos",
        "Dijkstra\ndistancia",
        "Dijkstra\nsaltos\nmulticircuito",
        "Dijkstra\ndistancia\nmulticircuito",
        "Q-CAST",
        "Q-CAST\nmultientrelazamiento",
    ]

    throughputs = [resultados_finales[n]["throughput"] for n, _, _, _, _, _ in sims]
    avg_fidelities = [
        resultados_finales[n]["fidelity"]
        for n, _, _, _, _, _ in sims
    ]
    plotted_fidelities = [
        fidelity if fidelity is not None else 0.0
        for fidelity in avg_fidelities
    ]
    fig = plt.figure(figsize=(10, 5))
    bars = plt.bar(algoritmos, throughputs, color="forestgreen")
    plt.ylabel("Throughput [EPS]")
    plt.grid(axis="y", linestyle="--", alpha=0.3)
    for bar, value in zip(bars, throughputs):
        plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
    plt.tight_layout()
    save_graph(fig, sim_folder, "01_throughput_global")

    fig = plt.figure(figsize=(10, 5))
    bars = plt.bar(algoritmos, plotted_fidelities, color="forestgreen")
    plt.ylabel("Fidelidad media observada")
    max_fid = max(plotted_fidelities)
    plt.ylim(0, max_fid * 1.15 if max_fid > 0 else 1.0)
    plt.grid(axis="y", linestyle="--", alpha=0.3)
    for bar, value in zip(bars, avg_fidelities):
        label = f"{value:.4f}" if value is not None else "N/D"
        plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), label, ha="center", va="bottom")
    plt.tight_layout()
    save_graph(fig, sim_folder, "03_average_fidelity")

    rutas_json_path = os.path.join(sim_folder, "rutas_asignadas.json")
    with open(rutas_json_path, "w", encoding="utf-8") as f:
        json.dump(rutas_exportar, f, indent=4, ensure_ascii=False)
    print("  Rutas guardadas: rutas_asignadas.json")

    analysis_path = export_analysis_json(
        sim_folder,
        resultados_finales,
        rutas_exportar,
        instrumentacion_por_algoritmo,
    )
    print("  Analisis guardado: analysis_results.json")
    with open(analysis_path, "r", encoding="utf-8") as analysis_file:
        save_routing_coverage_plots(
            sim_folder,
            json.load(analysis_file)["algorithms"],
        )

    if ultima_net:
        print("\nGuardando topologia e informacion de enlaces...")
        save_topology_diagram(ultima_net, sim_folder, "00_topology_diagram")
        save_link_metadata(ultima_net, sim_folder, "05_link_metadata")

    print_simulation_summary(sim_folder)
    print("Simulaciones completadas.")


if __name__ == "__main__":
    main()
