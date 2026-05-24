import sys
import os
import json

import matplotlib.pyplot as plt

# Aseguramos que MQNS está en el path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mqns.simulator import Simulator
from mqns.network.network.timing import TimingModeSyncQCast
from mqns.network.network.network import QuantumNetwork, dibujar_escenario
from mqns.network.network.reporting import (
    build_request_id,
    construir_resultados_qcast,
    obtener_prob_y_fidelidad_de_ruta,
)
from mqns.network.route import (
    DijkstraDistanceRouteAlgorithm,
    DijkstraRouteAlgorithm,
    YenRouteAlgorithm,
    assign_dijkstra_routes_with_capacity,
    initialize_virtual_node_capacity,
)
from mqns.network.qcast.controller import QCastController, QCastFidelityController
from mqns.network.qcast.forwarder import QCastForwarder
from mqns.network.protocol.link_layer import LinkLayer, LinkLayerCounters
from mqns.utils import log


LIMIT_VAL = 100.0
ATTEMPTS = 500
SCENARIO_PATH = os.path.join(os.path.dirname(__file__), "..", "escenario_basico.json")

log.set_default_level("DEBUG") 


def install_stack(node):
    link_layer = LinkLayer()
    forwarder = QCastForwarder(k_max=2, purif_enabled=True)
    setattr(forwarder, "swapping_enabled", True)
    node.add_apps([link_layer, forwarder])
    setattr(node, "forwarder", forwarder)
    return forwarder


def _build_requests(net, topo_config):
    solicitudes = []
    for idx, req in enumerate(topo_config.get("solicitudes", [])):
        src = net.get_node(req["src"])
        dst = net.get_node(req["dst"])
        if src and dst:
            req_id = build_request_id(src.name, dst.name, idx)
            net.add_request(src, dst, {"req_id": req_id})
            solicitudes.append({"req_id": req_id, "src": src, "dst": dst})
    return solicitudes


def _attach_controller(net, ctrl):
    setattr(net, "controller", ctrl)
    setattr(ctrl, "net", net)
    if net.nodes:
        net.nodes[0].add_apps(ctrl)


def ejecutar_simulacion(nombre, controller_class, route_alg=None, use_capacity=True):
    print(f"\n--- Ejecutando: {nombre} ---")

    with open(SCENARIO_PATH, "r", encoding="utf-8") as f:
        topo_config = json.load(f)

    sim = Simulator(0, LIMIT_VAL, accuracy=1000000)
    net = QuantumNetwork(None)
    net.build_topology_from_json(SCENARIO_PATH)
    net.requests.clear()

    if route_alg is not None:
        net.route = route_alg
        net.build_route()

    ctrl = controller_class(k_max=2)
    _attach_controller(net, ctrl)
    net.simulator = sim

    solicitudes = _build_requests(net, topo_config)

    for node in net.nodes:
        install_stack(node)
        node.install(sim)

    setattr(net, "all_nodes", list(net.nodes) + [ctrl])

    net.timing = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
    net.timing.install(net)

    if route_alg is not None:
        # Precarga de canales usando los objetos QNode directos y path_id=None
        from mqns.network.protocol.event import ManageActiveChannels
        for node in net.nodes:
            for ch in net.qchannels:
                if node in ch.node_list:
                    neighbor_node = next(n for n in ch.node_list if n != node)
                    sim.add_event(
                        ManageActiveChannels(
                            node,
                            neighbor_node,
                            ch,
                            path_id=None,
                            start=True,
                            t=sim.tc
                        )
                    )
                        
        initialize_virtual_node_capacity(ctrl, net.all_nodes)
        assign_dijkstra_routes_with_capacity(
            net,
            ctrl,
            solicitudes,
            obtener_prob_y_fidelidad_de_ruta,
            enforce_capacity=use_capacity,
        )

    sim.run()

    resultados = construir_resultados_qcast(ctrl, solicitudes, ATTEMPTS)
    counters = LinkLayerCounters.aggregate(net.nodes)
    
    return resultados, counters, net


def calcular_fidelidad_media(resultados):
    if not resultados:
        return 0.0
    fidelidades = []
    for r in resultados:
        fidelidades.append(obtener_fidelidad_reporte(r))
    return sum(fidelidades) / len(fidelidades)


def obtener_fidelidad_reporte(resultado):
    observed = resultado.get("observed_fidelity", 0.0)
    return observed if observed > 0 else resultado.get("route_fidelity", 0.0)


# =====================================================================
# EJECUCIÓN PRINCIPAL
# =====================================================================

sims = [
    ("Dijkstra Clásico", QCastController, DijkstraRouteAlgorithm(), True),
    ("Dijkstra Capacidad", QCastController, YenRouteAlgorithm(k_paths=3), True),
    ("Dijkstra Distancia", QCastController, DijkstraDistanceRouteAlgorithm(), True),
    ("Q-CAST", QCastController, None, False),
    ("Q-CAST Fidelidad", QCastFidelityController, None, False),
]

resultados_finales = {}
rutas_exportar = {}
ultima_net = None

