#    Modified by Amar Abane for Multiverse Quantum Network Simulator
#    Date: 05/17/2025
#    Summary of changes: Adapted logic to support dynamic approaches.
#
#    This file is based on a snapshot of SimQN (https://github.com/QNLab-USTC/SimQN),
#    which is licensed under the GNU General Public License v3.0.
#
#    The original SimQN header is included below.


#    SimQN: a discrete-event simulator for the quantum networks
#    Copyright (C) 2021-2022 Lutong Chen, Jian Li, Kaiping Xue
#    University of Science and Technology of China, USTC.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

import math
import json
from typing import cast, overload, TypeVar, Any

import matplotlib.pyplot as plt
import networkx as nx

from mqns.entity.base_channel import BaseChannel
from mqns.entity.cchannel import ClassicChannel
from mqns.entity.node import Controller, Node, QNode
from mqns.entity.qchannel import QuantumChannel
from mqns.entity.qchannel.link_arch_sim import LinkArchSim
from mqns.models.epr import Entanglement, WernerStateEntanglement
from mqns.network.network.request import Request
from mqns.network.network.reporting import estimar_fidelidad_observada_de_ruta
from mqns.network.network.timing import TimingMode, TimingModeAsync
from mqns.network.route import DijkstraRouteAlgorithm, RouteAlgorithm, RouteQueryResult
from mqns.network.topology import ClassicTopology, Topology
from mqns.simulator import Simulator
from mqns.utils import rng
from mqns.utils import log

C = TypeVar("C", bound=BaseChannel)

def _save_channel(l: list[C], d: dict[tuple[str, str], list[C]], ch: C):
    l.append(ch)
    node_list = getattr(ch, 'node_list', [])
    if len(node_list) != 2:
        return
    a, b = sorted((node.name for node in cast(list[Node], node_list)))
    d.setdefault((a, b), []).append(ch)


def _get_channel(l: list[C], d: dict[tuple[str, str], list[C]], q: tuple[str, ...]) -> C:
    if len(q) == 1:
        name = q[0]
        for ch in l:
            if ch.name == name:
                return ch
        raise IndexError(f"channel {name} does not exist")

    a, b = sorted(q)
    try:
        return d[(a, b)][0]
    except KeyError:
        raise IndexError(f"channel between {a} and {b} does not exist")
    except IndexError:
        raise IndexError(f"channel between {a} and {b} does not exist")


