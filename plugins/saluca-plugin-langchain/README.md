# Saluca LangChain Inbound Plugin

This plugin wraps a LangChain agent (chains or agent executors) to allow inbound task dispatch via the Saluca SDK.

## Features

- Implements the `SalucaPlugin` contract with `direction: inbound`.
- Auto‑discovers agent capabilities.
- Dispatches tasks by writing `task.json` and `scope.json` to a temporary directory and invoking the LangChain CLI (simulated in tests).
- Emits audit events for discovery, dispatch receipt, and tool calls.

## Installation

```bash
pip install saluca-plugin-langchain
```

## Usage

```python
from saluca_plugin_langchain import LangChainPlugin

plugin = LangChainPlugin()
cap = plugin.discovery(my_langchain_agent)
result = plugin.dispatch_inbound(task_dict, scope_dict)
```

## Testing

Run the test suite with:

```bash
pytest -q
```
