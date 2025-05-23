from time import monotonic

class Battery:
    def __init__(self, capacity=100, drain_rate=1.0):
        """drain_rate: % per minute when active."""
        self.capacity = capacity
        self.level    = capacity
        self._last_ts = monotonic()
        self.drain_rate = drain_rate    # %/min

    def tick(self):
        now = monotonic()
        elapsed_minutes = (now - self._last_ts) / 60
        self._last_ts = now
        self.level = max(0, self.level - elapsed_minutes * self.drain_rate)

    def charge(self):
        self.level = self.capacity

    def as_percent(self):
        self.tick()
        return round(self.level, 1)
