"""Lightweight model catalogue, shared by the UI and ASR process."""
MODELS = {
    'qwen': {'label': 'Qwen3-ASR-1.7B', 'languages': ('ja', 'en')},
    'turbo': {'label': 'Whisper large-v3-turbo', 'languages': ('ja', 'en'),
              'ct2': 'mobiuslabsgmbh/faster-whisper-large-v3-turbo',
              'hf': 'openai/whisper-large-v3-turbo'},
    'kotoba': {'label': 'Kotoba-Whisper v2.0（日文）', 'languages': ('ja',),
               'ct2': 'kotoba-tech/kotoba-whisper-v2.0-faster',
               'hf': 'kotoba-tech/kotoba-whisper-v2.0'},
}


class ASRLengthLimitError(RuntimeError):
    """Recoverable failure of a single audio segment."""
