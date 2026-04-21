import sys
import os
import json
import matplotlib.pyplot as plt
import networkx as nx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from mqns.mqns.simulator import Simulator
from mqns.mqns.network.network.timing import TimingModeSyncQCast
from mqns.mqns.network.network import QuantumNetwork
from mqns.mqns.utils import log
from mqns.mqns.network.qcast.controller import QCastController
from mqns.mqns.network.qcast.forwarder import QCastForwarder
from mqns.mqns.network.network import cargar_topologia_desde_json, guardar_configuracion




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
net.all_nodes = nodos_lista

net.simulator = sim
net.all_nodes = list(net.nodes)

qcast_timing = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
net.timing_mode = qcast_timing
qcast_timing.install(net)

nodes = net.all_nodes
for i, node in enumerate(nodes):
    node.install(sim)
    
    if i == 0:
        app = QCastController(k_max=2)
        node.forwarder = app
        app.install(node)
        net.controller = app
        app.net = net
        node.handle = app.handle 
    else:
        app = QCastForwarder(k_max=2)
        node.forwarder = app
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
    net.add_request(nodes[1], nodes[-1])
    net.add_request(nodes[2], nodes[5])
    net.add_request(nodes[2], nodes[3])
    solicitudes = [
        (nodes[1], nodes[-1]),
        (nodes[2], nodes[5]),
        (nodes[3], nodes[0]),
    ]



print("\n INICIANDO SIMULACIÓN...")
sim.run()


controlador = getattr(net, 'controller', None)
controller = net.controller
total_exitos = getattr(controller, 'successful_requests', 0)
throughput = total_exitos / LIMIT_VAL 

print(f"Total Éxitos E2E:    {total_exitos} Entrelazamientos")
print(f"Throughput Global:   {throughput:.2f} EPS (Entrelazamientos por segundo)")
dibujar_escenario(net)
