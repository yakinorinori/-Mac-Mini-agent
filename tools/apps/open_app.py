from agent.process import run_command


def open_app(name):

    run_command(["open", "-a", name])
