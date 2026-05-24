import sys
import os
import json
import argparse
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from mqns.simulator import Simulator
from mqns.network.network.timing import TimingModeSyncQCast
from mqns.network.network.network import QuantumNetwork, dibujar_escenario
from mqns.network.network.reporting import (
    build_request_id,
    obtener_prob_y_fidelidad_de_ruta,
    construir_resultados_qcast,
    imprimir_resumen_algoritmo,
    imprimir_info_rutas_detallada,
)
from mqns.network.route import (
    DijkstraRouteAlgorithm,
    DijkstraDistanceRouteAlgorithm,
    YenRouteAlgorithm,
    assign_dijkstra_routes_with_capacity,
    initialize_virtual_node_capacity,
)
from mqns.utils import log
from mqns.network.qcast.controller import QCastController, QCastFidelityController
from mqns.network.qcast.forwarder import QCastForwarder
from mqns.network.fw import RoutingPathSingle
from mqns.network.protocol.link_layer import LinkLayer

DEFAULT_ENTANGLEMENT_ATTEMPTS_PER_ROUTE = 500
DEFAULT_SIM_TIME = 150.0
DEFAULT_LOG_LEVEL = "WARNING"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Comparativa Q-CAST vs Dijkstra")
    parser.add_argument(
        "config_path",
        nargs="?",
        default=os.path.join(os.path.dirname(__file__), '..', 'escenario_basico.json'),
        help="Ruta al JSON del escenario",
    )
    parser.add_argument(
        "--sim-time",
        type=float,
        default=DEFAULT_SIM_TIME,
        help="Tiempo total de simulacion",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=DEFAULT_ENTANGLEMENT_ATTEMPTS_PER_ROUTE,
        help="Intentos de entrelazamiento por ruta para metricas",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=DEFAULT_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Nivel de log",
    )
    parser.add_argument(
        "--controller-node",
        type=str,
        default=None,
        help="Nombre de nodo donde instalar el controlador (por defecto, el primero)",
    )
    return parser.parse_args()

def install_qcast_stack(node, *, controller=None):
    apps = []
    if controller is not None:
        apps.append(controller)
    
    apps.append(LinkLayer())
    
    forwarder = QCastForwarder(k_max=4, ps=1.0, purif_enabled=True, max_purif_rounds=2)
    apps.append(forwarder)
    
    node.add_apps(apps)
    setattr(node, 'forwarder', forwarder)
    return forwarder


args = parse_args()
ENTANGLEMENT_ATTEMPTS_PER_ROUTE = args.attempts
LIMIT_VAL = args.sim_time
log.set_default_level(args.log_level)

config_path = args.config_path
print(f"Cargando topología desde {config_path}...\n")

with open(config_path, "r", encoding="utf-8") as f:
    topo_config = json.load(f)


# =====================================================================
# SIMULACIÓN 1: Q-CAST
# =====================================================================
print("SIMULACIÓN 1: Q-CAST ")

sim_qcast = Simulator(0, LIMIT_VAL)
net_qcast = QuantumNetwork(None)
net_qcast.build_topology_from_json(config_path)
net_qcast.requests.clear()

net_qcast.simulator = sim_qcast
net_qcast.all_nodes = list(net_qcast.nodes)

qcast_timing = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
net_qcast.timing = qcast_timing
qcast_timing.install(net_qcast)

for i, node in enumerate(net_qcast.all_nodes):
    is_controller_node = (node.name == args.controller_node) if args.controller_node else (i == 0)
    if is_controller_node:
        app = QCastController(k_max=2)
        fw_app = install_qcast_stack(node, controller=app)
        setattr(net_qcast, 'controller', app)
    else:
        fw_app = install_qcast_stack(node)

    node.install(sim_qcast)

solicitudes_qcast = []
request_idx = 1
for req in topo_config.get('solicitudes', []):
    src_name = req.get('src')
    dst_name = req.get('dst')
    if src_name and dst_name:
        src = net_qcast.get_node(src_name)
        dst = net_qcast.get_node(dst_name)
        if src is not None and dst is not None:
            req_id = build_request_id(src.name, dst.name, request_idx)
            request_idx += 1
            net_qcast.add_request(src, dst, attr={"req_id": req_id})
            solicitudes_qcast.append({"req_id": req_id, "src": src, "dst": dst})

