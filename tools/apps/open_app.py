import subprocess


def open_app(name):

    subprocess.run(["open", "-a", name])