class QuantumNetwork:
    """QuantumNetwork includes quantum nodes, quantum and classical channels, arranged in a given topology"""

    def __init__(
        self,
        topo: Topology | None = None,
        *,
        classic_topo: ClassicTopology | None = None,
        route: RouteAlgorithm[QNode, QuantumChannel] | None = None,
        timing: TimingMode = TimingModeAsync(),
        epr_type: type[Entanglement] = WernerStateEntanglement,
    ):
        """
        Args:
            topo: topology builder.
            classic_topo: classic topology parameter, passed to topology builder.
            route: routing algorithm, defaults to dijkstra.
            timing: network-wide application timing mode.
            epr_type: network-wide entanglement type.
        """
        assert getattr(epr_type, "__final__", False) is True, f"entanglement type {epr_type} must be marked @final"

        self.timing = timing
        """Network-wide application timing mode."""
        self.simulator: Simulator
        """Simulator instance (assigned during install)."""
        self.epr_type = epr_type
        """Network-wide entanglement type."""

        self.controller: Controller | None = None
        """Controller node."""
        self.nodes: list[QNode] = []
        """List of quantum nodes."""
        self._node_by_name: dict[str, QNode] = {}
        self.qchannels: list[QuantumChannel] = []
        """List of quantum channels."""
        self._qchannel_by_ends: dict[tuple[str, str], list[QuantumChannel]] = {}
        self.cchannels: list[ClassicChannel] = []
        """List of classic channels."""
        self._cchannel_by_ends: dict[tuple[str, str], list[ClassicChannel]] = {}

        if topo is not None:
            self._populate_from_topo(topo, classic_topo)

        self.route: RouteAlgorithm = DijkstraRouteAlgorithm() if route is None else route
        """Routing algorithm."""

        self.requests: list[Request] = []
        """Requested end-to-end entanglements."""

    def _populate_from_topo(self, topo: Topology, classic_topo: ClassicTopology | None):
        nodes, qchannels = topo.build()
        if classic_topo is not None:
            cchannels = topo.add_cchannels(classic_topo=classic_topo, nl=nodes, ll=qchannels)
        else:
            cchannels = topo.add_cchannels()

        for node in nodes:
            self.add_node(node)
        for ch in qchannels:
            self.add_qchannel(ch)
        for ch in cchannels:
            self.add_cchannel(ch)

        if topo.controller:
            self.set_controller(topo.controller)
    
    def get_qchannels_between(self, a: str, b: str) -> list[QuantumChannel]:
        """
        Recupera TODOS los canales cuánticos paralelos (Multigrafo) entre dos nodos.
        """
        target_nodes = {a, b}
        canales_paralelos = []
        for ch in self.qchannels:
            if {node.name for node in ch.node_list} == target_nodes:
                canales_paralelos.append(ch)
        return canales_paralelos

    def _ensure_not_installed(self) -> None:
        """
        Assert that this entity has not been installed into a simulator.
        """
        assert not hasattr(self, "simulator"), "function only available prior to self.install()"

    def install(self, simulator: Simulator):
        """
        Install all nodes (including channels, memories and applications) in this network

        Args:
            simulator: the simulator

        """
        self.simulator = simulator
        """Simulator instance."""

        self.all_nodes: list[Node] = []
        """A collection of quantum nodes and the controller (if present)."""
        self.all_nodes += self.nodes
        if self.controller:
            self.all_nodes.append(self.controller)

        for node in self.all_nodes:
            node.install(simulator)
        self.timing.install(self)

    def add_node(self, node: QNode):
        """
        Add a QNode into this network.
        """
        self._ensure_not_installed()
        assert node.name not in self._node_by_name, f"duplicate node name {node.name}"
        self.nodes.append(node)
        self._node_by_name[node.name] = node
        node.add_network(self)

    def get_node(self, name: str) -> QNode:
        """
        Get QNode by name.

        Raises:
            IndexError: node does not exist.
        """
        try:
            return self._node_by_name[name]
        except KeyError:
            raise IndexError(f"node {name} does not exist")

    def set_controller(self, controller: Controller):
        """
        Set the controller of this network.
        """
        self._ensure_not_installed()
        self.controller = controller
        controller.add_network(self)

    def get_controller(self) -> Controller:
        """
        Get the Controller of this network.

        Raises:
            IndexError: controller does not exist.
        """
        if self.controller is None:
            raise IndexError("network does not have a controller")
        return self.controller

    def add_qchannel(self, qchannel: QuantumChannel):
        """
        Add a QuantumChannel into this network.
        """
        self._ensure_not_installed()
        _save_channel(self.qchannels, self._qchannel_by_ends, qchannel)

    @overload
    def get_qchannel(self, name: str, /) -> QuantumChannel:
        """
        Retrieve QuantumChannel by name.

        Raises:
            IndexError: channel does not exist.
        """

    @overload
    def get_qchannel(self, a: str, b: str, /) -> QuantumChannel:
        """
        Retrieve QuantumChannel by node names.

        Raises:
            IndexError: channel does not exist.
        """

    def get_qchannel(self, *q: str) -> QuantumChannel:
        return _get_channel(self.qchannels, self._qchannel_by_ends, q)

    def add_cchannel(self, cchannel: ClassicChannel):
        """
        Add a ClassicChannel into this network.
        """
        self._ensure_not_installed()
        _save_channel(self.cchannels, self._cchannel_by_ends, cchannel)

    @overload
    def get_cchannel(self, name: str, /) -> ClassicChannel:
        """
        Retrieve ClassicalChannel by name.

        Raises:
            IndexError: channel does not exist.
        """

    @overload
    def get_cchannel(self, a: str, b: str, /) -> ClassicChannel:
        """
        Retrieve ClassicalChannel by node names.

        Raises:
            IndexError: channel does not exist.
        """

    def get_cchannel(self, *q: str) -> ClassicChannel:
        return _get_channel(self.cchannels, self._cchannel_by_ends, q)

    def build_route(self):
        """Build static route tables for each nodes"""
        self.route.build(self.nodes, self.qchannels)

    def query_route(self, src: QNode, dest: QNode) -> list[RouteQueryResult[QNode]]:
        """Query the metric, nexthop and the path

        Args:
            src: the source node
            dest: the destination node

        Returns:
            A list of route paths. The result should be sorted by the priority.
            The element is a tuple containing: metric, the next-hop and the whole path.

        """
        return self.route.query(src, dest)

    def add_request(self, src: QNode, dst: QNode, attr: dict = {}):
        """
        Add a request (src, dst) pair to the network.

        The request is placed in ``self.requests`` list.
        The scenario must manually pass these requests to relevant applications (e.g. ProactiveRoutingController).

        Args:
            src: the source node
            dst: the destination node
            attr: other attributions
        """
        req = Request(src, dst, attr)
        self.requests.append(req)

    def load_topology_from_json(self, json_file: str):
        """
        Load topology configuration from a JSON file and apply it to the existing network.
        
        The JSON should have the structure:
        {
            "nodos": [{"id": "n1", "capacity": 10}, ...],
            "enlaces": [{"u": "n1", "v": "n2", "prob": 0.9, "fidelity": 0.95}, ...],
            "solicitudes": [{"src": "n1", "dst": "n2"}, ...]
        }
        """
        with open(json_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Update node capacities
        for node_data in config.get("nodos", []):
            node_id = node_data["id"]
            capacity = node_data.get("capacity", 10)
            node = self.get_node(node_id)
            if node and hasattr(node, 'memory'):
                node.memory.capacity = capacity
        
        # Update channel success_prob and _fidelity
        for link_data in config.get("enlaces", []):
            u = link_data["u"]
            v = link_data["v"]
            prob = link_data.get("prob", 1.0)
            fidelity = link_data.get("fidelity", 0.99)
            # Find the channel
            qc: Any = self.get_qchannel(u, v)
            if qc:
                qc.success_prob = prob
                qc._fidelity = fidelity
        
        # Add requests
        for req_data in config.get("solicitudes", []):
            src_name = req_data.get("src")
            dst_name = req_data.get("dst")
            if src_name and dst_name:
                src = self.get_node(src_name)
                dst = self.get_node(dst_name)
                if src and dst:
                    self.add_request(src, dst)

    def build_topology_from_json(self, json_file: str):
        """
        Build the complete topology from a JSON file, creating nodes and channels.
        
        The JSON should have the structure:
        {
            "nodos": [{"id": "n1", "capacity": 10}, ...],
            "enlaces": [{"u": "n1", "v": "n2", "prob": 0.9, "fidelity": 0.95, "channels": 3}, ...],
            "solicitudes": [{"src": "n1", "dst": "n2"}, ...]
        }
        """
        from mqns.entity.memory import QuantumMemory
        
        with open(json_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Create nodes
        nodes = []
        for node_data in config.get("nodos", []):
            node_id = node_data["id"]
            
            # Create QNode
            node = QNode(node_id)
            
            # Add memory to node
            node.memory = QuantumMemory(f"{node_id}.memory", capacity=node_data.get("capacity", 10))
            
            # Set node fidelity if provided in config
            node.node_fidelity = node_data.get("fidelity", 1.0)
            
            self.add_node(node)
            nodes.append(node)
        
        # Create quantum channels between nodes.
        # Repeated entries for the same pair create parallel channels.
        link_parallel_index: dict[tuple[str, str], int] = {}
        for link_data in config.get("enlaces", []):
            u_name = link_data["u"]
            v_name = link_data["v"]
            prob_val = link_data.get("prob")
            fidelity = link_data.get("fidelity", None)
            length = link_data.get("length", 3.0)
            alpha = link_data.get("alpha", 0.2)
            eta_s = link_data.get("eta_s", 0.7)
            eta_d = link_data.get("eta_d", 0.8)
            transfer_error = link_data.get("transfer_error", "DEPOLAR:0.01")

            calc_arch: Any = LinkArchSim()
            prob: float
            if prob_val is None:
                prob = cast(float, calc_arch._compute_success_prob(
                        length=length, alpha=alpha, eta_s=eta_s, eta_d=eta_d
                    ))
            else:
                prob = float(prob_val)

            if prob < 0.5:
                modeled_prob = cast(float, calc_arch._compute_success_prob(
                    length=length, alpha=alpha, eta_s=eta_s, eta_d=eta_d
                ))
                prob = max(prob, modeled_prob)

            if fidelity is None:
                fidelity = math.exp(-alpha * length)
            
            # --- NUEVO: Leer la capacidad de multiplexación (Por defecto 1) ---
            num_canales = link_data.get("channels", 1)
            link_key = tuple(sorted((u_name, v_name)))

            u_node = self.get_node(u_name)
            v_node = self.get_node(v_name)

            if u_node and v_node:

                # 2. BUCLE MULTIGRAFO: Crear los canales físicos paralelos
                for i in range(num_canales):
                    ch_index = link_parallel_index.get(link_key, 0)
                    link_parallel_index[link_key] = ch_index + 1

                    # Nombres únicos por cada hilo (ej. QC_n1_n2_ch0, QC_n1_n2_ch1, ...)
                    ch_name = f"QC_{u_name}_{v_name}_ch{ch_index}"
                    
                    # Instanciar la arquitectura física e INYECTAR los parámetros de atenuación
                    link_arch: Any = LinkArchSim()
                    link_arch.length = length
                    link_arch.alpha = alpha
                    link_arch.eta_s = eta_s
                    link_arch.eta_d = eta_d
                    link_arch.success_prob = prob
                    link_arch.p_s = prob

                    qc: Any = QuantumChannel(
                        name=ch_name,
                        alpha=alpha,
                        length=length,
                        drop_rate=(1.0 - prob),
                        transfer_error=transfer_error,
                        link_arch=link_arch,
                    )
                    
                    # Asignamos directamente la probabilidad al canal (por si el simulador lo lee de aquí)
                    qc.success_prob = prob
                    qc.p_s = prob
                    qc.fidelity = fidelity
                    qc._fidelity = fidelity

                    # Add channel to nodes
                    u_node.add_qchannel(qc)
                    v_node.add_qchannel(qc)

                    # Add channel to network
                    self.add_qchannel(qc)
                    
                    # Assign memory qubits for this specific physical channel
                    try:
                        qc.assign_memory_qubits()
                        log.debug(f"Assigned memory qubits for channel {qc.name} on nodes {u_name},{v_name}")
                    except Exception as e:
                        log.debug(f"Failed to assign memory qubits for channel {qc.name}: {e}")
                    
                    # Create corresponding classic channel
                    cc_name = f"CC_{u_name}_{v_name}_ch{ch_index}"
                    cc = ClassicChannel(cc_name)
                    u_node.add_cchannel(cc)
                    v_node.add_cchannel(cc)
                    self.add_cchannel(cc)
        
        # Create control plane: connect first node (controller) to all other nodes
        if nodes:
            controller_node = nodes[0]  # First node acts as controller
            for other_node in nodes[1:]:
                cc_name = f"CC_CTRL_{controller_node.name}_{other_node.name}"
                cc = ClassicChannel(cc_name)
                controller_node.add_cchannel(cc)
                other_node.add_cchannel(cc)
                self.add_cchannel(cc)
        
        # Add requests from JSON
        for req_data in config.get("solicitudes", []):
            src_name = req_data.get("src")
            dst_name = req_data.get("dst")
            if src_name and dst_name:
                src = self.get_node(src_name)
                dst = self.get_node(dst_name)
                if src and dst:
                    self.add_request(src, dst)


    def random_requests(
        self,
        n: int,
        *,
        clear=True,
        allow_overlay=False,
        min_hops=1,
        max_hops=10,
        attr: dict | None = None,
        forbid_endpoint_internal=True,  # reject endpoint-vs-internal conflicts
    ):
        """
        Generate random (src, dst) pairs requests.

        The requests are placed in ``self.requests`` list.
        The scenario must manually pass these requests to relevant applications (e.g. ProactiveRoutingController).

        Args:
            n: number of requests to generate
            clear: if True, clear existing requests in ``self.requests``
            allow_overlay: allow nodes to be the source or destination in multiple requests
            min_hops: minimum number of hops (inclusive)
            max_hops: maximum number of hops (inclusive)
            attr: request attributes
            forbid_endpoint_internal: if True, eliminate requests that
                would fail the rank-based endpoint-vs-internal check in SWAP-ASAP.
        """
        attr = {} if attr is None else attr
        used_nodes: list[int] = []
        nnodes = len(self.nodes)

        if n < 1:
            raise ValueError("number of requests should be larger than 1")
        if not allow_overlay and n * 2 > nnodes:
            raise ValueError("Too many requests")

        if clear:
            self.requests.clear()

        # Track accepted paths
        accepted_paths: list[dict] = []  # each: {"endpoints": set, "edges": set}

        def to_meta(path_nodes: list[QNode]) -> dict:
            endpoints = {path_nodes[0].name, path_nodes[-1].name}
            edges = {(path_nodes[i].name, path_nodes[i + 1].name) for i in range(len(path_nodes) - 1)}
            return {"endpoints": endpoints, "edges": edges}

        def violates_endpoint_internal(candidate_meta: dict) -> bool:
            cend = candidate_meta["endpoints"]
            cedges = candidate_meta["edges"]
            for meta in accepted_paths:
                pend = meta["endpoints"]
                pedges = meta["edges"]
                shared = cedges & pedges
                if not shared:
                    continue
                for u, v in shared:
                    # one path treats node as endpoint, other as internal
                    if ((u in cend) != (u in pend)) or ((v in cend) != (v in pend)):
                        return True
            return False

        for _ in range(n):
            while True:
                src_idx = rng.integers(0, nnodes, dtype=int)
                dst_idx = rng.integers(0, nnodes, dtype=int)
                if src_idx == dst_idx:
                    continue
                if not allow_overlay and (src_idx in used_nodes or dst_idx in used_nodes):
                    continue

                src = self.nodes[src_idx]
                dst = self.nodes[dst_idx]
                route_result = self.query_route(src, dst)
                if not route_result:
                    continue

                hops, _, path_nodes = route_result[0]
                if not (min_hops <= hops <= max_hops):
                    continue

                if forbid_endpoint_internal:
                    meta = to_meta(path_nodes)
                    if violates_endpoint_internal(meta):
                        continue
                    accepted_paths.append(meta)

                # Accept
                if not allow_overlay:
                    used_nodes.extend([src_idx, dst_idx])

                self.add_request(src, dst, attr)
                break



def dibujar_escenario(net) -> None:
    """Dibuja la topología de la red cuántica con nodos, enlaces, probabilidades y fidelidades.
    """
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


    pos = nx.spring_layout(G, seed=42, k=0.3)
    
    plt.figure(figsize=(12, 8))
    
    nx.draw_networkx_nodes(G, pos, node_size=3500, node_color='lightblue', edgecolors='black')
    
    nx.draw_networkx_labels(G, pos, labels=labels_nodos, font_size=14, font_weight='bold')

    nx.draw_networkx_edges(G, pos, width=2, alpha=0.5)
    
    labels_enlaces = {}
    for qc in channels:
        if hasattr(qc, 'node_list'):
            u_name, v_name = qc.node_list[0].name, qc.node_list[1].name
        else:
            u_name, v_name = qc.node1.name, qc.node2.name
        prob = getattr(qc, 'success_prob', 1.0)
        route_nodes = list(qc.node_list) if hasattr(qc, 'node_list') else [qc.node1, qc.node2]
        
        est_fid = estimar_fidelidad_observada_de_ruta(net, route_nodes) 
        labels_enlaces[(u_name, v_name)] = f"P:{prob:.2f}\nF_est:{est_fid:.2f}"
        
    nx.draw_networkx_edge_labels(G, pos, edge_labels=labels_enlaces, font_color='red', font_size=12)

    plt.title("Topología de Red: Capacidad y Fidelidad estimada en nodos/enlaces")
    plt.axis('off')
    plt.tight_layout()
    plt.show()


def guardar_configuracion(
    net: QuantumNetwork, 
    solicitudes: list,
    controller=None,
    filename: str = "escenario_resultado.json"
) -> None:
    """Guarda la configuración de la red y resultados de las solicitudes en un archivo JSON.
    """
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
            
        config["enlaces"].append({
            "u": u_name, 
            "v": v_name, 
            "prob": getattr(qc, 'success_prob', 1.0), 
            "fidelity": getattr(qc, '_fidelity', 0.99)
        })

    if controller is not None and hasattr(controller, 'request_route_info'):
        for src_node, dst_node in solicitudes:
            req_id = _build_request_id(src_node.name, dst_node.name)
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


def _build_request_id(src_name: str, dst_name: str) -> str:
    """Construye un identificador único para una solicitud.
    
    Args:
        src_name (str): Nombre del nodo origen
        dst_name (str): Nombre del nodo destino
        
    Returns:
        str: Identificador de solicitud
    """
    return f"REQ_{src_name}_TO_{dst_name}"
