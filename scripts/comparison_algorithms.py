import sys
import os
import json
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



LIMIT_VAL = 1000.0  
SCENARIO_PATH = os.path.join(os.path.dirname(__file__), "..", "escenario_basico.json")
REQUEST_REPEAT = 1
MEMORY_T_COHERE = 10.0

T_PHASE = 1.0
TOTAL_CYCLE_TIME = T_PHASE * 4 

QCAST_STRICT_CONFIG = {
    "replan_each_cycle": True,
    "balance_attempts_across_requests": True,
    "max_main_path_width": 3,
    "cap_success_per_cycle": False,
    "max_recovery_paths": None,
    "recovery_priority": "metric_only",
    "retain_pending_queries_across_cycles": False,
    "q_swap": 1.0,
}

log.set_default_level("WARN")


def validar_configuracion_red(net):
    """
    Comprueba si la memoria definida en los nodos es suficiente
    para el número de canales físicos conectados (grado del nodo).
    """
    for node in net.nodes:
        canales_conectados = [ch for ch in net.qchannels if node in ch.node_list]
        num_canales = len(canales_conectados)
        
        capacidad_real = getattr(node, 'memory').capacity if hasattr(node, 'memory') else 0
        
        if capacidad_real < num_canales:
            print(f" ADVERTENCIA: El nodo {node.name} tiene {num_canales} canales "
                  f"pero solo {capacidad_real} de memoria definida. ¡Puede fallar!")
        else:
            print(f" Nodo {node.name}: {num_canales} canales vs {capacidad_real} memoria (OK)")

class StaticQCastForwarder(QCastForwarder):
    """Forwarder for static route experiments that does not send Q-CAST queries."""

    def _send_initial_queries(self):
        # Prevent automatic Q-CAST query generation in Dijkstra/static route runs.
        return


def install_stack(node, controller=None, qcast_queries=True, forwarder_class=None):
    if not hasattr(node, 'memory'):
        mem = QuantumMemory(name=f"mem_{node.name}", capacity=100, t_cohere=MEMORY_T_COHERE)
        node.memory = mem
        mem.node = node
    else:
        mem = node.memory
        if hasattr(mem, "_t_cohere"):
            mem._t_cohere = MEMORY_T_COHERE

    link_layer = LinkLayer()
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

def _build_requests(net, topo_config):
    solicitudes = []
    base_reqs = topo_config.get("solicitudes", [])
    idx = 0
    for ronda in range(REQUEST_REPEAT):
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


