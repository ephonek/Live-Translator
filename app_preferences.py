"""Validated, atomic preferences. Runtime and UI have separate writers."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent / 'config'


def load_preferences(section):
    try:
        data = json.loads((CONFIG_DIR / (section + '.json')).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preferences(section, data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=section+'-', suffix='.tmp', dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(name, CONFIG_DIR / (section + '.json'))
    finally:
        Path(name).unlink(missing_ok=True)


def choice(data, key, allowed, default):
    value = data.get(key)
    return value if isinstance(value, str) and value in allowed else default


def bounded(data, key, default, low, high):
    value = data.get(key)
    return max(low, min(high, value)) if type(value) is int else default


def flag(data, key, default):
    value = data.get(key)
    return value if type(value) is bool else default


def recommended_model(memory_mib=None):
    if memory_mib is None:
        try:
            result = subprocess.run(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            memory_mib = int(result.stdout.splitlines()[0].strip())
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return 'turbo'
    return 'qwen' if memory_mib >= 8000 else 'turbo'


def resolve_source(saved, applications):
    """Never reuse a PID across sessions, or silently capture all audio."""
    if saved.get('source_mode') != 'process':
        return {'mode': 'device', 'request_id': 'initial'}
    name = saved.get('source_name')
    matches = [app for app in applications if isinstance(name, str) and app.get('name', '').casefold() == name.casefold()]
    if len(matches) != 1:
        return None
    return {**matches[0], 'mode': 'process', 'request_id': 'initial'}
