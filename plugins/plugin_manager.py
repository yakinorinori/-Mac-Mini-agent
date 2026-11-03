import importlib
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


class PluginManager:

    def __init__(self, registry, tools_dir=TOOLS_DIR):

        self.registry = registry
        self.tools_dir = Path(tools_dir)


    def load_plugins(self):

        for plugin in sorted(self.tools_dir.iterdir()):

            if not (plugin / "register.py").is_file():
                continue

            try:

                module = importlib.import_module(
                    f"tools.{plugin.name}.register"
                )

                module.register(self.registry)

                print(f"[Plugin] {plugin.name} 読み込み成功")

            except Exception as e:

                print(
                    f"[Plugin] {plugin.name} 読み込み失敗: {e}"
                )
