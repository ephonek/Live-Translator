"""Bounded live audio queue. Preserve controls, replace oldest unprocessed audio."""
import queue


class LiveAudioQueue(queue.Queue):
    def put_audio(self, item):
        dropped = None
        with self.not_full:
            if self._qsize() >= self.maxsize:
                dropped = next((job for job in self.queue if isinstance(job, tuple)), None)
                if dropped is None:
                    raise queue.Full
                # ndarray equality is ambiguous; remove by identity/index.
                index = next(i for i, job in enumerate(self.queue) if job is dropped)
                del self.queue[index]
                self.unfinished_tasks -= 1
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()
        return dropped

    def put(self, item, block=True, timeout=None):
        if item is not None and not isinstance(item, dict):
            return super().put(item, block, timeout)
        # Controls cannot be rejected just because audio filled the queue.
        # Only the newest model generation is useful; stop markers are retained.
        with self.not_full:
            if isinstance(item, dict):
                old = [job for job in self.queue if isinstance(job, dict)]
                for job in old:
                    self.queue.remove(job)
                    self.unfinished_tasks -= 1
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()


def enqueue_translation(jobs, item, consumer, notify):
    """Backpressure preserves recognized text; dead consumers must not deadlock."""
    warned = False
    while consumer.is_alive():
        try:
            jobs.put(item, timeout=.2)
            return
        except queue.Full:
            if not warned:
                notify()
                warned = True
    raise RuntimeError('翻譯工作執行緒已停止。')
