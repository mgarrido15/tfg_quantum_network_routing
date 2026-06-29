import json
import math
import os
import statistics
import sys
from dataclasses import dataclass, asdict

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mqns.entity.memory.memory import QuantumMemory
from mqns.network.fw.routing import RoutingPathStatic
from mqns.network.network.network import QuantumNetwork, dibujar_escenario
from mqns.network.network.reporting import (
    build_request_id,
    construir_resultados_qcast,
    obtener_prob_y_fidelidad_de_ruta,
)
from mqns.network.network.timing import TimingModeSyncQCast
from mqns.network.protocol.link_layer import LinkLayer, LinkLayerCounters
from mqns.network.qcast.controller import QCastController
from mqns.network.qcast.forwarder import QCastForwarder
from mqns.network.route import (
    DijkstraDistanceRouteAlgorithm,
    DijkstraRouteAlgorithm,
    assign_dijkstra_routes_with_capacity,
    assign_dijkstra_routes_with_capacity_reserve_all,
)
from mqns.simulator import Simulator
from mqns.utils import log
from mqns.utils.random import rng


LIMIT_VAL = 100.0
SCENARIO_PATH = os.path.join(os.path.dirname(__file__), "..", "escenario_basico.json")
REQUEST_REPEAT = 1
N_REPETITIONS = 20
SEED_BASE = 12345
MEMORY_T_COHERE = 10.0

T_PHASE = 1.0
TOTAL_CYCLE_TIME = T_PHASE * 4

log.set_default_level("WARN")


@dataclass
class RunStats:
    throughput: float
    successes: int
    local_attempts: int
    local_successes: int
    local_failures: int
    app_success_prob: float
    physical_success_prob: float
    fidelity: float
    eligible_total: int
    p4_phase_count: int
    p4_recovery_applied: int


class StaticQCastForwarder(QCastForwarder):
    """Forwarder for static route experiments that does not send Q-CAST queries."""

    def _send_initial_queries(self):
        return


def install_stack(node, controller=None, qcast_queries=True):
    if not hasattr(node, "memory"):
        mem = QuantumMemory(name=f"mem_{node.name}", capacity=100, t_cohere=MEMORY_T_COHERE)
        node.memory = mem
        mem.node = node
    else:
        mem = node.memory
        if hasattr(mem, "_t_cohere"):
            mem._t_cohere = MEMORY_T_COHERE

    link_layer = LinkLayer()
    if qcast_queries:
        forwarder = QCastForwarder(k_max=2, ps=1.0, purif_enabled=False, swapping_enabled=True)
    else:
        forwarder = StaticQCastForwarder(k_max=2, ps=1.0, purif_enabled=False, swapping_enabled=True)

    if controller:
        forwarder.controller = controller

    node.add_apps([link_layer, forwarder])
    setattr(node, "forwarder", forwarder)
    return forwarder


def _build_requests(net, topo_config):
    solicitudes = []
    base_reqs = topo_config.get("solicitudes", [])
    idx = 0
    for _ in range(REQUEST_REPEAT):
        for req in base_reqs:
            src = net.get_node(req["src"])
            dst = net.get_node(req["dst"])
            if src and dst:
                req_id = build_request_id(src.name, dst.name, idx)
                net.add_request(src, dst, {"req_id": req_id})
                solicitudes.append({"req_id": req_id, "src": src, "dst": dst})
                idx += 1
    return solicitudes


def _attach_controller(net, ctrl):
    setattr(net, "controller", ctrl)
    setattr(ctrl, "net", net)
    if net.nodes:
        net.nodes[0].add_apps(ctrl)


def _install_static_route_on_forwarders(net, ctrl, route, req_id):
    path_id = ctrl.next_path_id
    ctrl.next_path_id += 1
    width = ctrl.request_route_info.get(req_id, {}).get("width", 1)
    ctrl.path_w[path_id] = int(width)
    ctrl.path_requests[path_id] = [req_id]
    if width > 1:
        m_v = [(int(width), int(width)) for _ in range(max(0, len(route) - 1))]
        route_path = RoutingPathStatic(route, req_id=req_id, path_id=path_id, m_v=m_v)
    else:
        route_path = RoutingPathStatic(route, req_id=req_id, path_id=path_id)

    instructions = next(route_path.compute_paths(net))
    install_msg = {"cmd": "INSTALL_PATH", "path_id": path_id, "instructions": instructions}
    for node_name in route:
        qnode = net.get_node(node_name)
        if hasattr(qnode, "forwarder"):
            qnode.forwarder.handle_classic_packet(qnode, install_msg)


