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

| `--seed` | Base seed from which independent topology, physical-event and request-priority seeds are derived for each repetition. 
| `--repetitions` | Number of independent topology/physical-seed replications performed at each experimental point. Each replication uses a new topology and one physical realization. 
| `--from-results` | Regenerates the plots from an existing `scaling_experiment_results.json` without rerunning the simulations. 
| `--output-dir` | Directory in which regenerated plots are saved. It is used together with `--from-results`. 
| `--reset-memories-each-cycle` / `--no-reset-memories-each-cycle` | Enables or disables quantum-memory reset at the start of each cycle. Reset is enabled by default. |
| `--help` | Displays the command-line help. 

### Algorithm comparison

The algorithm comparison script creates a degraded version of the selected
scenario and evaluates all routing algorithms under the same conditions:

```bash
python scripts/comparison_algorithms.py [options]
```

Available options:

| `--scenario` | Base JSON scenario used by the experiment. 
| `--sim-time` | Total simulated time in seconds. 
| `--fidelity-scale` | Multiplicative degradation applied to link fidelity. 
| `--min-link-fidelity` | Minimum link fidelity allowed after degradation. 
| `--alpha-scale` | Multiplicative factor applied to the attenuation coefficient. 
| `--length-scale` | Multiplicative factor applied to link lengths. 
| `--channels-per-link` | Number of parallel physical quantum channels per link. Must be at least one. 
| `--eta-d` | Detector efficiency used to calculate physical link success probabilities. Must be between zero and one. 
| `--log-level` | Logging level: `CRITICAL`, `FATAL`, `ERROR`, `WARN`, `INFO`, or `DEBUG`. 
| `--request-fraction` | Fraction of scenario requests to simulate. Must be in the interval `(0, 1]`. 
| `--request-seed` | Seed used to sample requests when `--request-fraction` is less than one. 
| `--simulation-seed` | Physical-event seed reset before every compared algorithm. 
| `--priority-seed` | Independent seed used to shuffle request priority in every cycle. 
| `--swap-policy` | Common swapping order used by every compared algorithm. 
| `--swap-success-prob` | DSuccess probability of each swapping operation. Must be between zero and one. 
| `--help` | Displays the command-line help. 


### Final basic, large and near-ideal scenarios

The three final scenarios are:


| Basic | `escenario_basico.json` | 8 nodes, 10 links, 2 requests | Two links near 0.30; remaining links 0.70–0.85
| Large | `escenario_grande.json` | 40 nodes, 73 links, 9 requests | 0.45–0.55 
| Near ideal | `escenario_ideal.json` | 40 nodes, 73 links, 30 requests | 0.95–0.99 


```bash
python scripts/build_final_scenarios.py
```

Run all three final comparisons without modifying their scenarios:

```bash
python scripts/run_final_scenarios.py --scenario all --sim-time 1000
```

The final basic, large and near-ideal campaign uses a swapping success
probability of `0.85` for every algorithm, matching the scaling campaign.

Run only one scenario:

```bash
python scripts/run_final_scenarios.py --scenario basic --sim-time 1000
python scripts/run_final_scenarios.py --scenario large --sim-time 1000
python scripts/run_final_scenarios.py --scenario ideal --sim-time 1000
```

### Multi-seed convergence and paired comparisons

The convergence runner executes every algorithm on the same physical and
request-priority seeds. It reports means, medians, 95% Student-t confidence
intervals, paired algorithm differences and the Q-CAST backup ablation:

```bash
python scripts/comparison_algorithms.py --repetitions 14 --sim-time 1000 --policies l2r
```
