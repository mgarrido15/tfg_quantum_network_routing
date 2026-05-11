import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mqns.simulator import Simulator
from mqns.network.network.timing import TimingModeSyncQCast
from mqns.network.network import QuantumNetwork
from mqns.network.network.reporting import build_request_id, obtener_prob_y_fidelidad_de_ruta
from mqns.network.qcast.controller import QCastController
from mqns.network.qcast.forwarder import QCastForwarder
from mqns.network.route.capacity_assignment import (
    assign_dijkstra_routes_with_capacity,
    initialize_virtual_node_capacity,
)
from mqns.network.route.dijkstra import (
    DijkstraCapacityRouteAlgorithm,
    DijkstraDistanceRouteAlgorithm,
    DijkstraRouteAlgorithm,
)


SIM_LIMIT = 10.0


def _format_path(route):
    return " -> ".join(node.name for node in route)


def _prepare_requests(network: QuantumNetwork) -> list[dict]:
    solicitudes: list[dict] = []
    for index, request in enumerate(network.requests, start=1):
        request.attr = dict(getattr(request, "attr", {}))
        req_id = request.attr.get("req_id")
        if not req_id:
            req_id = build_request_id(request.src.name, request.dst.name, index)
            request.attr["req_id"] = req_id
        solicitudes.append({"req_id": req_id, "src": request.src, "dst": request.dst})

    if solicitudes:
        return solicitudes

    node_names = [node.name for node in network.nodes]
    if len(node_names) < 2:
        return []

    src = network.get_node(node_names[0])
    dst = network.get_node(node_names[-1])
    req_id = build_request_id(src.name, dst.name, 1)
    network.add_request(src, dst, attr={"req_id": req_id})
    return [{"req_id": req_id, "src": src, "dst": dst}]


def _install_qcast_stack(network: QuantumNetwork, simulator: Simulator) -> QCastController:
    network.simulator = simulator
    network.all_nodes = list(network.nodes)

    timing = TimingModeSyncQCast(t1=0.1, t2=0.1, t3=0.1, t4=0.1)
    network.timing = timing
    timing.install(network)

    controller: QCastController | None = None
    for index, node in enumerate(network.all_nodes):
        node.install(simulator)

        if index == 0:
            controller = QCastController(k_max=2)
            setattr(node, "forwarder", controller)
            controller.install(node)
            setattr(controller, "net", network)
            setattr(network, "controller", controller)
            node.handle = controller.handle
        else:
            forwarder = QCastForwarder(k_max=2)
            setattr(node, "forwarder", forwarder)
            forwarder.install(node)
            node.handle = forwarder.handle

    if controller is None:
        raise RuntimeError("No se pudo instalar el controlador QCast")

    controller.handle_classic_packet = lambda node, msg: None
    return controller


def _run_algorithm(label: str, algorithm, config_path: str) -> None:
    network = QuantumNetwork(None)
    network.build_topology_from_json(config_path)

    solicitudes = _prepare_requests(network)
    if not solicitudes:
        raise RuntimeError("La topología no contiene suficientes nodos para simular")

    simulator = Simulator(0, SIM_LIMIT)
    controller = _install_qcast_stack(network, simulator)

    network.route = algorithm
    network.build_route()

    initialize_virtual_node_capacity(controller, network.all_nodes)
    assign_dijkstra_routes_with_capacity(
        network,
        controller,
        solicitudes,
        obtener_prob_y_fidelidad_de_ruta,
    )

    simulator.run()

    print(f"\n{label}")
    total_successes = 0
    for request in solicitudes:
        req_id = request["req_id"]
        route_info = controller.request_route_info.get(req_id, {})
        successes = controller.request_success_count.get(req_id, 0)
        total_successes += successes

        route = route_info.get("route")
        route_text = "SIN RUTA" if not route else " -> ".join(route)
        print(
            f"  {req_id}: successes={successes}, route={route_text}, "
            f"prob={route_info.get('route_success_prob', 0.0):.4f}, "
            f"fidelity={route_info.get('route_fidelity', 0.0):.4f}"
        )

    throughput = total_successes / SIM_LIMIT if SIM_LIMIT > 0 else 0.0
    print(f"  Total entrelazamientos: {total_successes}")
    print(f"  Throughput: {throughput:.4f} EPS")


def main() -> int:
    config_path = os.path.join(os.path.dirname(__file__), "..", "escenario_basico.json")
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    print(f"Cargando topología desde {config_path}...")

    algorithms = [
        ("Dijkstra clásico", DijkstraRouteAlgorithm()),
        ("Dijkstra con capacidad", DijkstraCapacityRouteAlgorithm()),
        ("Dijkstra por distancia", DijkstraDistanceRouteAlgorithm()),
    ]

    for label, algorithm in algorithms:
        _run_algorithm(label, algorithm, config_path)

    print("\nVerificación completada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())