def calcular_fidelidad_media_real(resultados):
    if not resultados:
        return 0.0
    fidelidades = []
    for r in resultados:
        observed_fidelity = r.get("observed_fidelity", None)
        if observed_fidelity is not None and observed_fidelity > 0:
            fidelidades.append(float(observed_fidelity))
            continue
        fidelidades.append(float(r.get("route_fidelity", 0.0)))
    return sum(fidelidades) / len(fidelidades) if fidelidades else 0.0


def ejecutar_simulacion(nombre, controller_class, route_alg=None, use_capacity=True, reserve_all_capacity=False):
    with open(SCENARIO_PATH, "r", encoding="utf-8") as f:
        topo_config = json.load(f)

    sim = Simulator(0, LIMIT_VAL, accuracy=1000000)
    net = QuantumNetwork(None)
    net.build_topology_from_json(SCENARIO_PATH)
    net.all_nodes = list(net.nodes)
    net.requests.clear()

    if route_alg is not None:
        net.route = route_alg
        net.build_route()

    ctrl = controller_class(k_max=5)
    _attach_controller(net, ctrl)
    net.simulator = sim
    solicitudes = _build_requests(net, topo_config)

    topo_config["t_cohere"] = 10
    qcast_queries = route_alg is None
    for node in net.nodes:
        install_stack(node, controller=ctrl, qcast_queries=qcast_queries)
        node.install(sim)

    net.timing = TimingModeSyncQCast(t1=10, t2=10, t3=10, t4=10)
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
            _install_static_route_on_forwarders(net, ctrl, route, req_id)

    ciclos_totales = int(LIMIT_VAL / TOTAL_CYCLE_TIME)
    sim.run()
    resultados = construir_resultados_qcast(ctrl, solicitudes, ciclos_totales)
    counters = LinkLayerCounters.aggregate(net.nodes)
    return resultados, counters, net, solicitudes, ciclos_totales, ctrl


def _summarize_run(resultados, counters, ctrl):
    total_successes = sum(r.get("successes", 0) for r in resultados)
    local_attempts = int(getattr(ctrl, "local_entanglement_total", 0))
    local_successes = sum(
        cycle.get("successes", 0)
        for cycle in getattr(ctrl, "local_entanglement_by_cycle", {}).values()
    )
    local_failures = sum(
        cycle.get("failures", 0)
        for cycle in getattr(ctrl, "local_entanglement_by_cycle", {}).values()
    )

    return RunStats(
        throughput=total_successes / LIMIT_VAL,
        successes=total_successes,
        local_attempts=local_attempts,
        local_successes=local_successes,
        local_failures=local_failures,
        app_success_prob=total_successes / 2500.0,
        physical_success_prob=(local_successes / local_attempts) if local_attempts else 0.0,
        fidelity=calcular_fidelidad_media_real(resultados),
        eligible_total=int(getattr(ctrl, "eligible_total", 0)),
        p4_phase_count=int(getattr(ctrl, "p4_phase_count", 0)),
        p4_recovery_applied=int(getattr(ctrl, "p4_recovery_applied", 0)),
    )


def _mean_std(values):
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return float(values[0]), 0.0
    return float(statistics.fmean(values)), float(statistics.stdev(values))


def _print_summary(name, stats_list):
    throughputs = [s.throughput for s in stats_list]
    attempts = [s.local_attempts for s in stats_list]
    successes = [s.successes for s in stats_list]
    app_probs = [s.app_success_prob for s in stats_list]
    phys_probs = [s.physical_success_prob for s in stats_list]
    fidelities = [s.fidelity for s in stats_list]

    mean_thr, std_thr = _mean_std(throughputs)
    mean_att, std_att = _mean_std(attempts)
    mean_succ, std_succ = _mean_std(successes)
    mean_app, std_app = _mean_std(app_probs)
    mean_phys, std_phys = _mean_std(phys_probs)
    mean_fid, std_fid = _mean_std(fidelities)

    print(f"\n{name}")
    print(f"  Throughput medio: {mean_thr:.6f} EPS ± {std_thr:.6f}")
    print(f"  Exitos medios: {mean_succ:.2f} ± {std_succ:.2f}")
    print(f"  Intentos físicos medios: {mean_att:.2f} ± {std_att:.2f}")
    print(f"  Prob. éxito app media: {mean_app:.6f} ± {std_app:.6f}")
    print(f"  Prob. éxito física media: {mean_phys:.6f} ± {std_phys:.6f}")
    print(f"  Fidelidad media: {mean_fid:.6f} ± {std_fid:.6f}")


