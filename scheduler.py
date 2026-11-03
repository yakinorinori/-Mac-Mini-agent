import threading
from datetime import datetime, timedelta


class Scheduler:

    def schedule(self, seconds, callback):

        timer = threading.Timer(
            seconds,
            callback
        )

        timer.start()

        return timer