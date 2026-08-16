import argparse
import json
import math
import os
import random
import sys
import time

import matplotlib.pyplot as plt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mqns.simulator import Simulator
from mqns.network.network.timing import TimingModeSyncQCast
from mqns.network.network.network import QuantumNetwork, dibujar_escenario
from mqns.network.network.reporting import (
    build_request_id,
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
from mqns.network.qcast.forwarder import QCastForwarder, QCastMultiEntForwarder
from mqns.network.protocol.link_layer import LinkLayer, LinkLayerCounters
from mqns.utils import log
from mqns.entity.memory.memory import QuantumMemory
from simulation_utils import (
    create_simulation_folder,
    save_graph,
    save_topology_diagram,
    save_link_metadata,
    print_simulation_summary,
)


DEFAULT_SCENARIO_PATH = os.path.join(os.path.dirname(__file__), "..", "escenario_grande_multicanal_w.json")
DEFAULT_SIM_TIME = 1000.0
REQUEST_REPEAT = 1
MEMORY_T_COHERE = 10.0

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
        "--sim-time",
        type=float,
        default=DEFAULT_SIM_TIME,
        help="Tiempo total de simulacion",
    )
    parser.add_argument(
        "--prob-scale",
        type=float,
        default=0.22,
        help="Factor de degradacion para probabilidad de exito de enlace",
    )
    parser.add_argument(
        "--fidelity-scale",
        type=float,
        default=0.70,
        help="Factor de degradacion para fidelidad de enlace",
    )
    parser.add_argument(
        "--min-link-fidelity",
        type=float,
        default=0.80,
        help="Cota inferior de fidelidad por enlace tras degradar",
    )
    parser.add_argument(
        "--alpha-scale",
        type=float,
        default=1.8,
        help="Factor de degradacion para alpha de atenuacion",
    )
    parser.add_argument(
        "--length-scale",
        type=float,
        default=1.6,
        help="Factor de degradacion para longitud de enlaces",
    )
    parser.add_argument(
        "--min-prob",
        type=float,
        default=0.02,
        help="Cota inferior para probabilidad de enlace tras degradar",
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
    return parser.parse_args()


def build_bad_link_scenario(base_scenario_path: str, output_path: str, args: argparse.Namespace) -> str:
    with open(base_scenario_path, "r", encoding="utf-8") as f:
        topo = json.load(f)

    for edge in topo.get("enlaces", []):
        base_length = float(edge.get("length", 3.0))
        scaled_length = base_length * float(args.length_scale)
        edge["length"] = scaled_length

        base_alpha = float(edge.get("alpha", 0.2))
        scaled_alpha = base_alpha * float(args.alpha_scale)
        edge["alpha"] = scaled_alpha

        base_prob = edge.get("prob", edge.get("p_s", 1.0))
        scaled_prob = max(float(args.min_prob), float(base_prob) * float(args.prob_scale))
        edge["prob"] = scaled_prob
        edge["p_s"] = scaled_prob

        if "fidelity" in edge:
            base_fidelity = float(edge["fidelity"])
        else:
            base_fidelity = math.exp(-base_alpha * base_length)

        scaled_fidelity = max(
            float(args.min_link_fidelity),
            min(0.999, base_fidelity * float(args.fidelity_scale)),
        )
        edge["fidelity"] = scaled_fidelity

        # Keep transfer-error model aligned with the degraded fidelity when possible.
        p_survival = max(0.0, min(1.0, (4.0 * scaled_fidelity - 1.0) / 3.0))
        depolar_p = max(0.0, min(1.0, 1.0 - p_survival))
        edge["transfer_error"] = f"DEPOLAR:{depolar_p:.6f}"

    solicitudes = topo.get("solicitudes", [])
    if 0 < float(args.request_fraction) < 1 and solicitudes:
        sample_size = max(1, int(round(len(solicitudes) * float(args.request_fraction))))
        rng = random.Random(args.request_seed)
        topo["solicitudes"] = rng.sample(solicitudes, sample_size)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(topo, f, indent=2, ensure_ascii=False)

    return output_path


class StaticQCastForwarder(QCastForwarder):
    def _send_initial_queries(self):
        return


def install_stack(node, controller=None, qcast_queries=True, forwarder_class=None):
    if not hasattr(node, "memory"):
        mem = QuantumMemory(name=f"mem_{node.name}", capacity=100, t_cohere=MEMORY_T_COHERE)
        node.memory = mem
    mem = node.memory
    if hasattr(mem, "_t_cohere"):
        mem._t_cohere = MEMORY_T_COHERE

    # Use per-link error/fidelity models from topology instead of a fixed 0.99 seed.
    link_layer = LinkLayer(init_fidelity=None)
    if forwarder_class is not None:
        forwarder = forwarder_class(k_max=2, ps=1.0, purif_enabled=False, swapping_enabled=True)
    elif qcast_queries:
        forwarder = QCastForwarder(k_max=2, ps=1.0, purif_enabled=False, swapping_enabled=True)
    else:
        forwarder = StaticQCastForwarder(k_max=2, ps=1.0, purif_enabled=False, swapping_enabled=True)

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


def ejecutar_simulacion(
    nombre,
    controller_class,
    scenario_path,
    sim_time,
    route_alg=None,
    use_capacity=True,
    reserve_all_capacity=False,
    forwarder_class=None,
):
    print(f"\n--- Ejecutando: {nombre} ---")

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

    # Logica original: sin cap de ancho adicional para Q-CAST.
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

    tiempo_calculo_rutas = None
    if route_alg is not None:
        inicio_calculo_rutas = time.perf_counter()
        net.route = route_alg
        net.build_route()
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
        tiempo_calculo_rutas = time.perf_counter() - inicio_calculo_rutas

        for req in solicitudes:
            req_id = req["req_id"]
            info = ctrl.request_route_info.get(req_id)
            if not info:
                continue
            route = info.get("route")
            if not route:
                continue
            install_static_route_on_forwarders(net, ctrl, route, req_id)

    ciclos_totales = int(sim_time / TOTAL_CYCLE_TIME)
    sim.run()
    if tiempo_calculo_rutas is None and ctrl is not None:
        tiempo_calculo_rutas = getattr(ctrl, "qcast_route_calc_time_total", None)

    resultados = construir_resultados_qcast(ctrl, solicitudes, ciclos_totales)
    counters = LinkLayerCounters.aggregate(net.nodes)
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
        return 0.0

    fidelidades = []
    for r in resultados:
        observed_fidelity = r.get("observed_fidelity", None)
        if observed_fidelity is not None and observed_fidelity > 0:
            fidelidades.append(float(observed_fidelity))

    return sum(fidelidades) / len(fidelidades) if fidelidades else 0.0


def _obtener_orden_pares_qcast(rutas_exportar, algoritmos):
    pares_ordenados = []
    vistos = set()

    if "Q-CAST" in rutas_exportar:
        for entrada in rutas_exportar.get("Q-CAST", []):
            base_req = entrada.get("base_req")
            if base_req and base_req not in vistos:
                pares_ordenados.append(base_req)
                vistos.add(base_req)

    for nombre in algoritmos:
        for entrada in rutas_exportar.get(nombre, []):
            base_req = entrada.get("base_req")
            if base_req and base_req not in vistos:
                pares_ordenados.append(base_req)
                vistos.add(base_req)

    return pares_ordenados


def _construir_matriz_probabilidad_pares(rutas_exportar, algoritmos, orden_pares, ciclos_por_algoritmo):
    matriz = []

    for nombre in algoritmos:
        entradas_por_par = {entrada.get("base_req"): entrada for entrada in rutas_exportar.get(nombre, [])}
        ciclos = max(1, int(ciclos_por_algoritmo.get(nombre, 1)))
        fila = []

        for base_req in orden_pares:
            entrada = entradas_por_par.get(base_req)
            if not entrada:
                fila.append(0.0)
                continue

            total_reqs = max(1, int(entrada.get("total_reqs", 1)))
            exitos = float(entrada.get("exitos_conseguidos", 0))
            fila.append(exitos / float(total_reqs * ciclos))

        matriz.append(fila)

    return matriz


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
        "p4_phase_count": getattr(ctrl, "p4_phase_count", 0),
        "p4_recovery_applied": getattr(ctrl, "p4_recovery_applied", 0),
        "qchannel_activations_by_path": getattr(ctrl, "qchannel_activations_by_path", {}),
        "qchannel_activation_names_by_path": getattr(ctrl, "qchannel_activation_names_by_path", {}),
    }