if not solicitudes_qcast and len(net_qcast.all_nodes) >= 6:
    fallback_nodes = list(net_qcast.nodes)
    req_a = build_request_id(fallback_nodes[1].name, fallback_nodes[-1].name, request_idx)
    request_idx += 1
    req_b = build_request_id(fallback_nodes[2].name, fallback_nodes[5].name, request_idx)
    request_idx += 1
    req_c = build_request_id(fallback_nodes[3].name, fallback_nodes[0].name, request_idx)
    request_idx += 1

    net_qcast.add_request(fallback_nodes[1], fallback_nodes[-1], attr={"req_id": req_a})
    net_qcast.add_request(fallback_nodes[2], fallback_nodes[5], attr={"req_id": req_b})
    net_qcast.add_request(fallback_nodes[3], fallback_nodes[0], attr={"req_id": req_c})
    solicitudes_qcast = [
        {"req_id": req_a, "src": fallback_nodes[1], "dst": fallback_nodes[-1]},
        {"req_id": req_b, "src": fallback_nodes[2], "dst": fallback_nodes[5]},
        {"req_id": req_c, "src": fallback_nodes[3], "dst": fallback_nodes[0]},
    ]

sim_qcast.run()
qcast_resultados = construir_resultados_qcast(getattr(net_qcast, 'controller', None), solicitudes_qcast, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)


# =====================================================================
# SIMULACIÓN 2: DIJKSTRA (con capacidad)
# =====================================================================
print("\nSIMULACIÓN 2: DIJKSTRA ")

sim_dijkstra = Simulator(0, LIMIT_VAL)
net_dijkstra = QuantumNetwork(None)
net_dijkstra.build_topology_from_json(config_path)
net_dijkstra.requests.clear()

net_dijkstra.route = YenRouteAlgorithm(k_paths=3)
net_dijkstra.simulator = sim_dijkstra
net_dijkstra.all_nodes = list(net_dijkstra.nodes)

dijkstra_timing = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
net_dijkstra.timing = dijkstra_timing
dijkstra_timing.install(net_dijkstra)

solicitudes_dijkstra = []
dijkstra_paths = []
request_idx = 1

for req in topo_config.get('solicitudes', []):
    src_name = req.get('src')
    dst_name = req.get('dst')
    if src_name and dst_name:
        src = net_dijkstra.get_node(src_name)
        dst = net_dijkstra.get_node(dst_name)
        if src is not None and dst is not None:
            req_id = build_request_id(src.name, dst.name, request_idx)
            request_idx += 1
            net_dijkstra.add_request(src, dst, attr={"req_id": req_id})
            solicitudes_dijkstra.append({"req_id": req_id, "src": src, "dst": dst})
            rpath = RoutingPathSingle(src_name, dst_name, req_id=request_idx - 1)
            dijkstra_paths.append(rpath)

if not solicitudes_dijkstra and len(net_dijkstra.all_nodes) >= 6:
    fallback_nodes = list(net_dijkstra.nodes)
    req_a = build_request_id(fallback_nodes[1].name, fallback_nodes[-1].name, request_idx)
    request_idx += 1
    req_b = build_request_id(fallback_nodes[2].name, fallback_nodes[5].name, request_idx)
    request_idx += 1
    req_c = build_request_id(fallback_nodes[3].name, fallback_nodes[0].name, request_idx)
    request_idx += 1

    net_dijkstra.add_request(fallback_nodes[1], fallback_nodes[-1], attr={"req_id": req_a})
    net_dijkstra.add_request(fallback_nodes[2], fallback_nodes[5], attr={"req_id": req_b})
    net_dijkstra.add_request(fallback_nodes[3], fallback_nodes[0], attr={"req_id": req_c})
    solicitudes_dijkstra = [
        {"req_id": req_a, "src": fallback_nodes[1], "dst": fallback_nodes[-1]},
        {"req_id": req_b, "src": fallback_nodes[2], "dst": fallback_nodes[5]},
        {"req_id": req_c, "src": fallback_nodes[3], "dst": fallback_nodes[0]},
    ]

controller_dijkstra = None
for i, node in enumerate(net_dijkstra.all_nodes):
    is_controller_node = (node.name == args.controller_node) if args.controller_node else (i == 0)
    if is_controller_node:
        controller_dijkstra = QCastController(k_max=2)
        fw_app = install_qcast_stack(node, controller=controller_dijkstra)
        setattr(net_dijkstra, 'controller', controller_dijkstra)
    else:
        fw_app = install_qcast_stack(node)

    node.install(sim_dijkstra)