def _save_bar_plot(output_dir, results_by_algo):
    algorithms = list(results_by_algo.keys())
    means = [results_by_algo[a]["throughput_mean"] for a in algorithms]
    stds = [results_by_algo[a]["throughput_std"] for a in algorithms]

    plt.figure(figsize=(10, 5))
    bars = plt.bar(algorithms, means, yerr=stds, capsize=5, color="forestgreen")
    plt.ylabel("Throughput [EPS]")
    plt.title("Throughput medio con desviación estándar")
    plt.grid(axis="y", linestyle="--", alpha=0.3)
    for bar, value in zip(bars, means):
        plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "throughput_convergence.png"), dpi=300)
    plt.close()


def main():
    sims = [
        ("Dijkstra Clásico", QCastController, DijkstraRouteAlgorithm(), True, False),
        ("Dijkstra Distancia", QCastController, DijkstraDistanceRouteAlgorithm(), True, False),
        ("Dijkstra Capacidad Reserva", QCastController, DijkstraRouteAlgorithm(), True, True),
        ("Dijkstra Distancia Reserva", QCastController, DijkstraDistanceRouteAlgorithm(), True, True),
        ("Q-CAST", QCastController, None, False, False),
    ]

    summary: dict[str, dict] = {}
    raw_runs: dict[str, list[dict]] = {}

    for algo_index, (nombre, ctrl_class, route_alg, use_cap, reserve_all) in enumerate(sims):
        algo_runs = []
        for rep in range(N_REPETITIONS):
            rng.reseed(SEED_BASE + algo_index * 1000 + rep)
            resultados, counters, net, solicitudes, intentos_reales, ctrl = ejecutar_simulacion(
                nombre,
                ctrl_class,
                route_alg,
                use_cap,
                reserve_all,
            )
            stats = _summarize_run(resultados, counters, ctrl)
            algo_runs.append({
                "repetition": rep,
                **asdict(stats),
                "n_etg": int(getattr(counters, "n_etg", 0)),
                "n_attempts": int(getattr(counters, "n_attempts", 0)),
                "n_success_attempts": stats.successes,
            })

            if rep == 0 and nombre == "Q-CAST":
                print("\nInstrumentación Q-CAST de la primera repetición:")
                print(f"  Qubits que llegan a ELIGIBLE: {stats.eligible_total}")
                print(f"  Entradas a P4: {stats.p4_phase_count}")
                print(f"  Recuperaciones P4 aplicadas: {stats.p4_recovery_applied}")

        raw_runs[nombre] = algo_runs
        stats_objects = [RunStats(**{k: v for k, v in r.items() if k in RunStats.__annotations__}) for r in algo_runs]
        _print_summary(nombre, stats_objects)

        summary[nombre] = {
            "throughput_mean": _mean_std([r["throughput"] for r in algo_runs])[0],
            "throughput_std": _mean_std([r["throughput"] for r in algo_runs])[1],
            "attempts_mean": _mean_std([r["local_attempts"] for r in algo_runs])[0],
            "attempts_std": _mean_std([r["local_attempts"] for r in algo_runs])[1],
            "successes_mean": _mean_std([r["successes"] for r in algo_runs])[0],
            "successes_std": _mean_std([r["successes"] for r in algo_runs])[1],
            "physical_success_prob_mean": _mean_std([r["physical_success_prob"] for r in algo_runs])[0],
            "physical_success_prob_std": _mean_std([r["physical_success_prob"] for r in algo_runs])[1],
            "app_success_prob_mean": _mean_std([r["app_success_prob"] for r in algo_runs])[0],
            "app_success_prob_std": _mean_std([r["app_success_prob"] for r in algo_runs])[1],
            "fidelity_mean": _mean_std([r["fidelity"] for r in algo_runs])[0],
            "fidelity_std": _mean_std([r["fidelity"] for r in algo_runs])[1],
        }

    output_dir = os.path.join(os.path.dirname(__file__), "..", "outputs")
    os.makedirs(output_dir, exist_ok=True)

    _save_bar_plot(output_dir, summary)

    summary_path = os.path.join(output_dir, "comparison_algorithms_convergence.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "runs": raw_runs}, f, indent=2, ensure_ascii=False)

    print(f"\nResumen guardado en: {summary_path}")
    print(f"Gráfica guardada en: {os.path.join(output_dir, 'throughput_convergence.png')}")


if __name__ == "__main__":
    main()