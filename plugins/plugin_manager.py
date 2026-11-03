import importlib
from pathlib import Path


class PluginManager:

    def __init__(self, registry):

        self.registry = registry


    def load_plugins(self):

        tools_dir = Path("tools")

        for plugin in tools_dir.iterdir():

            if not plugin.is_dir():
                continue

            try:

                module = importlib.import_module(
                    f"tools.{plugin.name}.register"
                )

                module.register(self.registry)

                print(f"[Plugin] {plugin.name} 読み込み成功")

            except ModuleNotFoundError:

                print(
                    f"[Plugin] {plugin.name} に register.py がありません"
                )

            except Exception as e:

                print(
                    f"[Plugin] {plugin.name} 読み込み失敗: {e}"
                )