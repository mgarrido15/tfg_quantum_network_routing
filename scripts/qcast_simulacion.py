import sys
import os
import json
import matplotlib.pyplot as plt

# Aseguramos que MQNS está en el path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from typing import cast
from mqns.simulator import Simulator, Time
from mqns.network.network.timing import TimingModeSyncQCast, TimingPhaseEvent, TimingPhase
from mqns.network.network.network import QuantumNetwork
from mqns.network.network.reporting import (
    build_request_id,
    construir_resultados_qcast,
    imprimir_info_rutas_detallada,
)
from mqns.utils import log
from mqns.network.qcast.controller import QCastController
from mqns.network.qcast.forwarder import QCastForwarder
from mqns.network.protocol.link_layer import LinkLayer, LinkLayerCounters

# Configuración básica
DEFAULT_ATTEMPTS = 500
LIMIT_VAL = 100.0
log.set_default_level("DEBUG") # DEBUG para ver los fotones en consola

def install_qcast_stack(node, net, *, controller=None, purif_enabled=True):
    # En lugar de una LinkLayer vacía, necesitamos inicializarla con los canales del nodo
    # MQNS usa el builder para configurar esto correctamente
    link_layer = LinkLayer()
    
    # Esto es crucial: el Forwarder necesita acceso al controlador
    forwarder = QCastForwarder(k_max=2, purif_enabled=purif_enabled)
    setattr(forwarder, 'swapping_enabled', True)
    
    # Añadimos las apps
    node.add_apps([link_layer, forwarder])
        
    setattr(node, 'forwarder', forwarder)
    return forwarder

def run_qcast_sim():
    config_path = os.path.join(os.path.dirname(__file__), '..', 'escenario_basico.json')
    with open(config_path, "r", encoding="utf-8") as f:
        topo_config = json.load(f)

    sim = Simulator(0, LIMIT_VAL, accuracy=1000000)
    net = QuantumNetwork(None)
    net.build_topology_from_json(config_path)
    net.requests.clear()
    
    # 1. Instalar el controlador ANTES de instalar los nodos
    ctrl = QCastController(k_max=2)
    setattr(net, 'controller', ctrl)
    setattr(ctrl, 'net', net)
    if net.nodes:
        net.nodes[0].add_apps(ctrl)
    net.simulator = sim
    
    # 2. Registrar las solicitudes en la red
    # La red debe tener el controlador YA ASIGNADO antes de añadir peticiones
    solicitudes = []
    for idx, req in enumerate(topo_config.get('solicitudes', [])):
        src, dst = net.get_node(req['src']), net.get_node(req['dst'])
        if src and dst:
            req_id = build_request_id(src.name, dst.name, idx)
            net.add_request(src, dst, {"req_id": req_id})
            solicitudes.append({"src": src, "dst": dst})
            
    # 3. Instalar nodos (con el controlador ya activo en la red)
    for node in net.nodes:
        # Pasamos el ctrl directamente al stack
        fwd = install_qcast_stack(node, net, controller=ctrl)
        setattr(fwd, 'swapping_enabled', True)
        node.install(sim)
        
    # 4. Inyección de fase inicial
    # IMPORTANTE: El TimingMode debe estar instalado para que haya eventos
    qcast_timing = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
    net.timing = qcast_timing
    qcast_timing.install(net)

    # Parche de compatibilidad para el protocolo de sincronización
    if not hasattr(net, 'all_nodes'):
    # Creamos una propiedad dinámica que devuelva la lista de nodos
    # Usamos fget para que siempre obtenga la lista actualizada de net.nodes
        setattr(
            QuantumNetwork,
            'all_nodes',
            property(lambda self: self.nodes + ([self.controller] if getattr(self, 'controller', None) is not None else [])),
        )
    
    sim.run()
    solicitudes_formateadas = []
    for req in net.requests:
        solicitudes_formateadas.append({
            "req_id": req.attr.get("req_id"), 
            "src": req.src,
            "dst": req.dst
        })

    counters = LinkLayerCounters.aggregate(net.nodes)
    return construir_resultados_qcast(ctrl, solicitudes_formateadas, DEFAULT_ATTEMPTS), solicitudes_formateadas, counters

# Ejecución
print("Iniciando simulación Q-CAST...")
resultados_qcast, reqs, counters = run_qcast_sim()

print("\n--- RESULTADOS FINALES ---")
throughput = counters.n_etg / counters.n_attempts if counters.n_attempts > 0 else 0.0
print(f"Tasa de éxito (n_etg/n_attempts): {throughput:.4f} EPS")
print(f"  n_etg={counters.n_etg}, n_attempts={counters.n_attempts}")
eps = counters.n_etg / LIMIT_VAL
print(f"Throughput Global (E2E Successful Requests): {eps:.4f} EPS")

# Aquí podrías añadir los bloques de Dijkstra siguiendo la misma lógica