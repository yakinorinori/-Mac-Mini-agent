from context import Context

ctx = Context()

ctx.add(
    "メモ",
    {
        "tool":"open_app"
    }
)

print(ctx.get_last_command())

print(ctx.get_history())