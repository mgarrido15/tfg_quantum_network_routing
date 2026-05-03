from pathlib import Path

_inner_package_dir = Path(__file__).resolve().parent / "mqns"
if str(_inner_package_dir) not in __path__:
    __path__.append(str(_inner_package_dir))