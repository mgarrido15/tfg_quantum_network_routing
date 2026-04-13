import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from mqns.mqns.simulator import Simulator
from mqns.mqns.network.network.timing import TimingModeSyncQCast
from mqns.mqns.network.topology.randomtopo import RandomTopology
from mqns.mqns.network.network import QuantumNetwork
from mqns.mqns.utils import log

from mqns.mqns.network.qcast.controller import QCastController
from mqns.mqns.network.qcast.forwarder import QCastForwarder

LIMIT_VAL = 10.0
log.set_default_level("INFO") 
sim = Simulator(0, LIMIT_VAL) 

topo = RandomTopology(nodes_number=8, lines_number=14, qchannel_args={"success_prob": 0.95})
net = QuantumNetwork(topo)

net.simulator = sim 
net.all_nodes = list(net.nodes.values()) if isinstance(net.nodes, dict) else net.nodes

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
        print(f" Controlador instalado en: {node.name}")
    else:
        app = QCastForwarder(k_max=2)
        node.forwarder = app
        app.install(node)
        node.handle = app.handle 

if len(nodes) >= 6:
    net.add_request(nodes[1], nodes[-1]) # n2 -> n8
    net.add_request(nodes[2], nodes[5])  # n3 -> n6
    net.add_request(nodes[3], nodes[0])  # n4 -> n1

print("\n INICIANDO SIMULACIÓN...")
sim.run()


print(" RESULTADOS FINALES Q-CAST")
controller = net.controller
total_exitos = getattr(controller, 'successful_requests', 0)
throughput = total_exitos / LIMIT_VAL 

print(f"Total Éxitos E2E:    {total_exitos}")
print(f"Throughput Global:   {throughput:.2f} EPS")