import sys
import os
import json
import matplotlib.pyplot as plt
import networkx as nx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from mqns.simulator import Simulator
from mqns.network.network.timing import TimingModeSyncQCast
from mqns.network.network import QuantumNetwork
from mqns.network.route import DijkstraRouteAlgorithm
from mqns.utils import log
from mqns.network.qcast.controller import QCastController
from mqns.network.qcast.forwarder import QCastForwarder
from mqns.network.network import cargar_topologia_desde_json, guardar_configuracion




def dibujar_escenario(net):
    G = nx.Graph()

    nodos_lista = net.nodes if isinstance(net.nodes, list) else list(net.nodes.values())

    labels_nodos = {}
    for node in nodos_lista:
        cap = getattr(node.memory, 'capacity', 10)
        G.add_node(node.name, capacity=cap)
        labels_nodos[node.name] = f"{node.name}\n(W:{cap})"

    channels = getattr(net, 'qchannels', getattr(net, '_qchannels', []))
    labels_enlaces = {}
    for qc in channels:
        if hasattr(qc, 'node_list'):
            u_name, v_name = qc.node_list[0].name, qc.node_list[1].name
        else:
            u_name, v_name = qc.node1.name, qc.node2.name

        G.add_edge(u_name, v_name)

        prob = getattr(qc, 'success_prob', 1.0)
        fid = getattr(qc, '_fidelity', None)
        if fid is None:
            transfer_error = getattr(qc, 'transfer_error', None)
            if transfer_error is not None and hasattr(transfer_error, 'p_survival'):
                fid = (3 * transfer_error.p_survival + 1) / 4
            else:
                fid = 0.99
        length = getattr(qc, 'length', None)
        length_label = f"L:{length:.1f}km\n" if length is not None and length > 0 else ""
        labels_enlaces[(u_name, v_name)] = f"{length_label}P:{prob:.4f}\nF:{fid:.4f}"

    pos = nx.spring_layout(G, seed=42)
    nx.draw(G, pos, with_labels=True, labels=labels_nodos, node_size=600, node_color='lightblue', font_weight='bold')
    nx.draw_networkx_edge_labels(G, pos, edge_labels=labels_enlaces, font_color='red', font_size=8)

    plt.title("Topología de Red: Capacidad (W) en Nodos y Probabilidad (P) en Enlaces")
    plt.axis('off')
    plt.tight_layout()
    plt.show()


def obtener_capacidades_residuales(net, controller):
    if controller is not None and getattr(controller, 'node_remaining_capacity', None):
        return dict(controller.node_remaining_capacity)

    return {
        node: getattr(getattr(node, 'memory', None), 'capacity', 1)
        for node in net.nodes
    }


def ejecutar_dijkstra_capacidad(net, solicitudes, capacities):
    algoritmo = DijkstraRouteAlgorithm()
    capacidades = dict(capacities)
    total_exitos = 0

    print("\n INICIANDO FASE DIJKSTRA...")
    for src, dst in solicitudes:
        algoritmo.node_capacities = dict(capacidades)
        algoritmo.build(net.nodes, net.qchannels)
        resultado = algoritmo.query(src, dst)

        if resultado:
            ruta = resultado[0].route
            nombres_ruta = [node.name for node in ruta]
            total_exitos += 1

            for node in ruta:
                capacidades[node] = max(0, capacidades.get(node, 0) - 1)

            print(f"Dijkstra: {src.name} -> {dst.name}")
            print(f"  - Ruta: {' -> '.join(nombres_ruta)}")
            print(f"  - Métrica: {resultado[0].metric:.4f}")
        else:
            print(f"Dijkstra: no se encontró ruta para {src.name} -> {dst.name}")

    return total_exitos, capacidades


LIMIT_VAL = 10.0
log.set_default_level("INFO")
sim = Simulator(0, LIMIT_VAL)

config_path = os.path.join(os.path.dirname(__file__), '..', 'escenario_basico.json')
if len(sys.argv) > 1:
    config_path = sys.argv[1]
print(f"Cargando topología desde {config_path}...\n")

topo_config = cargar_topologia_desde_json(config_path)

net = QuantumNetwork(None)
net.build_topology_from_json(config_path)

nodos_lista = list(net.nodes)

net.simulator = sim
net.all_nodes = list(net.nodes)

qcast_timing = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
net.timing = qcast_timing
qcast_timing.install(net)

nodes = net.all_nodes
for i, node in enumerate(nodes):
    node.install(sim)
    
    if i == 0:
        app = QCastController(k_max=2)
        setattr(node, 'forwarder', app)
        app.install(node)
        setattr(net, 'controller', app)
        setattr(app, 'net', net)
        node.handle = app.handle 
    else:
        app = QCastForwarder(k_max=2)
        setattr(node, 'forwarder', app)
        app.install(node)
        node.handle = app.handle 

solicitudes = []
for req in topo_config.get('solicitudes', []):
    src_name = req.get('src')
    dst_name = req.get('dst')
    if src_name and dst_name:
        src = net.get_node(src_name)
        dst = net.get_node(dst_name)
        if src is not None and dst is not None:
            net.add_request(src, dst)
            solicitudes.append((src, dst))

if not solicitudes and len(nodes) >= 6:
    fallback_nodes = list(net.nodes)
    net.add_request(fallback_nodes[1], fallback_nodes[-1])
    net.add_request(fallback_nodes[2], fallback_nodes[5])
    net.add_request(fallback_nodes[2], fallback_nodes[3])
    solicitudes = [
        (fallback_nodes[1], fallback_nodes[-1]),
        (fallback_nodes[2], fallback_nodes[5]),
        (fallback_nodes[3], fallback_nodes[0]),
    ]



print("\n INICIANDO SIMULACIÓN...")
sim.run()


capacidades_residuales = obtener_capacidades_residuales(net, getattr(net, 'controller', None))
dijkstra_exitos, capacidades_residuales = ejecutar_dijkstra_capacidad(net, solicitudes, capacidades_residuales)

controlador = getattr(net, 'controller', None)
controller = net.controller
total_exitos = getattr(controller, 'successful_requests', 0)
throughput = total_exitos / LIMIT_VAL 
dijkstra_throughput = dijkstra_exitos / LIMIT_VAL

print(f"Total Éxitos E2E:    {total_exitos} Entrelazamientos")
print(f"Throughput Global:   {throughput:.2f} EPS (Entrelazamientos por segundo)")
print(f"Total Éxitos Dijkstra: {dijkstra_exitos} Entrelazamientos")
print(f"Throughput Dijkstra:  {dijkstra_throughput:.2f} EPS (Entrelazamientos por segundo)")
dibujar_escenario(net)
