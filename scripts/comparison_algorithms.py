import sys
import os
import json

import matplotlib.pyplot as plt
import numpy as np

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
from mqns.network.qcast.controller import QCastController, QCastFidelityController
from mqns.network.qcast.forwarder import QCastForwarder
from mqns.network.protocol.link_layer import LinkLayer, LinkLayerCounters
from mqns.utils import log
from mqns.entity.memory.memory import QuantumMemory



LIMIT_VAL = 1000.0  
SCENARIO_PATH = os.path.join(os.path.dirname(__file__), "..", "escenario_basico.json")
REQUEST_REPEAT = 1
MEMORY_T_COHERE = 10.0

T_PHASE = 1.0
TOTAL_CYCLE_TIME = T_PHASE * 4 

log.set_default_level("DEBUG")


def validar_configuracion_red(net):
    """
    Comprueba si la memoria definida en los nodos es suficiente
    para el número de canales físicos conectados (grado del nodo).
    """
    print("\n--- Validación de Configuración ---")
    for node in net.nodes:
        # Contamos cuántos canales tiene conectados este nodo
        canales_conectados = [ch for ch in net.qchannels if node in ch.node_list]
        num_canales = len(canales_conectados)
        
        # Leemos la capacidad real definida en el nodo
        capacidad_real = getattr(node, 'memory').capacity if hasattr(node, 'memory') else 0
        
        # Validamos: el nodo necesita al menos 1 qubit por canal conectado
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


def install_stack(node, controller=None, qcast_queries=True):
    # Usamos la memoria ya creada por la topología JSON.
    # Si el nodo no tuviera memoria, la creamos como fallback.
    if not hasattr(node, 'memory'):
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
    # SOLUCIÓN TRÁFICO: Multiplicamos las peticiones para crear una cola continua.
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


def ejecutar_simulacion(nombre, controller_class, route_alg=None, use_capacity=True, reserve_all_capacity=False):
    print(f"\n--- Ejecutando: {nombre} ---")

    # 1. Carga de red y topología
    with open(SCENARIO_PATH, "r", encoding="utf-8") as f:
        topo_config = json.load(f)

    sim = Simulator(0, LIMIT_VAL, accuracy=1000000)
    net = QuantumNetwork(None)
    net.build_topology_from_json(SCENARIO_PATH)
    net.all_nodes = list(net.nodes)
    net.requests.clear()

    # 2. INSPECCIÓN (Para tu TFG)
    print("--- Verificación de Hardware ---")
    for node in net.nodes:
        # Contamos canales reales según la topología cargada
        num_canales = len([ch for ch in net.qchannels if node in ch.node_list])
        cap_actual = node.memory.capacity if hasattr(node, 'memory') else 0
        print(f"Nodo {node.name} tiene {num_canales} canales y {cap_actual} memoria.")

    # 3. Configuración inicial
    if route_alg is not None:
        net.route = route_alg
        net.build_route()

    ctrl = controller_class(k_max=5)
    _attach_controller(net, ctrl)
    net.simulator = sim

    solicitudes = _build_requests(net, topo_config)

    # 4. Instalación
    topo_config["t_cohere"] = 10
    qcast_queries = route_alg is None
    for node in net.nodes:
        install_stack(node, controller=ctrl, qcast_queries=qcast_queries)
        node.install(sim)

    # 5. No reasignamos manualmente los qubits del canal.
    # La topología ya asigna un qubit por canal a cada extremo en build_topology_from_json().

    # 6. Ejecución y métricas
    net.timing = TimingModeSyncQCast(t1= 1, t2= 1, t3= 1, t4= 1)
    net.timing.install(net)

    if route_alg is not None:
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

        # Instalamos las rutas estáticas calculadas para que los forwards las ejecuten.
        for req in solicitudes:
            req_id = req["req_id"]
            info = ctrl.request_route_info.get(req_id)
            if not info:
                continue
            route = info.get("route")
            if not route:
                continue

            _install_static_route_on_forwarders(net, ctrl, route, req_id)

    # Calculamos cuántas oportunidades reales (ciclos) tendrá cada petición
    ciclos_totales = int(LIMIT_VAL / TOTAL_CYCLE_TIME)

    sim.run()
    resultados = construir_resultados_qcast(ctrl, solicitudes, ciclos_totales)
    counters = LinkLayerCounters.aggregate(net.nodes)

    return resultados, counters, net, solicitudes, ciclos_totales


