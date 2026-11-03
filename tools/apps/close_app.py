from agent.process import run_command


def close_app(name):

    run_command(["pkill", "-a", "-i", name])