def ejecutar_simulacion(
    nombre,
    controller_class,
    route_alg=None,
    use_capacity=True,
    reserve_all_capacity=False,
    forwarder_class=None,
    controller_kwargs=None,
):

    # 1. Carga de red y topología
    with open(SCENARIO_PATH, "r", encoding="utf-8") as f:
        topo_config = json.load(f)

    sim = Simulator(0, LIMIT_VAL, accuracy=1000000)
    net = QuantumNetwork(None)
    net.build_topology_from_json(SCENARIO_PATH)
    net.all_nodes = list(net.nodes)
    net.requests.clear()

    # 2. INSPECCIÓN 
    for node in net.nodes:
        num_canales = len([ch for ch in net.qchannels if node in ch.node_list])
        cap_actual = node.memory.capacity if hasattr(node, 'memory') else 0
        print(f"Nodo {node.name} tiene {num_canales} canales y {cap_actual} memoria.")

    # 3. Configuración inicial
    if route_alg is not None:
        net.route = route_alg
        net.build_route()

    controller_kwargs = controller_kwargs or {}
    ctrl = controller_class(k_max=5, **controller_kwargs)
    _attach_controller(net, ctrl)
    net.simulator = sim

    solicitudes = _build_requests(net, topo_config)

    # 4. Instalación
    topo_config["t_cohere"] = 10
    qcast_queries = route_alg is None
    for node in net.nodes:
        install_stack(node, controller=ctrl, qcast_queries=qcast_queries, forwarder_class=forwarder_class)
        node.install(sim)

    # 6. Ejecución y métricas
    net.timing = TimingModeSyncQCast(t1= 1, t2= 1, t3= 1, t4= 1)
    net.timing.install(net)

    tiempo_calculo_rutas = None
    if route_alg is not None:
        inicio_calculo_rutas = time.perf_counter()
        net.route = route_alg
        net.build_route()
        if reserve_all_capacity:
            assign_dijkstra_routes_with_capacity_reserve_all(
                net, ctrl, solicitudes, obtener_prob_y_fidelidad_de_ruta,
                enforce_capacity=use_capacity,
            )
        else:
            assign_dijkstra_routes_with_capacity(
                net, ctrl, solicitudes, obtener_prob_y_fidelidad_de_ruta,
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

            _install_static_route_on_forwarders(net, ctrl, route, req_id)

    ciclos_totales = int(LIMIT_VAL / TOTAL_CYCLE_TIME)

    sim.run()
    if tiempo_calculo_rutas is None and ctrl is not None:
        tiempo_calculo_rutas = getattr(ctrl, "qcast_route_calc_time_total", None)
    resultados = construir_resultados_qcast(ctrl, solicitudes, ciclos_totales)
    counters = LinkLayerCounters.aggregate(net.nodes)

    return resultados, counters, net, solicitudes, ciclos_totales, tiempo_calculo_rutas


def calcular_fidelidad_media_real(resultados):
    if not resultados:
        return 0.0

    fidelidades = []
    for r in resultados:
        route = r.get("route")
        if not route:
            continue
        observed_fidelity = r.get("observed_fidelity", None)
        if observed_fidelity is not None and observed_fidelity > 0:
            fidelidades.append(float(observed_fidelity))
            continue
        route_fidelity = float(r.get("route_fidelity", 0.0) or 0.0)
        if route_fidelity > 0:
            fidelidades.append(route_fidelity)

    return sum(fidelidades) / len(fidelidades) if fidelidades else 0.0


def serializar_instrumentacion(ctrl):
    if ctrl is None:
        return {}

    local_by_cycle = getattr(ctrl, "local_entanglement_by_cycle", {})
    eligible_by_cycle = getattr(ctrl, "eligible_by_cycle", {})
    query_order_by_cycle = getattr(ctrl, "query_order_by_cycle", {})
    success_history = getattr(ctrl, "success_history", [])

    return {
        "eligible_total": getattr(ctrl, "eligible_total", 0),
        "eligible_by_cycle": eligible_by_cycle,
        "local_entanglement_total": getattr(ctrl, "local_entanglement_total", 0),
        "local_entanglement_by_cycle": local_by_cycle,
        "query_order_by_cycle": query_order_by_cycle,
        "success_history": success_history,
        "p4_phase_count": getattr(ctrl, "p4_phase_count", 0),
        "p4_recovery_applied": getattr(ctrl, "p4_recovery_applied", 0),
        "qchannel_activations_by_path": getattr(ctrl, "qchannel_activations_by_path", {}),
        "qchannel_activation_names_by_path": getattr(ctrl, "qchannel_activation_names_by_path", {}),
    }


def export_analysis_json(output_dir: str, resultados_finales: dict, rutas_exportar: dict, instrumentacion_por_algoritmo: dict) -> str:
    """Exporta un JSON de análisis con formato limpio para postprocesamiento."""
    payload = {
        "algorithms": []
    }

    for nombre, metrics in resultados_finales.items():
        payload["algorithms"].append({
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
        })

    filename = os.path.join(output_dir, "analysis_results.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4, ensure_ascii=False)

    return filename



# EJECUCIÓN PRINCIPAL

sims = [
    ("Dijkstra Clásico", QCastController, DijkstraRouteAlgorithm(), True, False, None, dict(QCAST_STRICT_CONFIG)),
    ("Dijkstra Distancia", QCastController, DijkstraDistanceRouteAlgorithm(), True, False, None, dict(QCAST_STRICT_CONFIG)),
    ("Dijkstra Capacidad Reserva", QCastController, DijkstraRouteAlgorithm(), True, True, None, dict(QCAST_STRICT_CONFIG)),
    ("Dijkstra Distancia Reserva", QCastController, DijkstraDistanceRouteAlgorithm(), True, True, None, dict(QCAST_STRICT_CONFIG)),
    ("Q-CAST", QCastController, None, False, False, None, dict(QCAST_STRICT_CONFIG)),
    ("Q-CAST Varios Entrelazamientos", QCastMultiEntController, None, False, False, QCastMultiEntForwarder, dict(QCAST_STRICT_CONFIG)),
]

resultados_finales = {}
rutas_exportar = {}
ultima_net = None
instrumentacion_por_algoritmo = {}
last_solicitudes = []

for nombre, ctrl_class, route_alg, use_cap, reserve_all, fw_class, ctrl_kwargs in sims:
    resultados, counters, net, solicitudes, intentos_reales, tiempo_calculo_rutas = ejecutar_simulacion(
        nombre,
        ctrl_class,
        route_alg,
        use_cap,
        reserve_all,
        forwarder_class=fw_class,
        controller_kwargs=ctrl_kwargs,
    )
    last_solicitudes = solicitudes
    ultima_net = net
    
    ctrl = getattr(net, "controller", None)
    instrumentacion = serializar_instrumentacion(ctrl)
    instrumentacion_por_algoritmo[nombre] = instrumentacion
    
    # Agrupar resultados por par src-dst (base_req) 
    grouped: dict[str, dict] = {}
    for r in resultados:
        req_id = r.get("req_id", "Desconocido")
        # Intentamos extraer par base src-dst del req_id
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
                            f"Fallo en {desvio['segment_src']}-{desvio['segment_dst']} -> Usar desvío: {desvio['route']} (Métrica: {desvio['metric']:.2f})"
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
        
        print(f"Petición {req_id}: Info completa del controlador: {info_ruta}")

    lista_rutas_agrupada = []
    for k, v in grouped.items():
        avg_metric = v["metrica_eda_sum"] / v["metrica_count"] if v["metrica_count"] > 0 else 0.0
        lista_rutas_agrupada.append({
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
        })

    pares_sd_con_exito = sum(1 for v in lista_rutas_agrupada if v["exitos_conseguidos"] > 0)
    pares_sd_con_ruta = sum(1 for v in lista_rutas_agrupada if v["ruta_asignada"])

    rutas_exportar[nombre] = lista_rutas_agrupada
    rutas_exportar[f"{nombre}_instrumentacion"] = instrumentacion
    rutas_exportar[f"{nombre}_tiempo_calculo_rutas_segundos"] = tiempo_calculo_rutas
    
    total_exitos = sum(r.get("successes", 0) for r in resultados)
    throughput = total_exitos / LIMIT_VAL
    
    intentos_posibles_totales = len(solicitudes) * intentos_reales
    app_level_success_prob = total_exitos /  500 
    
    physical_layer_success_prob = counters.n_etg / 2500 
    
    resultados_finales[nombre] = {
        "throughput": throughput,
        "app_level_success_prob": app_level_success_prob,
        "physical_layer_success_prob": physical_layer_success_prob,
        "n_etg": counters.n_etg,             
        "n_attempts": counters.n_attempts,
        "n_success_attempts": total_exitos,
        "fidelity": calcular_fidelidad_media_real(resultados),
        "sd_pairs_with_route": pares_sd_con_ruta,
        "sd_pairs_with_success": pares_sd_con_exito,
    }
    

print("\n========================================")
print("MÉTRICAS GLOBALES (TODOS LOS ALGORITMOS)")
print("========================================")
for nombre, data in resultados_finales.items():
    print(f"{nombre}:")
    print(f"  - Throughput: {data['throughput']:.4f} EPS")
    print(f"  - Probabilidad de éxito a nivel de aplicación: {data['app_level_success_prob']:.4f}")
    print(f"  - Probabilidad de éxito de la capa física (n_etg/n_attempts): {data['physical_layer_success_prob']:.4f}")
    print(f"  - Peticiones finales completadas (App): {data['n_success_attempts']}")
    print(f"  - Fidelidad real media observada: {data['fidelity']:.4f}")
    print(f"  - Parejas S-D con ruta: {data['sd_pairs_with_route']}")
    print(f"  - Parejas S-D con éxito: {data['sd_pairs_with_success']}")

if ultima_net is not None:
    ctrl = getattr(ultima_net, "controller", None)
    if ctrl is not None:
        print("\n========================================")
        print("INSTRUMENTACIÓN")
        print("========================================")
        print(f"Canales activados por path_id: {getattr(ctrl, 'qchannel_activations_by_path', {})}")
        print(f"Canales activados por path_id (nombres): {getattr(ctrl, 'qchannel_activation_names_by_path', {})}")
        print(f"Qubits que llegan a ELIGIBLE: {getattr(ctrl, 'eligible_total', 0)}")
        print(f"Qubits ELIGIBLE por ciclo: {getattr(ctrl, 'eligible_by_cycle', {})}")
        print(f"Entradas a P4 de recuperación: {getattr(ctrl, 'p4_phase_count', 0)}")
        print(f"Recuperaciones P4 aplicadas: {getattr(ctrl, 'p4_recovery_applied', 0)}")

if instrumentacion_por_algoritmo:
    for nombre_algoritmo, instrumentacion in instrumentacion_por_algoritmo.items():
        if instrumentacion:
            rutas_exportar[f"{nombre_algoritmo}_instrumentacion"] = instrumentacion
    


algoritmos = [
    "Dijkstra\nsalts",
    "Dijkstra\ndistáncia",
    "Dijkstra\ncapacidad\nreserva",
    "Dijkstra\ndistancia\ncapacidad\nreserva",
    "Q-CAST",
    "Q-CAST\nVarios\nEntrelazamientos.",
]

throughputs = [resultados_finales[n]["throughput"] for n, _, _, _, _, _, _ in sims]
physical_success_probs = [resultados_finales[n]["physical_layer_success_prob"] for n, _, _, _, _, _, _ in sims]
avg_fidelities = [resultados_finales[n]["fidelity"] for n, _, _, _, _, _, _ in sims]
sd_pairs_with_route = [resultados_finales[n]["sd_pairs_with_route"] for n, _, _, _, _, _, _ in sims]
sd_pairs_with_success = [resultados_finales[n]["sd_pairs_with_success"] for n, _, _, _, _, _, _ in sims]

sim_folder = create_simulation_folder()

total_requests_in_network = len(last_solicitudes)

# Gráfico 1: Throughput
fig = plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, throughputs, color="forestgreen")
plt.ylabel("Throughput [EPS]")
plt.title("Comparativa de Throughput Global")
plt.grid(axis="y", linestyle="--", alpha=0.3)
for bar, value in zip(bars, throughputs):
    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
plt.tight_layout()
save_graph(fig, sim_folder, "01_throughput_global")

# Gráfico 2: Probabilidad de Éxito Física
fig = plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, physical_success_probs, color="forestgreen")
plt.ylabel("Probabilidad de Éxito de la Capa Física")
plt.title("Comparativa de Probabilidad de Éxito de Entrelazamiento (Capa Física)")
max_succ = max(physical_success_probs)
plt.ylim(0, max_succ * 1.15 if max_succ > 0 else 1.0) 
plt.grid(axis="y", linestyle="--", alpha=0.3)
for bar, value in zip(bars, physical_success_probs):
    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
