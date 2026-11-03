from agent.agent import Agent
from context import Context

ctx = Context()

agent = Agent()

agent.set_context(ctx)

command = agent.plan("メモ")

ctx.add("メモ", command)

print(agent.plan("もう一度"))