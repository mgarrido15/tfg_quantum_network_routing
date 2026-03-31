# tfg_quantum_network_routing

**Author:** Marco Garrido González    
**University:** EETAC

## Project Description

The primary objective of this project is to evaluate and compare the performance of two different routing protocols in variable quantum network environments.

## Repository Structure

* **`/scripts`**: Contains the executable files and scripts used to launch the simulations and process results.
* **`/mqns`**: A snapshot of the **MQNS v0.1.0** (Modular Quantum Network Simulator) engine, used as the underlying technological core.

---

## Créditos y Licencia

This project reuses components from [MQNS v0.1.0](https://github.com/usnistgov/mqns), which is licensed under the GNU General Public License v3.0.

This is not a fork of the official MQNS repository, but rather a standalone project that incorporates a snapshot of MQNS's implementation - specifically the discrete-event simulation engine, noise modeling framework, and code structure. Substantial modifications have been made to support dynamic routing protocols and enhanced entanglement management capabilities.

This project is therefore licensed under the GPLv3. See the LICENSE file for details.

---

## Installation and Usage

To set up the environment and replicate the simulations, follow these steps:

### 1. Prerequisites
* Python 3.8 or higher.
* A virtual environment is highly recommended.

### 2. Environment Setup
```bash
# Clone the repository
git clone [https://github.com/mgarrido15/tfg_quantum_network_routing.git](https://github.com/mgarrido15/tfg_quantum_network_routing.git)
cd tfg_quantum_network_routing

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate 

# Install dependencies and the MQNS snapshot
pip install -r mqns/requirements.txt
pip install -e ./mqns
