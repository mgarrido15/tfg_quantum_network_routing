# Documentación de Métricas - Simulación Q-CAST vs Dijkstra

## 1. THROUGHPUT (EPS - Entrelazamientos Por Segundo)

```
Throughput = Éxitos_E2E / Tiempo_Simulación
```

## 2. PROBABILIDAD DE ÉXITO ESTIMADA

### Cálculo Pre-Simulación
```
P_enlace = eta_s * (eta_d * p_transmision(length / 2, alpha))^2
P_ruta_un_canal = (∏ P_enlace_i) * q_swap^(saltos - 1)
```

**Basada en:**
- Parámetros físicos: `alpha`, `eta_s`, `eta_d`
- Probabilidad de éxito del swapping: `q_swap`

La eficiencia de fuente `eta_s` aparece una sola vez porque el emisor central
produce un par; la supervivencia y detección se exige para cada uno de los dos
fotones.

## 3. FIDELIDAD (Calidad del Entrelazamiento)

### Fidelidad de un Enlace Individual (por Distancia)

```
w_link = e^(-r × distance)
F_link = (3 × w_link + 1) / 4
```

`r` es la tasa del modelo de error de transferencia, expresada en km^-1. No
debe confundirse con `alpha`, que controla la pérdida de fotones y, por tanto,
la probabilidad de generación, no la calidad condicional del EPR recibido.

### Fidelidad de una Ruta Completa (EPR Werner)

```
w_ruta = ∏ w_enlace_i
F_ruta = (3 × w_ruta + 1) / 4
```

El swapping multiplica parámetros Werner, no fidelidades directamente. La
fidelidad observada también incorpora el decaimiento de ambos extremos durante
el almacenamiento. Si no se entrega ningún EPR, la fidelidad observada es
`null`/no disponible y su número de muestras es cero; no se representa como
una fidelidad medida de `0.0`.

## 4. ANCHO DE MEMORIA (Width - Qubits Mínimos)

Es el número mínimo de qubits de memoria que un nodo necesita para almacenar entrelazamientos temporalmente durante el tránsito por la ruta.

## 5. PROBABILIDAD DE ÉXITO A NIVEL DE APLICACIÓN

Para las gráficas por pareja se usa la fracción de ciclos en la que una pareja S-D obtiene **al menos un** éxito E2E:

```
P_app(par) = ciclos_con_al_menos_un_exito(par) / ciclos_totales
```

La probabilidad global a nivel de aplicación es el promedio sobre las solicitudes activas en cada ciclo:

```
P_app(global) = parejas_exitosas_por_ciclo_media / solicitudes_por_ciclo
```

Las entregas múltiples de EPR para una misma solicitud y ciclo cuentan en el
throughput, pero no multiplican la probabilidad de satisfacción de esa solicitud.

## 6. EFICIENCIA POR INTENTO ELEMENTAL

```
eficiencia_intento = exitos_E2E / intentos_de_generacion_elemental
```

Esta métrica relaciona entregas con esfuerzo de generación. No es una
probabilidad de éxito a nivel de aplicación y se muestra por separado.

De este modo, si el escenario tiene 30 solicitudes base, la referencia correcta en las gráficas es **30 solicitudes por ciclo**, no el número total de copias internas creadas para mantener carga durante toda la simulación.

## 7. EJEMPLO MULTICANAL

Para un enlace directo con cuatro canales independientes y `p=0.6`:

```
E[EPR generados por ciclo] = 4 × 0.6 = 2.4
P(al menos un EPR) = 1 - (1 - 0.6)^4 = 0.9744
```

MultiEnt puede aceptar los 2.4 EPR/ciclo de media. Q-CAST normal puede reservar
los cuatro canales, pero acepta como máximo un EPR por solicitud y ciclo. En
ambos casos, la satisfacción de la solicitud es como máximo uno en ese ciclo.

## 8. DENOMINADORES NULOS

- Sin intentos elementales, la eficiencia por intento no se interpreta como
  evidencia de éxito; el código evita dividir por cero.
- Sin solicitudes activas o admitidas, la tasa correspondiente no debe
  presentarse como una observación física positiva.
- Sin EPR entregados, la fidelidad observada es `null` y el número de muestras
  es cero.
- El throughput sí es cero cuando no existen entregas durante un tiempo de
  simulación positivo.

## 9. IDENTIDAD Y AGREGACIÓN

Dos entradas de solicitud con los mismos extremos son demandas independientes
y reciben identificadores distintos. La asignación de recursos y el éxito se
registran primero por identificador. Las métricas por pareja S-D agrupan después
esas demandas de forma explícita, sin perder su identidad original.

## 10. REFERENCIAS Y ALCANCE DEL MODELO

- R. F. Werner, “Quantum states with Einstein-Podolsky-Rosen correlations
  admitting a hidden-variable model”, *Physical Review A*, 40, 4277 (1989),
  DOI: `10.1103/PhysRevA.40.4277`.
- El motor base procede de MQNS v0.1.0; la procedencia y licencia están
  documentadas en el README del proyecto.

Las ecuaciones anteriores describen el modelo implementado, no un gemelo
digital de hardware cuántico concreto. La independencia entre canales, el
modelo exponencial de pérdidas, el estado Werner y una probabilidad constante
de swapping son simplificaciones explícitas.