plt.tight_layout()
save_graph(fig, sim_folder, "02_success_probability_physical_layer")

# Gráfico 3: Fidelidad Teórica
fig = plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, avg_fidelities, color="forestgreen")
plt.ylabel("Fidelidad media teórica")
plt.title("Comparativa de Fidelidad (Basada en Topología)")
max_fid = max(avg_fidelities)
plt.ylim(0, max_fid * 1.15 if max_fid > 0 else 1.0)
plt.grid(axis="y", linestyle="--", alpha=0.3)
for bar, value in zip(bars, avg_fidelities):
    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
plt.tight_layout()
save_graph(fig, sim_folder, "03_average_fidelity")

# Gráfico 4: Parejas S-D con ruta y con éxito
fig = plt.figure(figsize=(10, 5))
x_positions = list(range(len(algoritmos)))
bar_width = 0.38

bars_route = plt.bar(
    [x - bar_width / 2 for x in x_positions],
    sd_pairs_with_route,
    width=bar_width,
    color="forestgreen",
    label="Parejas con ruta",
)
bars_success = plt.bar(
    [x + bar_width / 2 for x in x_positions],
    sd_pairs_with_success,
    width=bar_width,
    color="steelblue",
    label="Parejas con éxito",
)

if total_requests_in_network > 0:
    plt.axhline(
        y=total_requests_in_network,
        color="darkred",
        linestyle="--",
        linewidth=1.5,
        label=f"Peticiones en la red ({total_requests_in_network})",
    )
