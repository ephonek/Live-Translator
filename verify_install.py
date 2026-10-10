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

# Verify actual CUDA execution, not just device detection.
x = torch.randn(256, 256, device="cuda", dtype=torch.float16)
y = x @ x

torch.cuda.synchronize()

if not torch.isfinite(y).all().item():
    raise RuntimeError("GPU calculation failed.")

# Qwen uses SDPA; test this path as well as matrix multiplication.
q = torch.randn(1, 4, 64, 64, device='cuda', dtype=torch.float16)
attention = torch.nn.functional.scaled_dot_product_attention(q, q, q)
torch.cuda.synchronize()
if not torch.isfinite(attention).all().item():
    raise RuntimeError('GPU attention calculation failed.')
del x, y, q, attention
torch.cuda.empty_cache()

# Verify that the downloaded Python includes working Tk support.
root = tk.Tk()
root.withdraw()
root.update()
root.destroy()

# Download the model if necessary and check GPU loading.
settings = load_preferences('runtime')
key = choice(settings, 'model', MODELS, '') or recommended_model(torch.cuda.get_device_properties(0).total_memory // (1024*1024))
print(f"Loading {key}. First download may take a while.")
model = ASRProcess(key, Path(__file__).resolve().parent / 'models')
try:
    # Nonzero input exercises inference instead of taking the silence shortcut.
    sample = (np.sin(np.arange(16000, dtype=np.float32)*.04)*.001).astype(np.float32)
    model.transcribe(sample, 'ja')
finally:
    model.close()
settings['model'] = key
save_preferences('runtime', settings)

print("GPU, GUI and model loading checks passed.")