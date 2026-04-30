import importlib.util
import os
from pathlib import Path
from typing import Dict, Any

PLUGIN_ROOT = Path("/opt/alfred-coo/plugins/agents")

def _load_module_from_path(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise ImportError(f"Cannot load plugin from {path}")

def discover_plugins() -> Dict[str, Any]:
    """Scan PLUGIN_ROOT for plugin packages and return mapping of id to plugin object."""
    plugins: Dict[str, Any] = {}
    if not PLUGIN_ROOT.is_dir():
        return plugins
    for entry in PLUGIN_ROOT.iterdir():
        if entry.is_dir():
            init_file = entry / "__init__.py"
            if init_file.is_file():
                try:
                    module = _load_module_from_path(init_file)
                    plugin = getattr(module, "plugin", None)
                    if plugin:
                        plugins[entry.name] = plugin
                except Exception:
                    continue
    return plugins
