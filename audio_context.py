"""Prepend only the previous fresh segment's tail; never deduplicate transcripts."""
import numpy as np


class AudioContext:
    def __init__(self, sample_rate=16000, max_seconds=5.0):
        self.sample_rate = sample_rate
        self.limit = int(max_seconds * sample_rate)
        self.clear()

    def clear(self):
        self.tail = np.empty(0, dtype=np.float32)
        self.end = None
        self.epoch = None

    def prepend(self, fresh, start, epoch, seconds):
        count = min(self.limit, max(0, round(seconds * self.sample_rate)))
        continuous = (epoch == self.epoch and self.end is not None
                      and abs(start - self.end) < 0.5 / self.sample_rate)
        prefix = self.tail[-count:] if continuous and count else self.tail[:0]
        overlap = len(prefix) / self.sample_rate
        audio = np.concatenate((prefix, fresh)) if len(prefix) else fresh
        # Store only NEW samples, not the combined input. Disabled mode retains none.
        self.tail = fresh[-self.limit:].copy() if count and len(fresh) else self.tail[:0]
        self.end = start + len(fresh) / self.sample_rate
        self.epoch = epoch
        return audio, start - overlap, overlap
