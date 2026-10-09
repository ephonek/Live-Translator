"""Qwen3-ASR 1.7B, native Transformers, 16 kHz mono input."""
import numpy as np
import torch
from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration
from transformers.utils import logging
from huggingface_hub.utils import disable_progress_bars

MODEL_ID = 'Qwen/Qwen3-ASR-1.7B-hf'


class ASRLengthLimitError(RuntimeError):
    """Recoverable failure of a single audio segment, not a device failure."""


class QwenASR:
    def __init__(self):
        if not torch.cuda.is_available():
            raise RuntimeError('PyTorch 沒有偵測到 CUDA。')
        disable_progress_bars()
        logging.disable_progress_bar()
        self.processor = AutoProcessor.from_pretrained(MODEL_ID)
        self.model = Qwen3ASRForConditionalGeneration.from_pretrained(
            MODEL_ID, dtype=torch.float16, attn_implementation='sdpa',
        ).to('cuda').eval()

    def transcribe(self, audio, language):
        audio = np.asarray(audio, dtype=np.float32)
        return self._transcribe_bounded(audio, language, depth=0)

    def _transcribe_bounded(self, audio, language, depth):
        try:
            return self._transcribe_once(audio, language)
        except ASRLengthLimitError:
            # At most 7 model calls (original + 2 halves + 4 quarters).
            # Never retry GPU/runtime faults; those need a real shutdown.
            if depth >= 2 or len(audio) < 2 * 16000:
                raise
            midpoint = len(audio) // 2
            print(f'ASR 輸出達上限，將 {len(audio)/16000:.1f}s 音訊拆半重試（第 {depth+1} 層）。', flush=True)
            left = self._transcribe_bounded(audio[:midpoint], language, depth+1)
            right = self._transcribe_bounded(audio[midpoint:], language, depth+1)
            separator = ' ' if language == 'en' and left and right else ''
            return left + separator + right

    def _transcribe_once(self, audio, language):
        audio = np.asarray(audio, dtype=np.float32)
        if not audio.size or np.max(np.abs(audio)) < 0.0001:
            return ''
        inputs = self.processor.apply_transcription_request(
            audio=audio, language=language,
        ).to(self.model.device, self.model.dtype)
        with torch.inference_mode():
            output = self.model.generate(**inputs, max_new_tokens=256, do_sample=False)
        generated = output[:, inputs['input_ids'].shape[1]:]
        eos = self.model.generation_config.eos_token_id
        if eos is None:
            eos = self.processor.tokenizer.eos_token_id
        eos = set(eos if isinstance(eos, (list, tuple)) else [eos])
        ended = bool(generated.numel()) and int(generated[0, -1].item()) in eos
        if generated.shape[1] >= 256 and not ended:
            raise ASRLengthLimitError('辨識達 256-token 上限且未正常結束。')
        return self.processor.decode(generated, return_format='transcription_only')[0].strip()