print("Ejecutando simulación Dijkstra...")
initialize_virtual_node_capacity(controller_dijkstra, net_dijkstra.all_nodes)
assign_dijkstra_routes_with_capacity(
    net_dijkstra,
    controller_dijkstra,
    solicitudes_dijkstra,
    obtener_prob_y_fidelidad_de_ruta,
)

sim_dijkstra.run()
dijkstra_resultados = construir_resultados_qcast(controller_dijkstra, solicitudes_dijkstra, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)

imprimir_info_rutas_detallada("Q-CAST", getattr(net_qcast, 'controller', None), qcast_resultados, LIMIT_VAL, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)
imprimir_info_rutas_detallada("Dijkstra", getattr(net_dijkstra, 'controller', None), dijkstra_resultados, LIMIT_VAL, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)


# =====================================================================
# SIMULACIÓN 3: DIJKSTRA CLÁSICO (sin capacidad)
# =====================================================================
print("\nSIMULACIÓN 3: DIJKSTRA (sin capacidad)")

sim_classic = Simulator(0, LIMIT_VAL)
net_classic = QuantumNetwork(None)
net_classic.build_topology_from_json(config_path)
net_classic.requests.clear()

net_classic.route = DijkstraRouteAlgorithm()
net_classic.simulator = sim_classic
net_classic.all_nodes = list(net_classic.nodes)

net_classic.build_route()

tim = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
net_classic.timing = tim
tim.install(net_classic)

controller_classic = None
for i, node in enumerate(net_classic.all_nodes):
    is_controller_node = (node.name == args.controller_node) if args.controller_node else (i == 0)
    if is_controller_node:
        controller_classic = QCastController(k_max=2)
        fw_app = install_qcast_stack(node, controller=controller_classic)
        setattr(net_classic, 'controller', controller_classic)
    else:
        fw_app = install_qcast_stack(node)

    node.install(sim_classic)

solicitudes_classic = []
request_idx = 1
initialize_virtual_node_capacity(controller_classic, net_classic.all_nodes)

# Reutilizamos las mismas solicitudes por simplicidad
for idx, req in enumerate(solicitudes_qcast):
    src = net_classic.get_node(req["src"].name)
    dst = net_classic.get_node(req["dst"].name)
    net_classic.add_request(src, dst, attr={"req_id": req["req_id"]})
    solicitudes_classic.append({"req_id": req["req_id"], "src": src, "dst": dst})

assign_dijkstra_routes_with_capacity(
    net_classic,
    controller_classic,
    solicitudes_classic,
    obtener_prob_y_fidelidad_de_ruta,
    enforce_capacity=False,
)
sim_classic.run()

classic_resultados = construir_resultados_qcast(controller_classic, solicitudes_classic, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)
imprimir_info_rutas_detallada("Dijkstra clásico (sin capacidad)", controller_classic, classic_resultados, LIMIT_VAL, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)


# =====================================================================
# SIMULACIÓN 4: DIJKSTRA (distancia física)
# =====================================================================
print("\nSIMULACIÓN 4: DIJKSTRA (distancia física)")

sim_dist = Simulator(0, LIMIT_VAL)
net_dist = QuantumNetwork(None)
net_dist.build_topology_from_json(config_path)
net_dist.requests.clear()

net_dist.route = DijkstraDistanceRouteAlgorithm()
net_dist.simulator = sim_dist
net_dist.all_nodes = list(net_dist.nodes)

net_dist.build_route()

tim2 = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
net_dist.timing = tim2
tim2.install(net_dist)

controller_dist = None
for i, node in enumerate(net_dist.all_nodes):
    is_controller_node = (node.name == args.controller_node) if args.controller_node else (i == 0)
    if is_controller_node:
        controller_dist = QCastController(k_max=2)
        fw_app = install_qcast_stack(node, controller=controller_dist)
        setattr(net_dist, 'controller', controller_dist)
    else:
        fw_app = install_qcast_stack(node)

    node.install(sim_dist)

