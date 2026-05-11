from typing import Any, Callable
from collections import deque


def initialize_virtual_node_capacity(controller: Any, all_nodes: list) -> None:
    controller._node_remaining_capacity = {
        n: getattr(getattr(n, "memory", None), "capacity", 0) for n in all_nodes
    }


def assign_dijkstra_routes_with_capacity(
    net: Any,
    controller: Any,
    solicitudes: list,
    route_quality_fn: Callable[[Any, list], tuple[float, float]],
    enforce_capacity: bool = True,
) -> None:
    for req in solicitudes:
        req_id = req["req_id"]
        src_node = req["src"]
        dst_node = req["dst"]

        try:
            query_result = net.query_route(src_node, dst_node)
            if not query_result:
                continue

            candidates = query_result if isinstance(query_result, list) else [query_result]
            selected = None

            if not enforce_capacity:
                for cand in candidates:
                    if hasattr(cand, "route"):
                        selected = cand
                        break
            else:
                # Prefer any candidate that already fits capacity constraints
                for cand in candidates:
                    if not hasattr(cand, "route"):
                        continue
                    cand_route = cand.route
                    if all(controller._node_remaining_capacity.get(n, 0) > 0 for n in cand_route):
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
                    # wrap in a RouteQueryResult-like object with .route and .metric
                    class _Simple:
                        def __init__(self, route):
                            self.route = route
                            self.metric = len(route) - 1

                    selected = _Simple(alt_path)

            if selected is None:
                continue

            route_nodes = selected.route
            route_names = [n.name for n in route_nodes]
            hops = len(route_names) - 1
            prob, fidelity = route_quality_fn(net, route_nodes)
        except Exception:
            continue

        controller.request_route_info[req_id] = {
            "route": route_names,
            "hops": hops,
            "route_success_prob": prob,
            "route_fidelity": fidelity,
            "width": 1,
        }

        if enforce_capacity:
            for n in route_nodes:
                controller._node_remaining_capacity[n] = max(
                    0,
                    controller._node_remaining_capacity.get(n, 0) - 1,
                )

        controller.request_success.setdefault(req_id, False)
        controller.request_success_count.setdefault(req_id, 0)

        for k in range(len(route_nodes) - 1):
            current_node = route_nodes[k]
            next_node = route_nodes[k + 1]
            fw = getattr(current_node, "forwarder", None)
            if fw:
                if not hasattr(fw, "fib") or fw.fib is None:
                    from types import SimpleNamespace

                    fw.fib = SimpleNamespace(table={})
                fw.fib.table[req_id] = next_node.name
