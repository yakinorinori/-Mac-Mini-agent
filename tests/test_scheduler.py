from scheduler import Scheduler

scheduler = Scheduler()

print("開始")

scheduler.schedule(
    5,
    lambda: print("5秒経過")
)

input()