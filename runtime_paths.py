"""Keep generated session and test files outside the project root."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def runtime_directory(category):
    if category not in ('sessions', 'tests', 'backups'):
        raise ValueError('Unknown runtime directory')
    directory = ROOT / 'runtime' / category
    directory.mkdir(parents=True, exist_ok=True)
    return directory
