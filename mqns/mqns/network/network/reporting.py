from collections import defaultdict
from typing import Any



def build_request_id(src_name: str, dst_name: str, req_index: int) -> str:
    return f"REQ_{req_index:03d}_{src_name}_TO_{dst_name}"


def obtener_prob_y_fidelidad_de_ruta(net: Any, ruta: list) -> tuple[float, float]:
    """
    Calcula la probabilidad de éxito estimada y fidelidad para una ruta completa.
    
    Itera sobre cada enlace en la ruta y multiplica sus probabilidades y fidelidades.
    
    Retorna: (probabilidad_estimada, fidelidad_ruta)
    """
    route_prob = 1.0      # Comenzamos con prob=1.0
    route_fidelity = 1.0  # Comenzamos con fidelidad=1.0

    # Iterar sobre cada enlace (par de nodos consecutivos)
    for i in range(len(ruta) - 1):
        nodo_a = ruta[i]
        nodo_b = ruta[i + 1]

        # Buscar el canal cuántico entre estos dos nodos
        qc = None
        for ch in getattr(net, "qchannels", []):
            if hasattr(ch, "node_list") and len(ch.node_list) == 2:
                n1, n2 = ch.node_list
            else:
                n1 = getattr(ch, "node1", None)
                n2 = getattr(ch, "node2", None)
            if (n1 == nodo_a and n2 == nodo_b) or (n1 == nodo_b and n2 == nodo_a):
                qc = ch
                break

        if qc is None:
            return 0.0, 0.0

        # Obtener la probabilidad de éxito del enlace (basada en arquitectura física)
        prob = getattr(qc, "success_prob", 1.0)
        
        # Obtener la fidelidad del enlace (calculada como e^(-α*distance))
        fid = getattr(qc, "_fidelity", None)
        if fid is None:
            # Fallback si no está disponible
            transfer_error = getattr(qc, "transfer_error", None)
            if transfer_error is not None and hasattr(transfer_error, "p_survival"):
                fid = (3 * transfer_error.p_survival + 1) / 4
            else:
                fid = 0.99

        # Multiplicar probabilidades y fidelidades (producto en serie)
        route_prob *= prob
        route_fidelity *= fid

    return route_prob, route_fidelity


def construir_resultados_qcast(controller: Any, solicitudes: list, attempts_per_route: int) -> list:
    resultados = []
    route_info = getattr(controller, "request_route_info", {})
    success_count = getattr(controller, "request_success_count", {})

    for req in solicitudes:
        req_id = req["req_id"]
        src = req["src"].name
        dst = req["dst"].name
        info = route_info.get(req_id, None)

        if info is None:
            resultados.append(
                {
                    "req_id": req_id,
                    "src": src,
                    "dst": dst,
                    "route": None,
                    "hops": 0,
                    "route_success_prob": 0.0,
                    "route_fidelity": 0.0,
                    "route_width": 0,
                    "attempts": attempts_per_route,
                    "successes": 0,
                }
            )
            continue

        resultados.append(
            {
                "req_id": req_id,
                "src": src,
                "dst": dst,
                "route": info.get("route"),
                "hops": info.get("hops", 0),
                "route_success_prob": info.get("route_success_prob", 0.0),
                "route_fidelity": info.get("route_fidelity", 0.0),
                "route_width": info.get("width", 0),
                "attempts": attempts_per_route,
                "successes": success_count.get(req_id, 0),
            }
        )

    return resultados


def agregar_por_par(resultados: list) -> dict:
    agrupados = defaultdict(
        lambda: {
            "request_count": 0,
            "attempts": 0,
            "successes": 0,
            "fidelity_sum": 0.0,
            "fidelity_count": 0,
            "route_probs": [],
            "route_widths": [],
            "routes": [],
        }
    )

    for r in resultados:
        key = (r["src"], r["dst"])
        a = agrupados[key]
        a["request_count"] += 1
        a["attempts"] += r["attempts"]
        a["successes"] += r["successes"]
        if r["route"] is not None:
            a["fidelity_sum"] += r["route_fidelity"]
            a["fidelity_count"] += 1
            a["route_probs"].append(r["route_success_prob"])
            a["route_widths"].append(r["route_width"])
            a["routes"].append(" -> ".join(r["route"]))

    return agrupados


