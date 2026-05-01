from .plugin import LangChainPlugin

def dispatch_inbound(task, scope):
    """Execute inbound dispatch using the LangChain plugin."""
    plugin = LangChainPlugin()
    return plugin.dispatch_inbound(task, scope)
