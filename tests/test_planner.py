from agent.planner import Planner
from context import Context

ctx = Context()

planner = Planner(ctx)

print(planner.plan("メモ"))