def imprimir_resumen_algoritmo(nombre: str, resultados: list, sim_time: float) -> None:
    """
    Imprime un resumen de los resultados con métricas globales.
    
    Cálculos:
    - Total éxitos: suma de sucessos de todas las requests
    - Throughput global: total_éxitos / sim_time
    - Por cada par (src, dst):
      * Throughput: éxitos del par / sim_time
      * P_éxito: éxitos del par / intentos del par
      * Fidelidad: promedio de fidelidades estimadas
    """
    total_exitos = sum(r["successes"] for r in resultados)
    throughput_global = total_exitos / sim_time if sim_time > 0 else 0.0
    por_par = agregar_por_par(resultados)

    print(f"\nRESULTADOS {nombre}")
    print(f"Total exitos E2E: {total_exitos}")
    print(f"Throughput global: {throughput_global:.4f} EPS")

    for (src, dst), agg in sorted(por_par.items()):
        throughput_par = agg["successes"] / sim_time if sim_time > 0 else 0.0
        p_exito = (agg["successes"] / agg["attempts"]) if agg["attempts"] > 0 else 0.0
        fidelidad = (agg["fidelity_sum"] / agg["fidelity_count"]) if agg["fidelity_count"] > 0 else 0.0
        route_width = min(agg["route_widths"]) if agg["route_widths"] else 0
        rutas = "; ".join(sorted(set(agg["routes"]))) if agg["routes"] else "SIN_RUTA"

        print(f"\nRuta {src} -> {dst}")
        print(f"  - Numero de peticiones creadas: {agg['request_count']}")
        print(f"  - Throughput ruta: {throughput_par:.4f} EPS")
        print(f"  - Probabilidad de exito de la ruta: {p_exito:.4f}")
        print(f"  - Fidelidad de la ruta: {fidelidad:.4f}")
        print(f"  - Memoria minima de ruta: {route_width} qubits")
        print(f"  - Ruta usada: {rutas}")


def imprimir_info_rutas_detallada(
    nombre: str,
    controller: Any,
    resultados: list,
    sim_time: float,
    attempts_per_route: int,
) -> None:
    """
    Imprime información detallada de cada ruta individual con todas las métricas.
    
    Cálculos:
    - Throughput: successes / sim_time
    - Probabilidad estimada: calculada pre-simulación
    - Fidelidad: calculada como producto de fidelidades de enlaces
    - Agregado por par: agrupa requests del mismo (src, dst)
    """
    print(f"\nDETALLE RUTAS {nombre}")

    route_info = getattr(controller, "request_route_info", {}) if controller is not None else {}
    success_count = getattr(controller, "request_success_count", {}) if controller is not None else {}

    if not route_info:
        print("  (No hay rutas registradas en el controlador)")
    else:
        for req_id, info in route_info.items():
            route = info.get("route")
            hops = info.get("hops", 0)
            prob = info.get("route_success_prob", 0.0)
            fid = info.get("route_fidelity", 0.0)
            width = info.get("width", 0)
            metric = info.get("metric", None)
            successes = success_count.get(req_id, 0)
            throughput = successes / sim_time if sim_time > 0 else 0.0

            print(f"\n  Req {req_id}:")
            print(f"    - Ruta: {route if route is not None else 'SIN_RUTA'}")
            print(f"    - Saltos: {hops}")
            print(f"    - Intentos de entrelazamiento: {attempts_per_route}")
            print(f"    - Exitos observados: {successes}")
            print(f"    - Throughput: {throughput:.4f} EPS")
            print(f"    - Probabilidad de exito: {prob:.4f}")
            print(f"    - Fidelidad: {fid:.4f}")
            print(f"    - Memoria minima: {width}")
            if metric is not None:
                print(f"    - Metrica ruta: {metric}")
