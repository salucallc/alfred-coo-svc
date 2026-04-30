# saluca-plugin-sdk

A lightweight SDK for building Saluca plugins.

## Installation
```
pip install saluca-plugin-sdk==0.1.0
```

## Usage
```python
from saluca_plugin_sdk import SalucaPlugin

class MyPlugin(SalucaPlugin):
    direction = "bidirectional"
    # implement required methods ...
```
