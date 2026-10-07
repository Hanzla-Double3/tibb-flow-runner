import importlib.util
import sys
from pathlib import Path
from typing import Callable


def _load_module(path: Path):
    module_name = f"tibb_flow_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def discover_flows(target: Path) -> list[Callable]:
    files = [target] if target.is_file() else sorted(target.rglob("*.py"))
    flows: list[Callable] = []
    for f in files:
        if f.name.startswith("_"):
            continue
        module = _load_module(f)
        for attr in vars(module).values():
            if callable(attr) and getattr(attr, "__is_flow__", False):
                flows.append(attr)
    return flows
