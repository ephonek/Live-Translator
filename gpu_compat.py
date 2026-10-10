"""GPU policy without importing torch in the capture process or subtitle UI."""
import os
import re
import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def detect_gpu():
    # Older drivers may not expose compute_cap through nvidia-smi.
    for fields in ('name,memory.total,compute_cap', 'name,memory.total'):
        try:
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=' + fields, '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode:
                continue
            parts = [p.strip() for p in result.stdout.splitlines()[0].split(',')]
            name, memory = parts[0], int(parts[1])
            try:
                capability = tuple(int(x) for x in parts[2].split('.'))
                if len(capability) != 2:
                    capability = None
            except (IndexError, ValueError):
                capability = None
            if capability is None and re.search(r'GTX\s+10\d{2}(?:\D|$)', name, re.I):
                capability = (6, 1)
            return memory, capability
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            continue
    return None, None


def choose_compute_type(supported):
    for precision in ('int8_float16', 'int8_float32'):
        if precision in supported:
            return precision
    raise RuntimeError('GPU 不支援此 Whisper INT8 後端。請檢查顯卡、驅動並重新執行 install.bat。')


def check_native_backend(capability):
    if capability < (7, 0):
        raise RuntimeError('GTX 10 系列請使用 CUDA 12.6 的 Whisper turbo 或 Kotoba；'
                           '此 Whisper 後端不支援 Pascal。請重新執行 install.bat。')