for nombre, ctrl_class, route_alg, use_cap in sims:
    resultados, counters, net = ejecutar_simulacion(nombre, ctrl_class, route_alg, use_cap)
    ultima_net = net
    
    # Extraer rutas para guardar en JSON
    lista_rutas = []
    for r in resultados:
        camino = r.get("path", r.get("route", []))
        lista_rutas.append({
            "req_id": r.get("req_id", "Desconocido"),
            "ruta_asignada": camino,
            "fidelidad_ruta": obtener_fidelidad_reporte(r),
            "exitos_conseguidos": r.get("successes", 0)
        })
    rutas_exportar[nombre] = lista_rutas
    
    # Calcular métricas globales
    total_exitos = sum(r["successes"] for r in resultados)
    throughput = total_exitos / LIMIT_VAL
    success_ratio = counters.n_etg / counters.n_attempts if counters.n_attempts > 0 else 0.0
    resultados_finales[nombre] = {
        "throughput": throughput,
        "success_ratio": success_ratio,
        "n_etg": counters.n_etg,
        "n_attempts": counters.n_attempts,
        "fidelity": calcular_fidelidad_media(resultados),
    }

print("\n========================================")
print("MÉTRICAS GLOBALES (TODOS LOS ALGORITMOS)")
print("========================================")
for nombre, data in resultados_finales.items():
    print(f"{nombre}:")
    print(f"  - Throughput: {data['throughput']:.4f} EPS")
    print(f"  - Tasa de éxito (n_etg/n_attempts): {data['success_ratio']:.4f}")
    print(f"  - n_etg={data['n_etg']}, n_attempts={data['n_attempts']}")
    print(f"  - Fidelidad media: {data['fidelity']:.4f}")


# =====================================================================
# GENERACIÓN DE GRÁFICAS
# =====================================================================

algoritmos = [
    "Dijkstra\n#salts",
    "Dijkstra\ncapacitat",
    "Dijkstra\ndistáncia",
    "Q-CAST",
    "Q-CAST\n(Fidelidad)",
]

throughputs = [
    resultados_finales["Dijkstra Clásico"]["throughput"],
    resultados_finales["Dijkstra Capacidad"]["throughput"],
    resultados_finales["Dijkstra Distancia"]["throughput"],
    resultados_finales["Q-CAST"]["throughput"],
    resultados_finales["Q-CAST Fidelidad"]["throughput"],
]

success_probs = [
    resultados_finales["Dijkstra Clásico"]["success_ratio"],
    resultados_finales["Dijkstra Capacidad"]["success_ratio"],
    resultados_finales["Dijkstra Distancia"]["success_ratio"],
    resultados_finales["Q-CAST"]["success_ratio"],
    resultados_finales["Q-CAST Fidelidad"]["success_ratio"],
]

avg_fidelities = [
    resultados_finales["Dijkstra Clásico"]["fidelity"],
    resultados_finales["Dijkstra Capacidad"]["fidelity"],
    resultados_finales["Dijkstra Distancia"]["fidelity"],
    resultados_finales["Q-CAST"]["fidelity"],
    resultados_finales["Q-CAST Fidelidad"]["fidelity"],
]

output_dir = os.path.join(os.path.dirname(__file__), "..", "outputs")
os.makedirs(output_dir, exist_ok=True)

# Gráfico 1: Throughput
plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, throughputs, color="forestgreen")
plt.ylabel("Throughput [EPS]")
plt.title("Comparativa de Throughput Global")
plt.grid(axis="y", linestyle="--", alpha=0.3)

for bar, value in zip(bars, throughputs):
    plt.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height(),
        f"{value:.2f}",
        ha="center",
        va="bottom",
    )

throughput_path = os.path.join(output_dir, "throughput.png")
plt.tight_layout()
plt.savefig(throughput_path, dpi=300)
plt.close()

# Gráfico 2: Probabilidad de Éxito
plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, success_probs, color="forestgreen")
plt.ylabel("Probabilidad de Éxito")
plt.title("Comparativa de Probabilidad de Éxito")

max_succ = max(success_probs)
plt.ylim(0, max_succ * 1.15 if max_succ > 0 else 1.0) 
plt.grid(axis="y", linestyle="--", alpha=0.3)

for bar, value in zip(bars, success_probs):
    plt.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height(),
        f"{value:.4f}",
        ha="center",
        va="bottom",
    )

success_prob_path = os.path.join(output_dir, "success_probability_global_algoritmos.png")
plt.tight_layout()
plt.savefig(success_prob_path, dpi=300)
plt.close()

# Gráfico 3: Fidelidad
plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, avg_fidelities, color="forestgreen")
plt.ylabel("Fidelidad media")
plt.title("Comparativa de Fidelidad Media")

max_fid = max(avg_fidelities)
plt.ylim(0, max_fid * 1.15 if max_fid > 0 else 1.0)
plt.grid(axis="y", linestyle="--", alpha=0.3)

for bar, value in zip(bars, avg_fidelities):
    plt.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height(),
        f"{value:.4f}",
        ha="center",
        va="bottom",
    )

avg_fidelity_path = os.path.join(output_dir, "average_fidelity_global_algoritmos.png")
plt.tight_layout()
plt.savefig(avg_fidelity_path, dpi=300)
plt.close()


# =====================================================================
# GUARDADO DE DATOS Y VISUALIZACIÓN FINAL
# =====================================================================

rutas_json_path = os.path.join(output_dir, "rutas_asignadas.json")
with open(rutas_json_path, "w", encoding="utf-8") as f:
    json.dump(rutas_exportar, f, indent=4, ensure_ascii=False)

print(f"\n¡Gráficas guardadas exitosamente en: '{output_dir}'!")
print(f"Rutas exportadas detalladamente en: {rutas_json_path}")
print("Simulaciones completadas.")

if ultima_net:
    print("\nAbriendo el visor del escenario. Cierra la ventana para terminar el script...")
    dibujar_escenario(ultima_net)
    plt.show()