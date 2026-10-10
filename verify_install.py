import argparse
import tkinter as tk
import torch

from asr_process import ASRProcess
from asr_models import MODELS
from app_preferences import load_preferences, save_preferences, choice, recommended_model
from pathlib import Path
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument('--expected-torch')
parser.add_argument('--expected-cuda')
args = parser.parse_args()
if args.expected_torch and torch.__version__ != args.expected_torch:
    raise RuntimeError(f'Expected torch {args.expected_torch}, got {torch.__version__}. Run install.bat again.')
if args.expected_cuda and torch.version.cuda != args.expected_cuda:
    raise RuntimeError(f'Expected CUDA {args.expected_cuda}, got {torch.version.cuda}.')

print("PyTorch:", torch.__version__)
print("CUDA runtime:", torch.version.cuda)

if not torch.cuda.is_available():
    raise RuntimeError("PyTorch cannot access the NVIDIA GPU.")

print("GPU:", torch.cuda.get_device_name(0))

print("GPU capability:", torch.cuda.get_device_capability(0))
print("Compiled architectures:", torch.cuda.get_arch_list())

capability = torch.cuda.get_device_capability(0)
if capability < (7, 0) and torch.version.cuda != '12.6':
    raise RuntimeError('GTX 10 series requires CUDA 12.6. Run install.bat again.')

# Verify actual CUDA execution, not just device detection.
x = torch.randn(256, 256, device="cuda", dtype=torch.float32 if capability < (7, 0) else torch.float16)
y = x @ x

torch.cuda.synchronize()

if not torch.isfinite(y).all().item():
    raise RuntimeError("GPU calculation failed.")

# Test the usual RTX SDPA path. The chosen ASR backend is exercised below,
# including FP32 Qwen if a Pascal user explicitly saved that model.
if capability >= (7, 0):
    q = torch.randn(1, 4, 64, 64, device='cuda', dtype=torch.float16)
    attention = torch.nn.functional.scaled_dot_product_attention(q, q, q)
    torch.cuda.synchronize()
    if not torch.isfinite(attention).all().item():
        raise RuntimeError('GPU attention calculation failed.')
    del q, attention
del x, y
torch.cuda.empty_cache()

# Verify that the downloaded Python includes working Tk support.
root = tk.Tk()
root.withdraw()
root.update()
root.destroy()

# Download the model if necessary and check GPU loading.
settings = load_preferences('runtime')
key = choice(settings, 'model', MODELS, '') or recommended_model(
    torch.cuda.get_device_properties(0).total_memory // (1024*1024), capability)
print(f"Loading {key}. First download may take a while.")
model = ASRProcess(key, Path(__file__).resolve().parent / 'models')
try:
    # Nonzero input exercises inference instead of taking the silence shortcut.
    sample = (np.sin(np.arange(16000, dtype=np.float32)*.04)*.001).astype(np.float32)
    print('ASR backend:', model.backend)
    model.transcribe(sample, 'ja')
finally:
    model.close()
settings['model'] = key
save_preferences('runtime', settings)

print("GPU, GUI and model loading checks passed.")
