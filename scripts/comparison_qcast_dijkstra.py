import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from mqns.simulator import Simulator
from mqns.network.network.timing import TimingModeSyncQCast
from mqns.network.network import QuantumNetwork, dibujar_escenario
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
from mqns.network.qcast.controller import QCastController
from mqns.network.qcast.forwarder import QCastForwarder
from mqns.network.fw import RoutingPathSingle

ENTANGLEMENT_ATTEMPTS_PER_ROUTE = 75



LIMIT_VAL = 30.0
log.set_default_level("INFO")

config_path = os.path.join(os.path.dirname(__file__), '..', 'escenario_basico.json')
if len(sys.argv) > 1:
    config_path = sys.argv[1]
print(f"Cargando topología desde {config_path}...\n")

with open(config_path, "r", encoding="utf-8") as f:
    topo_config = json.load(f)


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
    node.install(sim_qcast)
    
    if i == 0:
        app = QCastController(k_max=2)
        setattr(node, 'forwarder', app)
        app.install(node)
        setattr(net_qcast, 'controller', app)
        setattr(app, 'net', net_qcast)
        node.handle = app.handle 
    else:
        app = QCastForwarder(k_max=2)
        setattr(node, 'forwarder', app)
        app.install(node)
        node.handle = app.handle 

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


print("SIMULACIÓN 2: DIJKSTRA ")


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
    dijkstra_paths = [
        RoutingPathSingle(fallback_nodes[1].name, fallback_nodes[-1].name, req_id=0),
        RoutingPathSingle(fallback_nodes[2].name, fallback_nodes[5].name, req_id=1),
        RoutingPathSingle(fallback_nodes[3].name, fallback_nodes[0].name, req_id=2),
    ]

controller_dijkstra = None
for i, node in enumerate(net_dijkstra.all_nodes):
    node.install(sim_dijkstra)
    
    if i == 0:
        controller_dijkstra = QCastController(k_max=2)
        setattr(node, 'forwarder', controller_dijkstra)
        controller_dijkstra.install(node)
        setattr(net_dijkstra, 'controller', controller_dijkstra)
        controller_dijkstra.paths = dijkstra_paths

        initialize_virtual_node_capacity(controller_dijkstra, net_dijkstra.all_nodes)
        setattr(net_dijkstra, 'controller', controller_dijkstra)
        # Build routing tables before assigning per-request routes.
        try:
            net_dijkstra.build_route()
            controller_dijkstra.install(node)
        except Exception:
            pass
    else:
        # Use QCastForwarder so it runs the same P3/P4 sampling as Q-CAST
        fw_app = QCastForwarder(k_max=2)
        setattr(node, 'forwarder', fw_app)
        fw_app.install(node)
        node.handle = fw_app.handle

print("Ejecutando simulación Dijkstra...")
assign_dijkstra_routes_with_capacity(
    net_dijkstra,
    controller_dijkstra,
    solicitudes_dijkstra,
    obtener_prob_y_fidelidad_de_ruta,
)

sim_dijkstra.run()



dijkstra_resultados = construir_resultados_qcast(controller_dijkstra, solicitudes_dijkstra, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)

imprimir_info_rutas_detallada(
    "Q-CAST",
    getattr(net_qcast, 'controller', None),
    qcast_resultados,
    LIMIT_VAL,
    ENTANGLEMENT_ATTEMPTS_PER_ROUTE,
)
imprimir_info_rutas_detallada(
    "Dijkstra",
    getattr(net_dijkstra, 'controller', None),
    dijkstra_resultados,
    LIMIT_VAL,
    ENTANGLEMENT_ATTEMPTS_PER_ROUTE,
)

qcast_total = sum(r["successes"] for r in qcast_resultados)
qcast_throughput = qcast_total / LIMIT_VAL

print("MÉTRICAS GLOBALES")
print(f"Q-CAST:")
print(f"  - Total Éxitos E2E:  {qcast_total} entrelazamientos")
print(f"  - Throughput Global: {qcast_throughput:.4f} EPS")
dijkstra_total = sum(r["successes"] for r in dijkstra_resultados)
dijkstra_throughput = dijkstra_total / LIMIT_VAL

print(f"\nDijkstra:")
print(f"  - Total Éxitos E2E:  {dijkstra_total} entrelazamientos")
print(f"  - Throughput Global: {dijkstra_throughput:.4f} EPS")


dibujar_escenario(net_qcast)


# -----------------------------------------------------------------------------
# SIMULACIONES ADICIONALES: Dijkstra clásico (sin considerar capacidad)
# y Dijkstra por distancia (sin considerar capacidad)
# -----------------------------------------------------------------------------


