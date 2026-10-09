"""Selectable capture inputs; each source owns its queue so switches cannot mix audio."""
import math
import queue
import threading
import time
import pyaudiowpatch as pyaudio
from process_audio import ProcessCapture


class AudioSource:
    def __init__(self, selection):
        self.selection = selection
        self.packets = queue.Queue(maxsize=100)
        self.error = None
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.thread = self.audio = self.stream = None
        self.rate, self.channels = 48000, 2
        self.name = ''

    def put(self, data):
        try:
            self.packets.put_nowait((data, time.perf_counter()))
        except queue.Full:
            self.error = RuntimeError('音訊佇列已滿，請重新套用音訊來源。')
            self.stopping.set()

    def start(self):
        if self.selection.get('mode') == 'process':
            if int(self.selection.get('pid', 0)) <= 0 or int(self.selection.get('created', 0)) <= 0:
                raise ValueError('無效的程式識別，請重新整理並選擇。')
            self.name = f"{self.selection.get('name', '程式')} · PID {self.selection['pid']}"
            self.thread = threading.Thread(target=self._process, daemon=True)
            self.thread.start()
            if not self.ready.wait(10):
                self.close()
                raise RuntimeError('程式音訊啟動逾時，請重試。')
            if self.error:
                self.close()
                raise self.error
        elif self.selection.get('mode') == 'device':
            try:
                self.audio = pyaudio.PyAudio()
                device = self.audio.get_default_wasapi_loopback()
                self.name = device['name']
                self.rate, self.channels = int(device['defaultSampleRate']), int(device['maxInputChannels'])
                unit = self.rate // math.gcd(self.rate, 16000)
                frames = max(1, round(self.rate * .1 / unit)) * unit
                self.stream = self.audio.open(format=pyaudio.paInt16, channels=self.channels,
                    rate=self.rate, input=True, input_device_index=int(device['index']),
                    frames_per_buffer=frames, stream_callback=self._callback)
            except Exception:
                self.close()
                raise
        else:
            raise ValueError('未知的音訊來源模式。')
        return self

    def _callback(self, data, frame_count, time_info, status):
        if self.stopping.is_set(): return None, pyaudio.paComplete
        if status:
            self.error = RuntimeError(f'輸出裝置音訊中斷（{status}），請重新套用來源。')
            return None, pyaudio.paAbort
        self.put(data)
        return None, pyaudio.paContinue

    def _process(self):
        native = ProcessCapture(int(self.selection['pid']), int(self.selection['created']))
        pending = bytearray()
        try:
            native.open()
            self.ready.set()
            while not self.stopping.is_set():
                pending.extend(native.read())
                while len(pending) >= 19200:  # 100 ms, 48 kHz stereo PCM16
                    self.put(bytes(pending[:19200]))
                    del pending[:19200]
        except Exception as exc:
            self.error = exc
        finally:
            if pending and self.error is None:
                self.put(bytes(pending))
            native.close()
            self.ready.set()

    def read(self, timeout=.2):
        try:
            return self.packets.get(timeout=timeout)
        except queue.Empty:
            if self.error: raise self.error
            if self.stream is not None and not self.stream.is_active():
                raise RuntimeError('輸出裝置擷取已停止，請重新套用來源。')
            raise

    def drain(self):
        while True:
            try: yield self.packets.get_nowait()
            except queue.Empty: return

    def close(self):
        self.stopping.set()
        if self.stream is not None:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
        if self.audio is not None:
            self.audio.terminate()
            self.audio = None
        if self.thread is not None:
            self.thread.join(timeout=2)
