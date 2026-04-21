import sys
import os
import random
import matplotlib.pyplot as plt 
import networkx as nx
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from mqns.mqns.simulator import Simulator
from mqns.mqns.network.network.timing import TimingModeSyncQCast
from mqns.mqns.network.topology.randomtopo import RandomTopology
from mqns.mqns.network.network import QuantumNetwork
from mqns.mqns.utils import log

from mqns.mqns.network.qcast.controller import QCastController
from mqns.mqns.network.qcast.forwarder import QCastForwarder

from mqns.mqns.network.builder import NetworkBuilder

# Función para construir topología con modelos físicos realistas
def build_topology_with_physical_models(num_nodes=8, num_channels=14):
    """
    Construye una topología aleatoria usando modelos físicos en lugar de valores aleatorios.
    Los parámetros físicos calculan automáticamente probabilidades y fidelidades realistas.
    """

    mem_capacity = {}
    for i in range(num_nodes):
        node_id = f"n{i+1}"
        capacity = random.randint(5, 15)  
        mem_capacity[node_id] = capacity

    channels = []
    nodes_list = list(mem_capacity.keys())

    for _ in range(num_channels):
        u = random.choice(nodes_list)
        v = random.choice(nodes_list)
        while u == v:  
            v = random.choice(nodes_list)

        distance = random.uniform(1.0, 10.0)
        channels.append((f"{u}-{v}", distance))

    builder = NetworkBuilder()

    eta_s = 0.7
    eta_d = 0.8

    builder.topo(
        mem_capacity=mem_capacity,
        channels=channels,
        channel_capacity=1,  # capacidad de memoria por canal
        fiber_alpha=0.2,     # pérdida de fibra en dB/km (atenuación)
        fiber_error="DEPOLAR:0.01",  # modelo de error cuántico en la fibra
        link_arch="SIM",     # arquitectura de enlace para generar EPR
        eta_s=eta_s,           # eficiencia de la fuente de fotones
        eta_d=eta_d,           # eficiencia del detector
        tau_0=1e-6,          # tiempo de preparación local (s)
        reset_time=1e-6,     # intervalo mínimo entre intentos (s)
        t_cohere=0.02,       # tiempo de coherencia de memoria (s)
    )

    net = builder.make_network()


    for qc in getattr(net, 'qchannels', getattr(net, '_qchannels', [])):
        if hasattr(qc, 'link_arch'):
            qc.success_prob = qc.link_arch._compute_success_prob(
                length=getattr(qc, 'length', 0.0),
                alpha=getattr(qc, 'alpha', 0.0),
                eta_s=eta_s,
                eta_d=eta_d,
            )
            if hasattr(qc, 'drop_rate'):
                qc.drop_rate = 1.0 - qc.success_prob

    return net



def dibujar_escenario(net):
    G = nx.Graph()
    
    nodos_lista = net.nodes if isinstance(net.nodes, list) else list(net.nodes.values())

    labels_nodos = {}
    for node in nodos_lista:
        cap = getattr(node.memory, 'capacity', 10)
        G.add_node(node.name, capacity=cap)
        labels_nodos[node.name] = f"{node.name}\n(W:{cap})"
    
    channels = getattr(net, 'qchannels', getattr(net, '_qchannels', []))
    for qc in channels:
        if hasattr(qc, 'node_list'):
            u_name, v_name = qc.node_list[0].name, qc.node_list[1].name
        else:
            u_name, v_name = qc.node1.name, qc.node2.name
            
        prob = getattr(qc, 'success_prob', 1.0)
        G.add_edge(u_name, v_name, weight=prob)

    pos = nx.spring_layout(G, seed=42)
    
    plt.figure(figsize=(12, 8))

    nx.draw_networkx_nodes(G, pos, node_size=1500, node_color='lightblue', edgecolors='black')
    nx.draw_networkx_labels(G, pos, labels=labels_nodos, font_size=9, font_weight='bold')

    nx.draw_networkx_edges(G, pos, width=2, alpha=0.5)
    
    labels_enlaces = {}
    for qc in channels:
        if hasattr(qc, 'node_list'):
            u_name, v_name = qc.node_list[0].name, qc.node_list[1].name
        else:
            u_name, v_name = qc.node1.name, qc.node2.name
        
        prob = getattr(qc, 'success_prob', 1.0)
        fid = getattr(qc, '_fidelity', None)
        if fid is None:
            transfer_error = getattr(qc, 'transfer_error', None)
            if transfer_error is not None and hasattr(transfer_error, 'p_survival'):
                fid = (3 * transfer_error.p_survival + 1) / 4
            else:
                fid = 0.99
        length = getattr(qc, 'length', None)
        length_label = f"L:{length:.1f}km\n" if length is not None else ""
        labels_enlaces[(u_name, v_name)] = f"{length_label}P:{prob:.4f}\nF:{fid:.4f}"
    nx.draw_networkx_edge_labels(G, pos, edge_labels=labels_enlaces, font_color='red', font_size=8)

    plt.title("Topología de Red: Capacidad (W) en Nodos y Probabilidad (P) en Enlaces")
    plt.axis('off')
    plt.tight_layout()
    plt.show()

