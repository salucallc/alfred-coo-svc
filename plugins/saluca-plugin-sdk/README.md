# Saluca Plugin SDK

A lightweight SDK for building Saluca plugins.

## Features
- Abstract `SalucaPlugin` base class with direction‑conditional dispatch methods.
- Typed dataclasses for capabilities, registration, dispatch results, and audit events.
- Pydantic manifest validators for `ExternalAgent` and `ExternalSurface`.
- Built‑in audit hook integration with HMAC‑signed payloads.

## Installation
```
pip install saluca-plugin-sdk==0.1.0
```

## Quick start
```python
from saluca_plugin_sdk import SalucaPlugin

class MyPlugin(SalucaPlugin):
    direction = "inbound"
    def discover(self): ...
    def register(self): ...
    def lifecycle(self, action): ...
    def audit_hook(self, event): ...
    def unregister(self): ...
    def dispatch_inbound(self, agent_id, task, scope):
        # implement inbound handling
        return None
```
