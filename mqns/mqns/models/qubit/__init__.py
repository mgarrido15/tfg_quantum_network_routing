from mqns.models.qubit.qubit import Qubit
from mqns.models.qubit.state import QState

__all__ = [
    "QState",
    "Qubit",
]

for name in __all__:
    globals()[name].__module__ = __name__