def calcular_fidelidad_media_real(resultados):
    """
    Fidelidad media observada en la simulación.

    Preferimos la fidelidad medida sobre los éxitos E2E reales porque la
    fidelidad teórica por ruta no refleja la pérdida acumulada por esperas,
    swaps y liberaciones efectivas durante la ejecución.
    """
    if not resultados:
        return 0.0
    fidelidades = []
    for r in resultados:
        # La fidelidad observada la calculamos a partir de las muestras reales
        # recogidas por el controlador. Si no existen, caemos a la fidelidad de
        # ruta estimada para no perder la fila.
        observed_fidelity = r.get("observed_fidelity", None)
        if observed_fidelity is not None and observed_fidelity > 0:
            fidelidades.append(float(observed_fidelity))
            continue

        route_fidelity = r.get("route_fidelity", 0.0)
        fidelidades.append(float(route_fidelity))
        
    return sum(fidelidades) / len(fidelidades) if fidelidades else 0.0


def serializar_instrumentacion(ctrl):
    if ctrl is None:
        return {}

    local_by_cycle = getattr(ctrl, "local_entanglement_by_cycle", {})
    eligible_by_cycle = getattr(ctrl, "eligible_by_cycle", {})

    return {
        "eligible_total": getattr(ctrl, "eligible_total", 0),
        "eligible_by_cycle": eligible_by_cycle,
        "local_entanglement_total": getattr(ctrl, "local_entanglement_total", 0),
        "local_entanglement_by_cycle": local_by_cycle,
        "p4_phase_count": getattr(ctrl, "p4_phase_count", 0),
        "p4_recovery_applied": getattr(ctrl, "p4_recovery_applied", 0),
        "qchannel_activations_by_path": getattr(ctrl, "qchannel_activations_by_path", {}),
        "qchannel_activation_names_by_path": getattr(ctrl, "qchannel_activation_names_by_path", {}),
    }


# =====================================================================
# EJECUCIÓN PRINCIPAL
# =====================================================================

sims = [
    ("Dijkstra Clásico", QCastController, DijkstraRouteAlgorithm(), True, False),
    ("Dijkstra Distancia", QCastController, DijkstraDistanceRouteAlgorithm(), True, False),
    ("Dijkstra Capacidad Reserva", QCastController, DijkstraRouteAlgorithm(), True, True),
    ("Dijkstra Distancia Reserva", QCastController, DijkstraDistanceRouteAlgorithm(), True, True),
    ("Q-CAST", QCastController, None, False, False),
]

resultados_finales = {}
rutas_exportar = {}
ultima_net = None
instrumentacion_por_algoritmo = {}

