from registry.tool_registry import ToolRegistry
from plugins.plugin_manager import PluginManager

registry = ToolRegistry()

PluginManager(registry).load_plugins()

for tool in registry.list():

    print(tool.name)