solicitudes_dist = []
initialize_virtual_node_capacity(controller_dist, net_dist.all_nodes)
for idx, req in enumerate(solicitudes_qcast):
    src = net_dist.get_node(req["src"].name)
    dst = net_dist.get_node(req["dst"].name)
    net_dist.add_request(src, dst, attr={"req_id": req["req_id"]})
    solicitudes_dist.append({"req_id": req["req_id"], "src": src, "dst": dst})

assign_dijkstra_routes_with_capacity(
    net_dist,
    controller_dist,
    solicitudes_dist,
    obtener_prob_y_fidelidad_de_ruta,
    enforce_capacity=False,
)
sim_dist.run()

dist_resultados = construir_resultados_qcast(controller_dist, solicitudes_dist, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)
imprimir_info_rutas_detallada("Dijkstra distancia (sin capacidad)", controller_dist, dist_resultados, LIMIT_VAL, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)


# =====================================================================
# SIMULACIÓN 5: Q-CAST (con fidelidad)
# =====================================================================
print("\nSIMULACIÓN 5: Q-CAST (con fidelidad)")

sim_qcast_fid = Simulator(0, LIMIT_VAL)
net_qcast_fid = QuantumNetwork(None)
net_qcast_fid.build_topology_from_json(config_path)
net_qcast_fid.requests.clear()

net_qcast_fid.simulator = sim_qcast_fid
net_qcast_fid.all_nodes = list(net_qcast_fid.nodes)

qcast_fid_timing = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
net_qcast_fid.timing = qcast_fid_timing
qcast_fid_timing.install(net_qcast_fid)

for i, node in enumerate(net_qcast_fid.all_nodes):
    is_controller_node = (node.name == args.controller_node) if args.controller_node else (i == 0)
    if is_controller_node:
        app = QCastFidelityController(k_max=2)
        fw_app = install_qcast_stack(node, controller=app)
        setattr(net_qcast_fid, 'controller', app)
    else:
        fw_app = install_qcast_stack(node)

    node.install(sim_qcast_fid)

solicitudes_qcast_fid = []
for idx, req in enumerate(solicitudes_qcast):
    src = net_qcast_fid.get_node(req["src"].name)
    dst = net_qcast_fid.get_node(req["dst"].name)
    net_qcast_fid.add_request(src, dst, attr={"req_id": req["req_id"]})
    solicitudes_qcast_fid.append({"req_id": req["req_id"], "src": src, "dst": dst})

sim_qcast_fid.run()
qcast_fid_resultados = construir_resultados_qcast(getattr(net_qcast_fid, 'controller', None), solicitudes_qcast_fid, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)

imprimir_info_rutas_detallada("Q-CAST (con fidelidad)", getattr(net_qcast_fid, 'controller', None), qcast_fid_resultados, LIMIT_VAL, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)


# =====================================================================
# COMPARATIVA GLOBAL DE THROUGHPUT, PROBABILIDAD Y FIDELIDAD
# =====================================================================

qcast_total = sum(r["successes"] for r in qcast_resultados)
qcast_throughput = qcast_total / LIMIT_VAL

# LA LÍNEA QUE FALTABA (Dijkstra con capacidad)
dijkstra_total = sum(r["successes"] for r in dijkstra_resultados)
dijkstra_throughput = dijkstra_total / LIMIT_VAL

classic_total = sum(r["successes"] for r in classic_resultados)
classic_throughput = classic_total / LIMIT_VAL

dist_total = sum(r["successes"] for r in dist_resultados)
dist_throughput = dist_total / LIMIT_VAL

qcast_fid_total = sum(r["successes"] for r in qcast_fid_resultados)
qcast_fid_throughput = qcast_fid_total / LIMIT_VAL

# Calcular probabilidades de éxito globales
num_requests = len(qcast_resultados)
total_attempts = num_requests * ENTANGLEMENT_ATTEMPTS_PER_ROUTE

qcast_success_prob = qcast_total / total_attempts
dijkstra_success_prob = dijkstra_total / total_attempts
classic_success_prob = classic_total / total_attempts
dist_success_prob = dist_total / total_attempts
qcast_fid_success_prob = qcast_fid_total / total_attempts

# Calcular fidelidades medias
def calcular_fidelidad_media(resultados):
    if not resultados:
        return 0.0
    fidelities = []
    for r in resultados:
        of = r.get("observed_fidelity", 0.0)
        fidelities.append(of if of > 0 else r.get("route_fidelity", 0.0))
    return sum(fidelities) / len(fidelities)