plt.ylabel("Parejas S-D")
plt.title("Parejas S-D con ruta y con entrelazamiento exitoso")
plt.xticks(x_positions, algoritmos)
plt.grid(axis="y", linestyle="--", alpha=0.3)
for bar, route_count in zip(bars_route, sd_pairs_with_route):
    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{route_count}", ha="center", va="bottom")
for bar, success_count in zip(bars_success, sd_pairs_with_success):
    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{success_count}", ha="center", va="bottom")
plt.legend(loc="upper right")
plt.tight_layout()
save_graph(fig, sim_folder, "04_sd_pairs_with_success")

# GUARDAR METADATA Y CONFIGURACIÓN
rutas_json_path = os.path.join(sim_folder, "rutas_asignadas.json")
with open(rutas_json_path, "w", encoding="utf-8") as f:
    json.dump(rutas_exportar, f, indent=4, ensure_ascii=False)
print(f"  Rutas guardadas: rutas_asignadas.json")

analysis_json_path = os.path.join(sim_folder, "analysis_results.json")
export_analysis_json(sim_folder, resultados_finales, rutas_exportar, instrumentacion_por_algoritmo)
print(f"  Análisis guardado: analysis_results.json")

# GUARDAR TOPOLOGÍA Y METADATA DE ENLACES
if ultima_net:
    print("\nGuardando topología e información de enlaces...")
    save_topology_diagram(ultima_net, sim_folder, "00_topology_diagram")
    save_link_metadata(ultima_net, sim_folder, "05_link_metadata")

print_simulation_summary(sim_folder)
print("Simulaciones completadas.")

if ultima_net:
    dibujar_escenario(ultima_net)