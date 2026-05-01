from .plugin import LangChainPlugin

def discover_agent(agent):
    """Convenience wrapper that returns the capabilities of a LangChain agent."""
    plugin = LangChainPlugin()
    return plugin.discovery(agent)
