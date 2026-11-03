import subprocess


def close_app(name):

    subprocess.run(["pkill", "-a", "-i", name])
