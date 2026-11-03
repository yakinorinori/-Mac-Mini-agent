from registry.tool_registry import ToolRegistry
from plugins.plugin_manager import PluginManager


class ToolManager:

    def __init__(self):

        self.registry = ToolRegistry()
        self.plugin_manager = PluginManager(self.registry)
        self.plugin_manager.load_plugins()

    def execute(self, command):

        # impossible 判定時の no-op
        if command.tool == "__noop__":
            print("[ToolManager] noop: 何もしません")
            return

        tool = self.registry.get(command.tool)

        if tool is None:
            raise ValueError(f"ツールが見つかりません: {command.tool}")

        return tool.function(**command.args)

    def execute_plan(self, plan):
        """Planの各ステップを順番に実行する。失敗した場合は何ステップ目かを添えて例外を送出する。

        戻り値は (step, 実行結果) のリスト。スクリーンショットのパスなど、
        呼び出し側(main.py)がユーザーへ返す必要がある結果を受け取れるようにする。
        """

        results = []

        for step in plan.steps:
            try:
                result = self.execute(step)
                results.append((step, result))
            except Exception as e:
                raise RuntimeError(f"ステップ{step.tool}の実行に失敗しました: {e}") from e

        return results