def export_analysis_json(output_dir: str, resultados_finales: dict, rutas_exportar: dict, instrumentacion_por_algoritmo: dict) -> str:
    payload = {"algorithms": []}

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


def main():
    args = parse_args()
    if not (0 < float(args.request_fraction) <= 1):
        raise ValueError("--request-fraction debe estar en el rango (0, 1]")

    log.set_default_level(args.log_level)

    sim_folder = create_simulation_folder()
    bad_scenario_path = os.path.join(sim_folder, "scenario_bad_links.json")
    build_bad_link_scenario(args.scenario, bad_scenario_path, args)

    print("Escenario base:", args.scenario)
    print("Escenario degradado:", bad_scenario_path)
    print("Fraccion de solicitudes:", args.request_fraction)

    sims = [
        ("Dijkstra Clásico", QCastController, DijkstraRouteAlgorithm(), True, False, None),
        ("Dijkstra Distancia", QCastController, DijkstraDistanceRouteAlgorithm(), True, False, None),
        ("Dijkstra Capacidad Reserva", QCastController, DijkstraRouteAlgorithm(), True, True, None),
        ("Dijkstra Distancia Reserva", QCastController, DijkstraDistanceRouteAlgorithm(), True, True, None),
        ("Q-CAST", QCastController, None, False, False, None),
        ("Q-CAST Varios Entrelazamientos", QCastMultiEntController, None, False, False, QCastMultiEntForwarder),
    ]

    resultados_finales = {}
    rutas_exportar = {}
    ultima_net = None
    instrumentacion_por_algoritmo = {}
    last_solicitudes = []
    ciclos_por_algoritmo = {}

    for nombre, ctrl_class, route_alg, use_cap, reserve_all, fw_class in sims:
        resultados, counters, net, solicitudes, intentos_reales, tiempo_calculo_rutas = ejecutar_simulacion(
            nombre,
            ctrl_class,
            bad_scenario_path,
            args.sim_time,
            route_alg,
            use_cap,
            reserve_all,
            forwarder_class=fw_class,
        )
        last_solicitudes = solicitudes
        ultima_net = net

        ctrl = getattr(net, "controller", None)
        instrumentacion = serializar_instrumentacion(ctrl)
        instrumentacion_por_algoritmo[nombre] = instrumentacion

        grouped: dict[str, dict] = {}
        for r in resultados:
            req_id = r.get("req_id", "Desconocido")
            parts = req_id.split("_")
            base_key = req_id
            if len(parts) >= 3 and "TO" in parts:
                try:
                    idx_to = parts.index("TO")
                    src_part = parts[idx_to - 1]
                    dst_part = parts[idx_to + 1]
                    base_key = f"{src_part}_TO_{dst_part}"
                except Exception:
                    base_key = req_id

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
        app_level_success_prob = total_exitos / max(1, len(solicitudes) * intentos_reales)
        physical_layer_success_prob = counters.n_etg / counters.n_attempts if counters.n_attempts > 0 else 0.0
        avg_successful_requests_per_cycle = total_exitos / max(1, total_cycles)

        resultados_finales[nombre] = {
            "throughput": throughput,
            "app_level_success_prob": app_level_success_prob,
            "physical_layer_success_prob": physical_layer_success_prob,
            "n_etg": counters.n_etg,
            "n_attempts": counters.n_attempts,
            "n_success_attempts": total_exitos,
            "fidelity": calcular_fidelidad_media_real(resultados, instrumentacion.get("success_history", [])),
            "sd_pairs_with_route": pares_sd_con_ruta,
            "sd_pairs_with_success": pares_sd_con_exito,
            "avg_successful_requests_per_cycle": avg_successful_requests_per_cycle,
        }
        ciclos_por_algoritmo[nombre] = intentos_reales

    print("\n========================================")
    print("METRICAS GLOBALES (TODOS LOS ALGORITMOS)")
    print("========================================")
    for nombre, data in resultados_finales.items():
        print(f"{nombre}:")
        print(f"  - Throughput: {data['throughput']:.4f} EPS")
        print(f"  - Probabilidad de exito a nivel de aplicacion: {data['app_level_success_prob']:.4f}")
        print(f"  - Probabilidad de exito de la capa fisica (n_etg/n_attempts): {data['physical_layer_success_prob']:.4f}")
        print(f"  - Peticiones finales completadas (App): {data['n_success_attempts']}")
        print(f"  - Fidelidad real media observada: {data['fidelity']:.4f}")
        print(f"  - Parejas S-D con ruta: {data['sd_pairs_with_route']}")
        print(f"  - Peticiones con exito por ciclo (media): {data['avg_successful_requests_per_cycle']:.4f}")
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
    avg_fidelities = [resultados_finales[n]["fidelity"] for n, _, _, _, _, _ in sims]
    sd_pairs_with_route = [resultados_finales[n]["sd_pairs_with_route"] for n, _, _, _, _, _ in sims]
    sd_pairs_with_success_per_cycle = [resultados_finales[n]["avg_successful_requests_per_cycle"] for n, _, _, _, _, _ in sims]

    total_requests_in_network = len(last_solicitudes)

    fig = plt.figure(figsize=(10, 5))
    bars = plt.bar(algoritmos, throughputs, color="forestgreen")
    plt.ylabel("Throughput [EPS]")
    plt.title("Comparativa de Throughput Global")
    plt.grid(axis="y", linestyle="--", alpha=0.3)
    for bar, value in zip(bars, throughputs):
        plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
    plt.tight_layout()
    save_graph(fig, sim_folder, "01_throughput_global")

    fig = plt.figure(figsize=(10, 5))
    bars = plt.bar(algoritmos, avg_fidelities, color="forestgreen")
    plt.ylabel("Fidelidad media teorica")
    plt.title("Comparativa de Fidelidad (Basada en Topologia)")
    max_fid = max(avg_fidelities)
    plt.ylim(0, max_fid * 1.15 if max_fid > 0 else 1.0)
    plt.grid(axis="y", linestyle="--", alpha=0.3)
    for bar, value in zip(bars, avg_fidelities):
        plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
    plt.tight_layout()
    save_graph(fig, sim_folder, "03_average_fidelity")

    fig = plt.figure(figsize=(10, 5))
    x_positions = list(range(len(algoritmos)))
    bar_width = 0.38

    bars_route = plt.bar(
        [x - bar_width / 2 for x in x_positions],
        sd_pairs_with_route,
        width=bar_width,
        color="forestgreen",
        label="Parejas con ruta (total)",
    )
    bars_success = plt.bar(
        [x + bar_width / 2 for x in x_positions],
        sd_pairs_with_success_per_cycle,
        width=bar_width,
        color="steelblue",
        label="Parejas con éxito por ciclo (media)",
    )

    if total_requests_in_network > 0:
        plt.axhline(
            y=total_requests_in_network,
            color="darkred",
            linestyle="--",
            linewidth=1.5,
            label=f"Peticiones en el JSON ({total_requests_in_network})",
        )

    plt.ylabel("Parejas S-D")
    plt.title("Parejas S-D con ruta y con éxito medio por ciclo")
    plt.xticks(x_positions, algoritmos)
    plt.grid(axis="y", linestyle="--", alpha=0.3)
    for bar, route_count in zip(bars_route, sd_pairs_with_route):
        plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{route_count}", ha="center", va="bottom")
    for bar, success_count in zip(bars_success, sd_pairs_with_success_per_cycle):
        plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{success_count:.2f}", ha="center", va="bottom")
    plt.legend(loc="upper right")
    plt.tight_layout()
    save_graph(fig, sim_folder, "04_sd_pairs_with_success")

    orden_pares_qcast = _obtener_orden_pares_qcast(rutas_exportar, [nombre for nombre, _, _, _, _, _ in sims])
    matriz_probabilidades = _construir_matriz_probabilidad_pares(
        rutas_exportar,
        [nombre for nombre, _, _, _, _, _ in sims],
        orden_pares_qcast,
        ciclos_por_algoritmo,
    )

    fig = plt.figure(figsize=(max(12, len(orden_pares_qcast) * 0.9), 5.5))
    ax = fig.add_subplot(111)

    if orden_pares_qcast:
        bar_width = 0.08
        y_positions = list(range(len(algoritmos)))

        for row_index, fila in enumerate(matriz_probabilidades):
            base_y = y_positions[row_index]
            for col_index, value in enumerate(fila):
                x_center = col_index
                height = 0.85 * max(0.05, float(value))
                ax.bar(
                    x_center,
                    height,
                    width=bar_width,
                    bottom=base_y - 0.42,
                    color=plt.cm.YlGnBu(value),
                    edgecolor="black",
                    linewidth=0.2,
                    align="center",
                )
                if value > 0.02:
                    ax.text(
                        x_center,
                        base_y - 0.42 + height + 0.02,
                        f"{value:.2f}",
                        ha="center",
                        va="bottom",
                        fontsize=7,
                        rotation=90,
                    )

    ax.set_xticks(range(len(orden_pares_qcast)))
    ax.set_xticklabels([pair.replace("_TO_", "\n") for pair in orden_pares_qcast], rotation=0)
    ax.set_yticks(range(len(algoritmos)))
    ax.set_yticklabels(algoritmos)
    ax.set_xlabel("Parejas S-D (orden Q-CAST)")
    ax.set_ylabel("Algoritmo")
    ax.set_title("Probabilidad de éxito por pareja y algoritmo")
    ax.set_xlim(-0.5, max(0.5, len(orden_pares_qcast) - 0.5))
    ax.set_ylim(-0.6, len(algoritmos) - 0.1)
    ax.grid(axis="y", linestyle="--", alpha=0.2)
    plt.tight_layout()
    save_graph(fig, sim_folder, "05_pair_success_probability_barcode")

    rutas_json_path = os.path.join(sim_folder, "rutas_asignadas.json")
    with open(rutas_json_path, "w", encoding="utf-8") as f:
        json.dump(rutas_exportar, f, indent=4, ensure_ascii=False)
    print("  Rutas guardadas: rutas_asignadas.json")

    export_analysis_json(sim_folder, resultados_finales, rutas_exportar, instrumentacion_por_algoritmo)
    print("  Analisis guardado: analysis_results.json")

    if ultima_net:
        print("\nGuardando topologia e informacion de enlaces...")
        save_topology_diagram(ultima_net, sim_folder, "00_topology_diagram")
        save_link_metadata(ultima_net, sim_folder, "05_link_metadata")

    print_simulation_summary(sim_folder)
    print("Simulaciones completadas.")

    if ultima_net:
        dibujar_escenario(ultima_net)


if __name__ == "__main__":
    main()