print("\nSIMULACIÓN 3: DIJKSTRA (sin capacidad)")
sim_classic = Simulator(0, LIMIT_VAL)
net_classic = QuantumNetwork(None)
net_classic.build_topology_from_json(config_path)
net_classic.requests.clear()

net_classic.route = DijkstraRouteAlgorithm()

net_classic.simulator = sim_classic
net_classic.all_nodes = list(net_classic.nodes)

tim = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
net_classic.timing = tim
tim.install(net_classic)

controller_classic = None
for i, node in enumerate(net_classic.all_nodes):
    node.install(sim_classic)
    if i == 0:
        controller_classic = QCastController(k_max=2)
        setattr(node, 'forwarder', controller_classic)
        controller_classic.install(node)
        setattr(net_classic, 'controller', controller_classic)
        setattr(controller_classic, 'net', net_classic)
        node.handle = controller_classic.handle
    else:
        fw_app = QCastForwarder(k_max=2)
        setattr(node, 'forwarder', fw_app)
        fw_app.install(node)
        node.handle = fw_app.handle

solicitudes_classic = []
request_idx = 1
for req in topo_config.get('solicitudes', []):
    src_name = req.get('src')
    dst_name = req.get('dst')
    if src_name and dst_name:
        src = net_classic.get_node(src_name)
        dst = net_classic.get_node(dst_name)
        if src is not None and dst is not None:
            req_id = build_request_id(src.name, dst.name, request_idx)
            request_idx += 1
            net_classic.add_request(src, dst, attr={"req_id": req_id})
            solicitudes_classic.append({"req_id": req_id, "src": src, "dst": dst})

assign_dijkstra_routes_with_capacity(
    net_classic,
    controller_classic,
    solicitudes_classic,
    obtener_prob_y_fidelidad_de_ruta,
    enforce_capacity=False,
)
sim_classic.run()

classic_resultados = construir_resultados_qcast(controller_classic, solicitudes_classic, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)
imprimir_info_rutas_detallada(
    "Dijkstra clásico (sin capacidad)",
    controller_classic,
    classic_resultados,
    LIMIT_VAL,
    ENTANGLEMENT_ATTEMPTS_PER_ROUTE,
)


print("\nSIMULACIÓN 4: DIJKSTRA (distancia física)")
sim_dist = Simulator(0, LIMIT_VAL)
net_dist = QuantumNetwork(None)
net_dist.build_topology_from_json(config_path)
net_dist.requests.clear()

net_dist.route = DijkstraDistanceRouteAlgorithm()

net_dist.simulator = sim_dist
net_dist.all_nodes = list(net_dist.nodes)

tim2 = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
net_dist.timing = tim2
tim2.install(net_dist)

controller_dist = None
for i, node in enumerate(net_dist.all_nodes):
    node.install(sim_dist)
    if i == 0:
        controller_dist = QCastController(k_max=2)
        setattr(node, 'forwarder', controller_dist)
        controller_dist.install(node)
        setattr(net_dist, 'controller', controller_dist)
        setattr(controller_dist, 'net', net_dist)
        node.handle = controller_dist.handle
    else:
        fw_app = QCastForwarder(k_max=2)
        setattr(node, 'forwarder', fw_app)
        fw_app.install(node)
        node.handle = fw_app.handle

solicitudes_dist = []
request_idx = 1
for req in topo_config.get('solicitudes', []):
    src_name = req.get('src')
    dst_name = req.get('dst')
    if src_name and dst_name:
        src = net_dist.get_node(src_name)
        dst = net_dist.get_node(dst_name)
        if src is not None and dst is not None:
            req_id = build_request_id(src.name, dst.name, request_idx)
            request_idx += 1
            net_dist.add_request(src, dst, attr={"req_id": req_id})
            solicitudes_dist.append({"req_id": req_id, "src": src, "dst": dst})

assign_dijkstra_routes_with_capacity(
    net_dist,
    controller_dist,
    solicitudes_dist,
    obtener_prob_y_fidelidad_de_ruta,
    enforce_capacity=False,
)
sim_dist.run()

dist_resultados = construir_resultados_qcast(controller_dist, solicitudes_dist, ENTANGLEMENT_ATTEMPTS_PER_ROUTE)
imprimir_info_rutas_detallada(
    "Dijkstra distancia (sin capacidad)",
    controller_dist,
    dist_resultados,
    LIMIT_VAL,
    ENTANGLEMENT_ATTEMPTS_PER_ROUTE,
)
