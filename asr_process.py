"""Own and reap one GPU subprocess; no GPU imports in the capture process."""
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from asr_models import ASRLengthLimitError


class ASRProcess:
    def __init__(self, key, cache_dir, cancelled=lambda: False):
        self.key = key
        self.last_stats = []
        self.responses = queue.Queue()
        self.diagnostics = deque(maxlen=12)
        self.process = subprocess.Popen(
            [sys.executable, '-X', 'utf8', str(Path(__file__).with_name('asr_service.py')), key, str(cache_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._drain, daemon=True)]
        for thread in self.readers:
            thread.start()
        try:
            ready = self._receive(1800, cancelled)
            if ready['event'] != 'ready':
                raise RuntimeError('ASR 啟動協定錯誤。')
            self.backend = ready['backend']
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except ValueError:
                    self.diagnostics.append(line.rstrip())
        finally:
            self.responses.put({'event': 'error', 'error': 'ASR 程序已結束。'})

    def _drain(self):
        for line in self.process.stderr:
            self.diagnostics.append(line.rstrip())

    def _receive(self, timeout, cancelled=lambda: False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled():
                raise InterruptedError('已取消模型載入。')
            try:
                message = self.responses.get(timeout=.1)
            except queue.Empty:
                continue
            if message['event'] == 'length_error':
                self.last_stats = message.get('stats', [])
                raise ASRLengthLimitError(message['error'])
            if message['event'] == 'error':
                raise RuntimeError(message['error'])
            return message
        raise TimeoutError('ASR 等待逾時；請檢查網路、模型快取或 GPU。')

    def transcribe(self, audio, language):
        payload = {'language': language, 'audio': base64.b64encode(audio.astype('<f4', copy=False).tobytes()).decode('ascii')}
        self.process.stdin.write(json.dumps(payload) + '\n')
        self.process.stdin.flush()
        self.last_stats = []
        result = self._receive(120)
        self.last_stats = result.get('stats', [])
        return result['text']

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        for thread in self.readers:
            thread.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
