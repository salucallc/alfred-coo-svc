import pytest
from saluca_plugin_langchain.plugin import LangChainPlugin

def test_plugin_contract():
    plugin = LangChainPlugin()
    assert plugin.direction == "inbound"
    assert plugin.default_mode == "cli"
