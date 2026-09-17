# tfg_quantum_network_routing

**Author:** Marco Garrido González    
**University:** EETAC

## Project Description

The primary objective of this project is to evaluate and compare the performance of two different routing protocols in variable quantum network environments.

## Repository Structure

* **`/scripts`**: Contains the executable files and scripts used to launch the simulations and process results.
* **`/mqns`**: A snapshot of the **MQNS v0.1.0** (Modular Quantum Network Simulator) engine, used as the underlying technological core.

---

## Based on MQNS

This project reuses components from [MQNS v0.1.0](https://github.com/usnistgov/mqns), which is licensed under the GNU General Public License v3.0.

This is not a fork of the official MQNS repository, but rather a standalone project that incorporates a snapshot of MQNS's implementation - specifically the discrete-event simulation engine, noise modeling framework, and code structure. Substantial modifications have been made to support dynamic routing protocols and enhanced entanglement management capabilities.

This project is therefore licensed under the GPLv3. See the LICENSE file for details.

---

## Installation and Usage

To set up the environment and replicate the simulations, follow these steps:

### 1. Prerequisites
* Python 3.12 or higher.
* A virtual environment is highly recommended.

### 2. Environment Setup
```bash
# Clone the repository
git clone https://github.com/mgarrido15/tfg_quantum_network_routing.git
cd tfg_quantum_network_routing

# Install dependencies and the MQNS snapshot
pip install -r mqns/requirements.txt
pip install -e ./mqns
```

---

## Running the Simulation Scripts

Run the following commands from the repository root.

### Algorithm semantics

All multicircuit variants may reserve every parallel channel permitted by the
route and node memories. Q-CAST and the multicircuit Dijkstra variants accept
at most one end-to-end EPR per request and cycle. Q-CAST MultiEnt accepts every
valid end-to-end EPR delivered in that request-cycle. Satisfaction metrics
always count a request at most once per cycle, independently of the number of
EPR deliveries.

If a link contains `prob`, that explicit synthetic probability is used and
must be in `[0, 1]`. If it is omitted, the simulator derives the probability
from `length`, `alpha`, `eta_s` and `eta_d`. Simulation durations must be exact
multiples of the four-second routing cycle.


### Scaling experiments

The scaling study evaluates throughput as the network size and the number of
source-destination pairs increase. The network-size campaign uses a spatial
constant-density model: the deployment area grows with the number of nodes,
local connectivity and per-link resources remain constant, and S-D endpoints
are sampled from the 20% of node pairs with the greatest spatial separation.
Consequently, larger networks produce longer end-to-end paths without
prescribing their hop count or throughput. Link probabilities follow the same source-in-midpoint model in every scenario:
the source emits a pair with efficiency `eta_s`, and both photons must survive
half of the link and be detected. Each swapping operation succeeds with
probability 0.85. The S-D-pair campaign is a separate fixed-size experiment on
100-node random topologies.

```bash
python scripts/scaling_experiments.py [options]
```

Available options:

| Option | Value | Default | Description |
| --- | --- | --- | --- |
| `--seed` | Integer | `7` | Base seed from which independent topology, physical-event and request-priority seeds are derived for each repetition. |
| `--repetitions` | Integer | `10` | Number of independent topology/physical-seed replications performed at each experimental point. Each replication uses a new topology and one physical realization. |
| `--from-results` | Path | None | Regenerates the plots from an existing `scaling_experiment_results.json` without rerunning the simulations. |
| `--output-dir` | Path | Results directory | Directory in which regenerated plots are saved. It is used together with `--from-results`. |
| `-h`, `--help` | — | — | Displays the command-line help. |

### Algorithm comparison

The algorithm comparison script creates a degraded version of the selected
scenario and evaluates all routing algorithms under the same conditions:

```bash
python scripts/comparison_algorithms_badlinks_original.py [options]
```

Available options:

| Option | Value | Default | Description |
| --- | --- | --- | --- |
| `--scenario` | Path | `escenario_grande_multicanal_w.json` | Base JSON scenario used by the experiment. |
| `--sim-time` | Decimal | `1000.0` | Total simulated time in seconds. |
| `--fidelity-scale` | Decimal | `0.95` | Multiplicative degradation applied to link fidelity. |
| `--min-link-fidelity` | Decimal | `0.92` | Minimum link fidelity allowed after degradation. |
| `--alpha-scale` | Decimal | `1.05` | Multiplicative factor applied to the attenuation coefficient. |
| `--length-scale` | Decimal | `1.02` | Multiplicative factor applied to link lengths. |
| `--channels-per-link` | Integer | `5` | Number of parallel physical quantum channels per link. Must be at least one. |
| `--eta-d` | Decimal | `0.8` | Detector efficiency used to calculate physical link success probabilities. Must be between zero and one. |
| `--log-level` | Text | `WARN` | Logging level: `CRITICAL`, `FATAL`, `ERROR`, `WARN`, `INFO`, or `DEBUG`. |
| `--request-fraction` | Decimal | `1.0` | Fraction of scenario requests to simulate. Must be in the interval `(0, 1]`. |
| `--request-seed` | Integer | `42` | Seed used to sample requests when `--request-fraction` is less than one. |
| `--simulation-seed` | Integer | `10007` | Physical-event seed reset before every compared algorithm. |
| `--priority-seed` | Integer | `20011` | Independent seed used to shuffle request priority in every cycle. |
| `--swap-policy` | Text | `l2r` | Common swapping order used by every compared algorithm. |
| `--swap-success-prob` | Decimal | `0.85` | Success probability of each swapping operation. Must be between zero and one. |
| `-h`, `--help` | — | — | Displays the command-line help. |

Run the comparison with its default configuration:

```bash
python scripts/comparison_algorithms_badlinks_original.py
```

Use `--scenario-mode as-is` for an already materialized scenario. In this mode
the runner copies the JSON unchanged into the result folder and does not apply
length, attenuation, fidelity, detector-efficiency or channel transformations.

### Final basic, large and near-ideal scenarios

The three final scenarios are:

| Name | File | Topology | Link success probabilities | Channels |
| --- | --- | --- | --- | --- |
| Basic | `escenario_basico.json` | 8 nodes, 10 links, 2 requests | Two links near 0.30; remaining links 0.70–0.85 | 5 |
| Large | `escenario_grande_final.json` | 40 nodes, 73 links, 30 requests | 0.45–0.55 | 5 |
| Near ideal | `escenario_casi_ideal_final.json` | Same as large | 0.95–0.99 | 5 |

Probabilities are derived from physical distances; the final JSON files do not
contain an explicit `prob` override. Regenerate the large and near-ideal files
deterministically with:

```bash
python scripts/build_final_scenarios.py
```

Run all three final comparisons without modifying their scenarios:

```bash
python scripts/run_final_scenarios.py --scenario all --sim-time 1000
```

Run only one scenario:

```bash
python scripts/run_final_scenarios.py --scenario basic --sim-time 1000
python scripts/run_final_scenarios.py --scenario large --sim-time 1000
python scripts/run_final_scenarios.py --scenario ideal --sim-time 1000
```

Every command creates a new `outputs/final_scenarios_<timestamp>/` directory.
Each selected scenario has its own child directory containing the effective
scenario, throughput, fidelity, satisfied-pair and topology plots, raw JSON
results, link metadata and a reproducibility manifest.

### Multi-seed convergence and paired comparisons

The convergence runner executes every algorithm on the same physical and
request-priority seeds. It reports means, medians, 95% Student-t confidence
intervals, paired algorithm differences and the Q-CAST backup ablation:

```bash
python scripts/comparison_algorithms_convergence.py --repetitions 14 --sim-time 1000 --policies l2r
```

The default throughput precision target is a 95% confidence-interval
half-width no larger than `max(0.005 EPS, 10% of the mean)`. The generated JSON
reports whether this target was reached and estimates the required repetitions
from the observed standard deviation. This estimate is configuration-specific;
other scenarios and swapping policies must verify their own dispersion.

| Option | Value | Default | Description |
| --- | --- | --- | --- |
| `--repetitions` | Integer | `10` | Independent physical and priority seed pairs. Must be at least two. |
| `--seed-base` | Integer | `10007` | First physical-event seed. |
| `--priority-seed-base` | Integer | `20011` | First independent request-priority seed. |
| `--policies` | Text list | `l2r asap` | Swapping policies evaluated separately. |
| `--scenario-mode` | Text | `as-is` | Copy the effective basic scenario or apply the legacy transformation with `historical-compatible`. |
| `--historical-results` | Path | Basic historical output | Historical `analysis_results.json` included only as a non-statistical reference. |
| `--throughput-absolute-precision` | Decimal | `0.005` | Absolute EPS half-width used near zero. |
| `--throughput-relative-precision` | Decimal | `0.10` | Relative half-width used away from zero. |

### Channel-count ablation

The channel ablation repeats the basic scenario with 1, 2, 3 and 5 physical
channels per link while preserving its topology, requests, link physics and
memory capacities. Each algorithm uses the same physical and request-priority
seed pairs at every experimental point. The runner stores the generated
scenarios, raw runs, Student-t intervals, paired comparisons, the backup
ablation, a throughput plot and a reproducibility manifest.

```bash
python scripts/channel_ablation.py --repetitions 10 --sim-time 1000 --policies l2r
```

The default output is a new timestamped directory under `outputs`; existing
historical results are not overwritten. Use `--channel-counts` to select other
positive channel counts and `--output-dir` to choose a new explicit directory.
The source scenario must provision enough memory at every node for the largest
selected channel count.

### Historical results

The preserved historical outputs were produced by earlier simulator semantics.
Their values and limitations are audited in
`Archivos_GuiaProyecto/COMPARACION_RESULTADOS_HISTORICOS.md`. They are useful as
descriptive legacy evidence, but they are not numerically interchangeable with
results from the corrected lifecycle, metrics and multichannel semantics.

The complete and authoritative option list for either script can always be
displayed with:

```bash
python scripts/scaling_experiments.py --help
python scripts/comparison_algorithms_badlinks_original.py --help
python scripts/comparison_algorithms_convergence.py --help
python scripts/channel_ablation.py --help
```