def guardar_configuracion(net, solicitudes, controller=None, filename="escenario_aleatorio.json"):
    print(f"\nGuardando configuración en {filename}...")
    
    config = {
        "nodos": [],
        "enlaces": [],
        "solicitudes": []
    }
    
    nodos_lista = net.nodes if isinstance(net.nodes, list) else list(net.nodes.values())
    
    for node in nodos_lista:
        config["nodos"].append({"id": node.name, "capacity": getattr(node.memory, 'capacity', 10)})
    
    channels = getattr(net, 'qchannels', getattr(net, '_qchannels', []))
    for qc in channels:
        if hasattr(qc, 'node_list'):
            u_name, v_name = qc.node_list[0].name, qc.node_list[1].name
        else:
            u_name, v_name = qc.node1.name, qc.node2.name
            
        config["enlaces"].append({"u": u_name, "v": v_name, "prob": getattr(qc, 'success_prob', 1.0), "fidelity": getattr(qc, '_fidelity', 0.99)})

    if controller is not None and hasattr(controller, 'request_route_info'):
        for src_node, dst_node in solicitudes:
            req_id = build_request_id(src_node.name, dst_node.name)
            info = controller.request_route_info.get(req_id)
            if info is not None:
                config["solicitudes"].append({
                    "req_id": req_id,
                    "src": info["src"],
                    "dst": info["dst"],
                    "route": info["route"],
                    "hops": info["hops"],
                    "route_success_prob": info["route_success_prob"],
                    "route_fidelity": info["route_fidelity"],
                    "route_width": info["width"],
                    "success": controller.request_success.get(req_id, False),
                })
            else:
                config["solicitudes"].append({
                    "req_id": req_id,
                    "src": src_node.name,
                    "dst": dst_node.name,
                    "route": None,
                    "hops": 0,
                    "route_success_prob": 0.0,
                    "route_fidelity": 0.0,
                    "route_width": 0,
                    "success": False,
                })
    else:
        for src_node, dst_node in solicitudes:
            config["solicitudes"].append({"src": src_node.name, "dst": dst_node.name})

    with open(filename, "w", encoding='utf-8') as f:
        json.dump(config, f, indent=4)
    print(f"Escenario exportado correctamente.")


def build_request_id(src_name, dst_name):
    return f"REQ_{src_name}_TO_{dst_name}"


LIMIT_VAL = 10.0
log.set_default_level("INFO") 
sim = Simulator(0, LIMIT_VAL) 

net = build_topology_with_physical_models(num_nodes=8, num_channels=14)

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

if len(nodes) >= 6:
    net.add_request(nodes[1], nodes[-1]) # n2 -> n8
    net.add_request(nodes[2], nodes[5])
    net.add_request(nodes[2], nodes[3])


solicitudes = []
if len(nodes) >= 6:
    solicitudes = [
        (nodes[1], nodes[-1]), 
        (nodes[2], nodes[5]), 
        (nodes[3], nodes[0])   
    ]



print("\n INICIANDO SIMULACIÓN...")
sim.run()


controlador = getattr(net, 'controller', None)
controller = net.controller
total_exitos = getattr(controller, 'successful_requests', 0)
throughput = total_exitos / LIMIT_VAL 

print(f"Total Éxitos E2E:    {total_exitos} Entrelazamientos")
print(f"Throughput Global:   {throughput:.2f} EPS (Entrelazamientos por segundo)")
guardar_configuracion(net, solicitudes, controlador)
dibujar_escenario(net)


