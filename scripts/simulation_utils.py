"""
Utilidades para gestionar carpetas y resultados de simulaciones.
"""
import os
import json
from datetime import datetime
import matplotlib.pyplot as plt
from typing import Optional


def create_simulation_folder(base_output_dir: Optional[str] = None) -> str:
    """
    Crea una carpeta para la simulación con timestamp YYYYMMDD_HHMMSS.
    
    Args:
        base_output_dir: Directorio base (default: ../outputs)
        
    Returns:
        Path de la carpeta creada
    """
    if base_output_dir is None:
        base_output_dir = os.path.join(os.path.dirname(__file__), "..", "outputs")
    
    os.makedirs(base_output_dir, exist_ok=True)
    
    # Generar timestamp: YYYYMMDD_HHMMSS
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sim_folder = os.path.join(base_output_dir, timestamp)
    
    os.makedirs(sim_folder, exist_ok=True)
    print(f"✓ Carpeta de simulación creada: {sim_folder}")
    
    return sim_folder


def save_graph(figure_obj: plt.Figure, sim_folder: str, filename: str) -> str:
    """
    Guarda una gráfica matplotlib en la carpeta de simulación.
    
    Args:
        figure_obj: Figura de matplotlib
        sim_folder: Carpeta de simulación
        filename: Nombre del archivo (sin extensión)
        
    Returns:
        Path completo del archivo guardado
    """
    filepath = os.path.join(sim_folder, f"{filename}.png")
    figure_obj.savefig(filepath, dpi=300, bbox_inches='tight')
    plt.close(figure_obj)
    print(f"  ✓ Gráfica guardada: {filename}.png")
    return filepath


def save_topology_diagram(net, sim_folder: str, filename: str = "topology_diagram") -> str:
    """
    Guarda el diagrama de topología en la carpeta de simulación.
    
    Args:
        net: QuantumNetwork
        sim_folder: Carpeta de simulación
        filename: Nombre del archivo
        
    Returns:
        Path del archivo guardado
    """
    from mqns.network.network.network import dibujar_escenario
    import networkx as nx
    
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
    
    fig = plt.figure(figsize=(14, 10))
    
    nx.draw_networkx_nodes(G, pos, node_size=3500, node_color='lightblue', edgecolors='black')
    nx.draw_networkx_labels(G, pos, labels=labels_nodos, font_size=10, font_weight='bold')
    nx.draw_networkx_edges(G, pos, width=2, alpha=0.5)
    
    labels_enlaces = {}
    for qc in channels:
        if hasattr(qc, 'node_list'):
            u_name, v_name = qc.node_list[0].name, qc.node_list[1].name
        else:
            u_name, v_name = qc.node1.name, qc.node2.name
        
        prob = getattr(qc, 'success_prob', 1.0)
        length = getattr(qc, 'length', 0)
        labels_enlaces[(u_name, v_name)] = f"P:{prob:.2f}\nL:{length:.2f}"
    
    nx.draw_networkx_edge_labels(G, pos, edge_labels=labels_enlaces, font_color='red', font_size=8)
    
    plt.title("Topología de Red Cuántica: Capacidad y Probabilidad de Éxito")
    plt.axis('off')
    plt.tight_layout()
    
    return save_graph(fig, sim_folder, filename)


def save_link_metadata(net, sim_folder: str, filename: str = "link_metadata") -> str:
    """
    Guarda metadata de enlaces (probabilidades, fidelidades, longitudes) en JSON.
    
    Args:
        net: QuantumNetwork
        sim_folder: Carpeta de simulación
        filename: Nombre del archivo
        
    Returns:
        Path del archivo guardado
    """
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "nodes": [],
        "links": []
    }
    
    # Nodos
    nodos_lista = net.nodes if isinstance(net.nodes, list) else list(net.nodes.values())
    for node in nodos_lista:
        metadata["nodes"].append({
            "id": node.name,
            "capacity": getattr(node.memory, 'capacity', 10),
            "fidelity": getattr(node, 'node_fidelity', 1.0),
            "degree": len([ch for ch in getattr(net, 'qchannels', getattr(net, '_qchannels', []))
                          if (hasattr(ch, 'node_list') and (ch.node_list[0].name == node.name or ch.node_list[1].name == node.name)) or
                             (not hasattr(ch, 'node_list') and (ch.node1.name == node.name or ch.node2.name == node.name))])
        })
    
    # Enlaces
    seen_pairs = set()
    channels = getattr(net, 'qchannels', getattr(net, '_qchannels', []))
    for qc in channels:
        if hasattr(qc, 'node_list'):
            u_name, v_name = qc.node_list[0].name, qc.node_list[1].name
        else:
            u_name, v_name = qc.node1.name, qc.node2.name
        
        pair_key = tuple(sorted([u_name, v_name]))
        is_duplicate = pair_key in seen_pairs
        
        metadata["links"].append({
            "u": u_name,
            "v": v_name,
            "length": getattr(qc, 'length', 0.0),
            "success_probability": getattr(qc, 'success_prob', 1.0),
            "fidelity": getattr(qc, '_fidelity', 0.99),
            "is_parallel": is_duplicate
        })
        
        seen_pairs.add(pair_key)
    
    filepath = os.path.join(sim_folder, f"{filename}.json")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=4, ensure_ascii=False)
    
    print(f"  ✓ Metadata de enlaces guardada: {filename}.json")
    return filepath


def save_simulation_config(config_data: dict, sim_folder: str, filename: str = "simulation_config") -> str:
    """
    Guarda configuración general de la simulación.
    
    Args:
        config_data: Diccionario con datos de configuración
        sim_folder: Carpeta de simulación
        filename: Nombre del archivo
        
    Returns:
        Path del archivo guardado
    """
    config_data["timestamp"] = datetime.now().isoformat()
    
    filepath = os.path.join(sim_folder, f"{filename}.json")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, indent=4, ensure_ascii=False)
    
    print(f"  ✓ Configuración guardada: {filename}.json")
    return filepath


def print_simulation_summary(sim_folder: str):
    """Imprime resumen de archivos guardados en la carpeta."""
    print(f"\n{'='*60}")
    print(f"RESULTADOS DE SIMULACIÓN GUARDADOS EN:")
    print(f"  {sim_folder}")
    print(f"{'='*60}")
    
    if os.path.exists(sim_folder):
        files = os.listdir(sim_folder)
        print(f"Archivos guardados ({len(files)}):")
        for f in sorted(files):
            filepath = os.path.join(sim_folder, f)
            size = os.path.getsize(filepath) / 1024  # KB
            print(f"  • {f} ({size:.1f} KB)")
    print(f"{'='*60}\n")
