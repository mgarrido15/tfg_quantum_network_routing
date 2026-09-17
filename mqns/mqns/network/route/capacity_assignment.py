from typing import Any, Callable
from collections import deque
from mqns.utils import log


def initialize_virtual_node_capacity(controller: Any, all_nodes: list) -> None:
    controller._node_remaining_capacity = {
        n: getattr(getattr(n, "memory", None), "capacity", 0) for n in all_nodes
    }


def _route_available_width(net: Any, controller: Any, route_nodes: list) -> int:
    """Compute the maximum width that can be reserved along a route.

    Endpoint nodes can use one qubit per path; intermediate nodes must reserve
    two qubits per path because they need one qubit for each incoming and outgoing
    entanglement during swapping.
    """
    widths: list[int] = []
    for i, node in enumerate(route_nodes):
        cap = controller._node_remaining_capacity.get(node, 0)
        if i == 0 or i == len(route_nodes) - 1:
            widths.append(cap)
        else:
            widths.append(cap // 2)
    memory_width = min(widths) if widths else 0
    if memory_width <= 0 or len(route_nodes) < 2:
        return 0
    channel_width = min(
        controller._edge_remaining_capacity.get(
            tuple(sorted((route_nodes[i].name, route_nodes[i + 1].name))),
            0,
        )
        for i in range(len(route_nodes) - 1)
    )
    return min(memory_width, channel_width)


def assign_dijkstra_routes_with_capacity(
    net: Any,
    controller: Any,
    solicitudes: list,
    route_quality_fn: Callable[[Any, list], tuple[float, float]],
    enforce_capacity: bool = True,
    reserve_all_route_capacity: bool = False,
) -> None:
    all_nodes = list(getattr(net, "nodes", getattr(net, "all_nodes", [])))
    controller._node_remaining_capacity = {
        n: getattr(getattr(n, "memory", None), "capacity", 0) for n in all_nodes
    }
    controller._edge_remaining_capacity = {}
    for channel in getattr(net, "qchannels", []):
        if not hasattr(channel, "node_list") or len(channel.node_list) != 2:
            continue
        edge_key = tuple(sorted((channel.node_list[0].name, channel.node_list[1].name)))
        controller._edge_remaining_capacity[edge_key] = (
            controller._edge_remaining_capacity.get(edge_key, 0) + 1
        )

    def _commit_selection(req, route_nodes, selected_width: int, prob: float, fidelity: float):
        req_id = req["req_id"]
        route_names = [n.name for n in route_nodes]
        hops = len(route_names) - 1

        controller.request_route_info[req_id] = {
            "route": route_names,
            "hops": hops,
            "route_success_prob": prob,
            "route_fidelity": fidelity,
            "width": selected_width,
            "w_asignado": selected_width,
        }

        if enforce_capacity:
            if reserve_all_route_capacity:
                for i, n in enumerate(route_nodes):
                    consume = selected_width if (i == 0 or i == len(route_nodes) - 1) else (2 * selected_width)
                    controller._node_remaining_capacity[n] = max(
                        0,
                        controller._node_remaining_capacity.get(n, 0) - consume,
                    )
            else:
                for i, n in enumerate(route_nodes):
                    consume = 1 if (i == 0 or i == len(route_nodes) - 1) else 2
                    controller._node_remaining_capacity[n] = max(
                        0,
                        controller._node_remaining_capacity.get(n, 0) - consume,
                    )
            edge_width = selected_width if reserve_all_route_capacity else 1
            for left, right in zip(route_nodes[:-1], route_nodes[1:]):
                edge_key = tuple(sorted((left.name, right.name)))
                controller._edge_remaining_capacity[edge_key] = max(
                    0,
                    controller._edge_remaining_capacity.get(edge_key, 0) - edge_width,
                )

        controller.request_success.setdefault(req_id, False)
        controller.request_success_count.setdefault(req_id, 0)

    for req in solicitudes:
        req_id = req["req_id"]
        src_node = req["src"]
        dst_node = req["dst"]

        try:
            query_result = net.query_route(src_node, dst_node)
            log.debug(
                f"REQ {req_id}: query_route({src_node.name}, {dst_node.name}) -> "
                f"{len(query_result) if query_result else 0} results"
            )
            if not query_result:
                log.debug("No route found, skipping")
                continue

            candidates = query_result if isinstance(query_result, list) else [query_result]
            selected = None
            selected_width = 1

            if not enforce_capacity:
                for cand in candidates:
                    if hasattr(cand, "route"):
                        selected = cand
                        break
            else:
                # Prefer any candidate that already fits capacity constraints
                if reserve_all_route_capacity:
                    best_score = -1.0
                    for cand in candidates:
                        if not hasattr(cand, "route"):
                            continue
                        cand_route = cand.route
                        width = _route_available_width(net, controller, cand_route)
                        if width <= 0:
                            continue
                        cand_prob, _cand_fid = route_quality_fn(net, cand_route)
                        score = float(width) * float(cand_prob)
                        if score > best_score:
                            best_score = score
                            selected = cand
                            selected_width = width
                else:
                    for cand in candidates:
                        if not hasattr(cand, "route"):
                            continue
                        if _route_available_width(net, controller, cand.route) >= 1:
                            selected = cand
                            break

            # If no candidate fits, and the routing algorithm supports
            # capacity-awareness, try to find an alternative route excluding
            # nodes that have no remaining capacity (except endpoints).
            route_obj = getattr(net, 'route', None)
            is_capacity_algo = (
                route_obj is not None
                and getattr(route_obj, '__class__', None) is not None
                and route_obj.__class__.__name__ == 'DijkstraCapacityRouteAlgorithm'
            )

            if enforce_capacity and selected is None and is_capacity_algo:
                # Build set of nodes to exclude (capacity <= 0), but allow src/dst
                excluded = {n for n, c in controller._node_remaining_capacity.items() if c <= 0}
                if src_node in excluded:
                    excluded.remove(src_node)
                if dst_node in excluded:
                    excluded.remove(dst_node)

                # Simple BFS on network graph excluding nodes in `excluded`
                def _bfs_find_path(start, goal, excluded_nodes):
                    q = deque()
                    q.append(start)
                    parent = {start: None}
                    # Build adjacency from qchannels
                    adj = {}
                    for ch in getattr(net, "qchannels", []):
                        if hasattr(ch, "node_list") and len(ch.node_list) == 2:
                            a, b = ch.node_list
                        else:
                            a = getattr(ch, "node1", None)
                            b = getattr(ch, "node2", None)
                        if a is None or b is None:
                            continue
                        adj.setdefault(a, set()).add(b)
                        adj.setdefault(b, set()).add(a)

                    while q:
                        cur = q.popleft()
                        if cur == goal:
                            # reconstruct
                            path = []
                            u = cur
                            while u is not None:
                                path.append(u)
                                u = parent.get(u)
                            path.reverse()
                            return path

                        for nb in adj.get(cur, []):
                            if nb in parent:
                                continue
                            if nb in excluded_nodes:
                                continue
                            parent[nb] = cur
                            q.append(nb)
                    return None

                alt_path = _bfs_find_path(src_node, dst_node, excluded)
                if alt_path:
                    if _route_available_width(net, controller, alt_path) < 1:
                        alt_path = None

                if alt_path:
                    # wrap in a RouteQueryResult-like object with .route and .metric
                    class _Simple:
                        def __init__(self, route):
                            self.route = route
                            self.metric = len(route) - 1

                    selected = _Simple(alt_path)
                    if reserve_all_route_capacity:
                        selected_width = _route_available_width(net, controller, selected.route)

            if selected is None:
                log.debug("No candidate selected, skipping")
                continue

            route_nodes = selected.route
            route_names = [n.name for n in route_nodes]
            hops = len(route_names) - 1
            prob, fidelity = route_quality_fn(net, route_nodes)
            log.debug(
                f"Selected route: {route_names} "
                f"({hops} hops, prob={prob}, fidelity={fidelity})"
            )
        except Exception as e:
            log.error(f"Failed to assign route for {req_id}: {e}")
            continue

        _commit_selection(req, route_nodes, selected_width, prob, fidelity)


def assign_dijkstra_routes_with_capacity_reserve_all(
    net: Any,
    controller: Any,
    solicitudes: list,
    route_quality_fn: Callable[[Any, list], tuple[float, float]],
    enforce_capacity: bool = True,
) -> None:
    """Variant that reserves the maximum feasible width for each selected route."""
    assign_dijkstra_routes_with_capacity(
        net,
        controller,
        solicitudes,
        route_quality_fn,
        enforce_capacity=enforce_capacity,
        reserve_all_route_capacity=True,
    )