qcast_avg_fidelity = calcular_fidelidad_media(qcast_resultados)
dijkstra_avg_fidelity = calcular_fidelidad_media(dijkstra_resultados)
classic_avg_fidelity = calcular_fidelidad_media(classic_resultados)
dist_avg_fidelity = calcular_fidelidad_media(dist_resultados)
qcast_fid_avg_fidelity = calcular_fidelidad_media(qcast_fid_resultados)

print("\nMÉTRICAS GLOBALES (TODOS LOS ALGORITMOS)")
print(f"Q-CAST:")
print(f"  - Throughput:         {qcast_throughput:.4f} EPS ({qcast_total} Éxitos)")
print(f"  - Prob. Éxito:        {qcast_success_prob:.4f} ({qcast_total}/{total_attempts})")
print(f"  - Fidelidad media:    {qcast_avg_fidelity:.4f}")

print(f"\nDijkstra con capacidad:")
print(f"  - Throughput:         {dijkstra_throughput:.4f} EPS ({dijkstra_total} Éxitos)")
print(f"  - Prob. Éxito:        {dijkstra_success_prob:.4f} ({dijkstra_total}/{total_attempts})")
print(f"  - Fidelidad media:    {dijkstra_avg_fidelity:.4f}")

print(f"\nDijkstra clásico:")
print(f"  - Throughput:         {classic_throughput:.4f} EPS ({classic_total} Éxitos)")
print(f"  - Prob. Éxito:        {classic_success_prob:.4f} ({classic_total}/{total_attempts})")
print(f"  - Fidelidad media:    {classic_avg_fidelity:.4f}")

print(f"\nDijkstra distancia:")
print(f"  - Throughput:         {dist_throughput:.4f} EPS ({dist_total} Éxitos)")
print(f"  - Prob. Éxito:        {dist_success_prob:.4f} ({dist_total}/{total_attempts})")
print(f"  - Fidelidad media:    {dist_avg_fidelity:.4f}")

print(f"\nQ-CAST (con fidelidad):")
print(f"  - Throughput:         {qcast_fid_throughput:.4f} EPS ({qcast_fid_total} Éxitos)")
print(f"  - Prob. Éxito:        {qcast_fid_success_prob:.4f} ({qcast_fid_total}/{total_attempts})")
print(f"  - Fidelidad media:    {qcast_fid_avg_fidelity:.4f}")

algoritmos = [
    "Dijkstra\n#salts",
    "Dijkstra\ncapacitat",
    "Dijkstra\ndistáncia",
    "Q-CAST",
    "Q-CAST\n(Fidelidad)",
]
throughputs = [
    classic_throughput,
    dijkstra_throughput,
    dist_throughput,
    qcast_throughput,
    qcast_fid_throughput,
]
success_probs = [
    classic_success_prob,
    dijkstra_success_prob,
    dist_success_prob,
    qcast_success_prob,
    qcast_fid_success_prob,
]
avg_fidelities = [
    classic_avg_fidelity,
    dijkstra_avg_fidelity,
    dist_avg_fidelity,
    qcast_avg_fidelity,
    qcast_fid_avg_fidelity,
]

output_dir = os.path.join(os.path.dirname(__file__), "..", "outputs")
os.makedirs(output_dir, exist_ok=True)

# Gráfico 1: Throughput Global
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
print(f"\nGráfico de Throughput guardado en: {throughput_path}")

# Gráfico 2: Probabilidad de Éxito Global
plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, success_probs, color="forestgreen")
plt.ylabel("Probabilidad de Éxito")
plt.title("Comparativa de Probabilidad de Éxito")
plt.ylim(0, max(success_probs) * 1.15)
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
print(f" Gráfico de Probabilidad de Éxito guardado en: {success_prob_path}")

# Gráfico 3: Fidelidad Media
plt.figure(figsize=(10, 5))
bars = plt.bar(algoritmos, avg_fidelities, color="forestgreen")
plt.ylabel("Fidelidad media")
plt.title("Comparativa de Fidelidad Media")
plt.ylim(0, max(avg_fidelities) * 1.15)
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
print(f"Gráfico de Fidelidad Media guardado en: {avg_fidelity_path}")

dibujar_escenario(net_qcast)