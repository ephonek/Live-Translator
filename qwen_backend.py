"""Qwen3-ASR 1.7B, native Transformers, 16 kHz mono input."""
import time
import numpy as np
import torch
from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration
from transformers.utils import logging
from huggingface_hub.utils import disable_progress_bars

MODEL_ID = 'Qwen/Qwen3-ASR-1.7B-hf'
SEGMENT_BUDGET_SECONDS = 8.0


from asr_models import ASRLengthLimitError


class QwenASR:
    def __init__(self):
        if not torch.cuda.is_available():
            raise RuntimeError('PyTorch 沒有偵測到 CUDA。')
        capability = torch.cuda.get_device_capability(0)
        if capability < (7, 0) and torch.version.cuda != '12.6':
            raise RuntimeError('GTX 10 系列請使用 CUDA 12.6，重新執行 install.bat。')
        dtype = torch.float32 if capability < (7, 0) else torch.float16
        disable_progress_bars()
        logging.disable_progress_bar()
        self.processor = AutoProcessor.from_pretrained(MODEL_ID)
        self.model = Qwen3ASRForConditionalGeneration.from_pretrained(
            MODEL_ID, dtype=dtype, attn_implementation='sdpa',
        ).to('cuda').eval()

    def transcribe(self, audio, language):
        audio = np.asarray(audio, dtype=np.float32)
        self.last_stats = []
        return self._transcribe_bounded(audio, language, depth=0, deadline=time.monotonic()+SEGMENT_BUDGET_SECONDS)

    def _transcribe_bounded(self, audio, language, depth, deadline):
        try:
            return self._transcribe_once(audio, language, deadline)
        except ASRLengthLimitError:
            # At most 7 model calls (original + 2 halves + 4 quarters).
            # Never retry GPU/runtime faults; those need a real shutdown.
            if time.monotonic() >= deadline or depth >= 2 or len(audio) < 2 * 16000:
                raise
            midpoint = len(audio) // 2
            print(f'ASR 輸出達上限，將 {len(audio)/16000:.1f}s 音訊拆半重試（第 {depth+1} 層）。', flush=True)
            left = self._transcribe_bounded(audio[:midpoint], language, depth+1, deadline)
            right = self._transcribe_bounded(audio[midpoint:], language, depth+1, deadline)
            separator = ' ' if language == 'en' and left and right else ''
            return left + separator + right

    def _transcribe_once(self, audio, language, deadline):
        started = time.monotonic()
        audio = np.asarray(audio, dtype=np.float32)
        if not audio.size or np.max(np.abs(audio)) < 0.0001:
            return ''
        inputs = self.processor.apply_transcription_request(
            audio=audio, language=language,
        ).to(self.model.device, self.model.dtype)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ASRLengthLimitError('此段辨識已超過 8 秒預算，略過以避免積壓。')
        with torch.inference_mode():
            # One shared budget covers the original call AND split retries.
            # Transformers checks max_time between decoding steps, not within a GPU kernel.
            output = self.model.generate(**inputs, max_new_tokens=256, do_sample=False, max_time=remaining)
        generated = output[:, inputs['input_ids'].shape[1]:]
        eos = self.model.generation_config.eos_token_id
        if eos is None:
            eos = self.processor.tokenizer.eos_token_id
        eos = set(eos if isinstance(eos, (list, tuple)) else [eos])
        ended = bool(generated.numel()) and int(generated[0, -1].item()) in eos
        self.last_stats.append({'audio_seconds': round(len(audio)/16000, 3),
                               'generation_seconds': round(time.monotonic()-started, 3),
                               'generated_tokens': int(generated.shape[1]), 'ended': ended,
                               'budget_expired': time.monotonic() >= deadline})
        if not ended and time.monotonic() >= deadline:
            raise ASRLengthLimitError('此段生成超過 8 秒預算，略過以避免積壓。')
        if generated.shape[1] >= 256 and not ended:
            raise ASRLengthLimitError('辨識達 256-token 上限且未正常結束。')
        return self.processor.decode(generated, return_format='transcription_only')[0].strip()
