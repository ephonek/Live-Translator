import tkinter as tk
import torch

from qwen_backend import QwenASR

print("PyTorch:", torch.__version__)
print("CUDA runtime:", torch.version.cuda)

if not torch.cuda.is_available():
    raise RuntimeError("PyTorch cannot access the NVIDIA GPU.")

print("GPU:", torch.cuda.get_device_name(0))

# Verify actual CUDA execution, not just device detection.
x = torch.randn(256, 256, device="cuda", dtype=torch.float16)
y = x @ x

torch.cuda.synchronize()

if not torch.isfinite(y).all().item():
    raise RuntimeError("GPU calculation failed.")

del x, y
torch.cuda.empty_cache()

# Verify that the downloaded Python includes working Tk support.
root = tk.Tk()
root.withdraw()
root.update()
root.destroy()

# Download the model if necessary and check GPU loading.
print("Loading Qwen3-ASR. First download may take a while.")
model = QwenASR()

print("GPU, GUI and model loading checks passed.")