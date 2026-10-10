"""Single-model GPU process. stdout is reserved for newline JSON IPC."""
import base64
import json
import os
import sys
from pathlib import Path


def main():
    protocol = sys.stdout
    sys.stdout = sys.stderr

    def send(**message):
        protocol.write(json.dumps(message, ensure_ascii=False) + '\n')
        protocol.flush()

    try:
        from asr_models import MODELS, ASRLengthLimitError
        from gpu_compat import choose_compute_type, check_native_backend
        import numpy as np
        from huggingface_hub.utils import disable_progress_bars
        disable_progress_bars()
        key, cache_dir = sys.argv[1:3]
        spec = MODELS[key]
        synchronize = lambda: None
        if key == 'qwen':
            import torch
            torch.set_num_threads(min(4, os.cpu_count() or 1))
            synchronize = torch.cuda.synchronize
            from qwen_backend import QwenASR
            model = QwenASR()
            recognize = model.transcribe
            backend = ('Transformers FP32（GTX 實驗模式：高顯存／可能較慢）'
                       if model.model.dtype == torch.float32 else 'Transformers FP16')
        else:
            # CUDA 13 / Blackwell use the native PyTorch backend; CT2 requires CUDA 12.
            from importlib.util import find_spec
            # The installed PyTorch supplies DLLs, but CT2 does not need its runtime.
            torch_spec = find_spec('torch')
            if torch_spec is None or torch_spec.origin is None:
                raise RuntimeError('PyTorch CUDA runtime files were not found.')
            torch_dir = Path(torch_spec.origin).parent
            lib = torch_dir / 'lib'
            # CUDA 13 installations use the native Transformers path (RTX 50).
            use_ct2 = (lib / 'cublas64_12.dll').exists() if os.name == 'nt' else False
            if use_ct2:
                os.environ['PATH'] = str(lib) + os.pathsep + os.environ.get('PATH', '')
                dll_handle = os.add_dll_directory(str(lib)) if os.name == 'nt' else None
                import ctranslate2
                compute_type = choose_compute_type(ctranslate2.get_supported_compute_types('cuda', 0))
                from faster_whisper import WhisperModel
                model = WhisperModel(spec['ct2'], device='cuda', compute_type=compute_type,
                                     download_root=cache_dir, cpu_threads=4, num_workers=1)
                backend = 'faster-whisper ' + ('INT8/FP16' if compute_type == 'int8_float16' else 'INT8/FP32')

                def recognize(audio, language):
                    segments, _ = model.transcribe(audio, language=language, task='transcribe',
                        beam_size=5, temperature=0.0, condition_on_previous_text=False,
                        # Kotoba's CT2 config carries large-v3 alignment heads that
                        # exceed its two decoder layers. Do not run word alignment.
                        vad_filter=False, word_timestamps=(key != 'kotoba'), max_new_tokens=256)
                    # Alignment bounds keep padded end-of-window text out of subtitles.
                    duration = len(audio) / 16000
                    if key == 'kotoba':
                        return ''.join(s.text for s in segments if s.start < duration).strip()
                    words = [w.word for s in segments for w in (s.words or [])
                             if w.start < duration and w.end > w.start]
                    return ''.join(words).strip()
            else:
                import torch
                torch.set_num_threads(min(4, os.cpu_count() or 1))
                if not torch.cuda.is_available():
                    raise RuntimeError('PyTorch 沒有偵測到 CUDA。')
                check_native_backend(torch.cuda.get_device_capability(0))
                synchronize = torch.cuda.synchronize
                from transformers import AutoProcessor, WhisperForConditionalGeneration
                processor = AutoProcessor.from_pretrained(spec['hf'])
                model = WhisperForConditionalGeneration.from_pretrained(
                    spec['hf'], dtype=torch.float16, attn_implementation='sdpa').to('cuda').eval()
                backend = 'Transformers FP16'

                def recognize(audio, language):
                    inputs = processor(audio, sampling_rate=16000, return_tensors='pt',
                                       return_attention_mask=True).to('cuda', torch.float16)
                    with torch.inference_mode():
                        result = model.generate(**inputs, language=language, task='transcribe',
                            num_beams=5, do_sample=False, max_new_tokens=256, return_timestamps=False)
                    if int(result[0, -1]) != model.generation_config.eos_token_id:
                        raise ASRLengthLimitError('Whisper 輸出達上限，略過此段。')
                    return processor.batch_decode(result, skip_special_tokens=True)[0].strip()
        send(event='ready', backend=backend)
        for line in sys.stdin:
            request = json.loads(line)
            try:
                language = request['language']
                if language not in spec['languages']:
                    raise ValueError('此模型只支援日文，請先切換 JP。')
                audio = np.frombuffer(base64.b64decode(request['audio']), dtype='<f4').copy()
                if not len(audio) or np.max(np.abs(audio)) < .0001:
                    text = ''
                else:
                    text = recognize(audio, language)
                    synchronize()
                send(event='result', text=text, stats=getattr(model, 'last_stats', []) if key == 'qwen' and len(audio) and np.max(np.abs(audio)) >= .0001 else [])
            except ASRLengthLimitError as exc:
                send(event='length_error', error=str(exc), stats=getattr(model, 'last_stats', []))
    except Exception as exc:
        send(event='error', error=f'{type(exc).__name__}: {exc}')


if __name__ == '__main__':
    main()
