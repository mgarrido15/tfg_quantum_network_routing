# Documentación de Métricas - Simulación Q-CAST vs Dijkstra

## 1. THROUGHPUT (EPS - Entrelazamientos Por Segundo)

```
Throughput = Éxitos_E2E / Tiempo_Simulación
```

## 2. PROBABILIDAD DE ÉXITO ESTIMADA

### Cálculo Pre-Simulación
```
P_est = ∏ P_enlace_i
```

**Basada en:**
- Parámetros físicos: `alpha`, `eta_s`, `eta_d`

## 3. FIDELIDAD (Calidad del Entrelazamiento)

### Fidelidad de un Enlace Individual (por Distancia)

```
fidelity_link = e^(-α × distance)
```

### Fidelidad de una Ruta Completa (Producto en Serie)

```
F_ruta = ∏ F_enlace_i (para cada enlace en la ruta)
```


## 4. ANCHO DE MEMORIA (Width - Qubits Mínimos)

Es el número mínimo de qubits de memoria que un nodo necesita para almacenar entrelazamientos temporalmente durante el tránsito por la ruta.