for nombre, ctrl_class, route_alg, use_cap, reserve_all in sims:
    resultados, counters, net, solicitudes, intentos_reales = ejecutar_simulacion(nombre, ctrl_class, route_alg, use_cap, reserve_all)
    ultima_net = net
    
    ctrl = getattr(net, "controller", None)
    instrumentacion = serializar_instrumentacion(ctrl)
    instrumentacion_por_algoritmo[nombre] = instrumentacion
    
    # Agrupar resultados por par src-dst (base_req) para evitar filas repetidas
    grouped: dict[str, dict] = {}
    for r in resultados:
        req_id = r.get("req_id", "Desconocido")
        # Intentamos extraer par base src-dst del req_id: formato esperado *_<src>_TO_<dst>
        parts = req_id.split("_")
        base_key = req_id
        if len(parts) >= 3 and "TO" in parts:
            # reconstruir desde la primera aparición de <src>_TO_<dst>
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
                    break

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
                "total_reqs": 1,
            }
        else:
            entry["examples"].append(req_id)
            # si ruta vacía, mantenemos la no-vacía previa
            if not entry["ruta_asignada"] and camino:
                entry["ruta_asignada"] = camino
            if metrica_eda != 0.0:
                entry["metrica_eda_sum"] += metrica_eda
                entry["metrica_count"] += 1
            entry["exitos_conseguidos"] += successes
            entry["total_reqs"] += 1
        
        print(f"Petición {req_id}: Info completa del controlador: {info_ruta}")

    # Formatear lista agrupada: promediar métricas cuando proceda
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
            "total_reqs": v["total_reqs"],
        })

    pares_sd_con_exito = sum(1 for v in lista_rutas_agrupada if v["exitos_conseguidos"] > 0)

    rutas_exportar[nombre] = lista_rutas_agrupada
    rutas_exportar[f"{nombre}_instrumentacion"] = instrumentacion
    
    # MÉTRICAS REALES DE RENDIMIENTO
    total_exitos = sum(r.get("successes", 0) for r in resultados)
    throughput = total_exitos / LIMIT_VAL
    
    # Probabilidad de éxito a nivel de aplicación (basada en los intentos para métricas)
    # Éxitos totales / (Número de peticiones * Ciclos posibles)
    intentos_posibles_totales = len(solicitudes) * intentos_reales
    app_level_success_prob = total_exitos /  2500 
    
    # Probabilidad de éxito de la capa física (n_etg/n_attempts)
    physical_layer_success_prob = counters.n_etg / 2500 
    
    resultados_finales[nombre] = {
        "throughput": throughput,
        "app_level_success_prob": app_level_success_prob,
        "physical_layer_success_prob": physical_layer_success_prob,
        "n_etg": counters.n_etg,             
        "n_attempts": counters.n_attempts,
        "n_success_attempts": total_exitos,
        "fidelity": calcular_fidelidad_media_real(resultados),
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
    


# =====================================================================
# GENERACIÓN DE GRÁFICAS
# =====================================================================

algoritmos = [
    "Dijkstra\nsalts",
    "Dijkstra\ndistáncia",
    "Dijkstra\ncapacitat\nreserva",
    "Dijkstra\ndistancia\ncapacitat\nreserva",
    "Q-CAST",
]

throughputs = [resultados_finales[n]["throughput"] for n, _, _, _, _ in sims]
physical_success_probs = [resultados_finales[n]["physical_layer_success_prob"] for n, _, _, _, _ in sims]
avg_fidelities = [resultados_finales[n]["fidelity"] for n, _, _, _, _ in sims]
sd_pairs_with_success = [resultados_finales[n]["sd_pairs_with_success"] for n, _, _, _, _ in sims]

output_dir = os.path.join(os.path.dirname(__file__), "..", "outputs")
os.makedirs(output_dir, exist_ok=True)

# Gráfico 1: Throughput
plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, throughputs, color="forestgreen")
plt.ylabel("Throughput [EPS]")
plt.title("Comparativa de Throughput Global")
plt.grid(axis="y", linestyle="--", alpha=0.3)
for bar, value in zip(bars, throughputs):
    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
plt.tight_layout()
plt.savefig(os.path.join(output_dir, "throughput.png"), dpi=300)
plt.close()

# Gráfico 2: Probabilidad de Éxito Física
plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, physical_success_probs, color="forestgreen")
plt.ylabel("Probabilidad de Éxito de la Capa Física")
plt.title("Comparativa de Probabilidad de Éxito de Entrelazamiento (Capa Física)")
max_succ = max(physical_success_probs)
plt.ylim(0, max_succ * 1.15 if max_succ > 0 else 1.0) 
plt.grid(axis="y", linestyle="--", alpha=0.3)
for bar, value in zip(bars, physical_success_probs):
    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
plt.tight_layout()
plt.savefig(os.path.join(output_dir, "success_probability_global_algoritmos.png"), dpi=300)
plt.close()

# Gráfico 3: Fidelidad Teórica
plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, avg_fidelities, color="forestgreen")
plt.ylabel("Fidelidad media teórica")
plt.title("Comparativa de Fidelidad (Basada en Topología)")
max_fid = max(avg_fidelities)
plt.ylim(0, max_fid * 1.15 if max_fid > 0 else 1.0)
plt.grid(axis="y", linestyle="--", alpha=0.3)
for bar, value in zip(bars, avg_fidelities):
    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.4f}", ha="center", va="bottom")
plt.tight_layout()
plt.savefig(os.path.join(output_dir, "average_fidelity_global_algoritmos.png"), dpi=300)
plt.close()

# Gráfico 4: Parejas S-D con éxito
plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, sd_pairs_with_success, color="forestgreen")
plt.ylabel("Parejas S-D con éxito")
plt.title("Número de parejas S-D conéxito")
plt.grid(axis="y", linestyle="--", alpha=0.3)
for bar, value in zip(bars, sd_pairs_with_success):
    plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value}", ha="center", va="bottom")

plt.tight_layout()
plt.savefig(os.path.join(output_dir, "sd_pairs_with_success.png"), dpi=300)
plt.close()

rutas_json_path = os.path.join(output_dir, "rutas_asignadas.json")
with open(rutas_json_path, "w", encoding="utf-8") as f:
    json.dump(rutas_exportar, f, indent=4, ensure_ascii=False)

print(f"\n¡Gráficas guardadas exitosamente en: '{output_dir}'!")
print("Simulaciones completadas.")

if ultima_net:
    dibujar_escenario(ultima_net)