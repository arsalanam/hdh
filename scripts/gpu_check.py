"""GPU readiness check for the media I/O service (design: docs/design/media-io-service.md).

The STT/TTS/OCR stack (NeMo ASR + Sortformer/TitaNet diarization, Kokoro TTS,
optional local VLM for OCR) all sit on PyTorch + CUDA. This proves the card
actually runs CUDA kernels for THIS architecture — the real gotcha on a new GPU
(e.g. Blackwell sm_120 needs cu128+ wheels) — not merely that torch imports.

Run it inside an environment that has a CUDA build of torch (not the base hdh
env, which has none):

    uv venv gpu-env --python 3.12
    uv pip install --python gpu-env torch --index-url https://download.pytorch.org/whl/cu128
    gpu-env/Scripts/python scripts/gpu_check.py        # Windows
    gpu-env/bin/python scripts/gpu_check.py            # Linux

Exit code 0 = ready; non-zero = a problem a human should see before building.
"""

from __future__ import annotations

import sys


def _reconfigure_stdout() -> None:
    """Windows consoles default to cp1252 and choke on any non-ASCII; make
    output UTF-8 so a check script never dies on a tick mark."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _row(key: str, value: object) -> None:
    print(f"  {key:<28} {value}")


def main() -> int:
    _reconfigure_stdout()
    try:
        import torch
    except ImportError:
        print("torch is not installed in this environment.")
        print("This check needs a CUDA build of torch — see the module docstring.")
        return 2

    print("=== PyTorch / CUDA build ===")
    _row("torch", torch.__version__)
    _row("compiled CUDA", torch.version.cuda)
    _row("cuDNN", torch.backends.cudnn.version())
    _row("cuda.is_available()", torch.cuda.is_available())
    if not torch.cuda.is_available():
        print("\nFAIL: CUDA is not available to PyTorch.")
        print("On a new GPU this usually means the wheel lacks kernels for its")
        print("architecture — install a matching CUDA build (e.g. cu128 for Blackwell).")
        return 1

    dev = torch.device("cuda:0")
    props = torch.cuda.get_device_properties(0)
    arch = f"sm_{props.major}{props.minor}"
    print("\n=== device ===")
    _row("name", props.name)
    _row("compute capability", arch)
    _row("total VRAM", f"{props.total_memory / 1024**3:.1f} GB")
    _row("multiprocessors", props.multi_processor_count)

    print("\n=== architecture support ===")
    arch_list = torch.cuda.get_arch_list()
    _row("torch arch list", arch_list)
    supported = any(arch.replace("sm_", "") in a for a in arch_list)
    _row(f"kernels for {arch}", "yes" if supported else "NO — will fall back / fail")
    if not supported:
        print("\nFAIL: this torch build has no kernels for the installed GPU.")
        return 1

    print("\n=== correctness ===")
    a = torch.randn(1024, 1024, device=dev)
    b = torch.randn(1024, 1024, device=dev)
    ok = torch.allclose((a @ b).cpu(), a.cpu() @ b.cpu(), atol=1e-2)
    _row("matmul GPU vs CPU", "ok" if ok else "MISMATCH")
    if not ok:
        print("\nFAIL: GPU matmul disagrees with CPU.")
        return 1

    print("\n=== throughput (warmed, the precisions the models use) ===")
    _benchmark(torch, dev)

    print("\n=== memory ===")
    free, total = torch.cuda.mem_get_info()
    _row("free / total", f"{free / 1024**3:.1f} / {total / 1024**3:.1f} GB")

    print("\nPASS: GPU is CUDA-ready for the media-service stack.")
    return 0


def _benchmark(torch, dev, n: int = 8192) -> None:
    def run(dtype, label, tf32: bool) -> None:
        torch.backends.cuda.matmul.allow_tf32 = tf32
        torch.backends.cudnn.allow_tf32 = tf32
        x = torch.randn(n, n, device=dev, dtype=dtype)
        y = torch.randn(n, n, device=dev, dtype=dtype)
        for _ in range(5):  # warm up so cuBLAS algorithm selection isn't timed
            _ = x @ y
        torch.cuda.synchronize()
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        iters = 30
        start.record()
        for _ in range(iters):
            _ = x @ y
        end.record()
        torch.cuda.synchronize()
        dt = start.elapsed_time(end) / iters / 1000.0
        _row(label, f"{dt * 1000:6.1f} ms   ~{(2 * n**3) / dt / 1e12:6.1f} TFLOP/s")

    run(torch.float32, "fp32 (strict)", tf32=False)
    run(torch.float32, "tf32", tf32=True)
    run(torch.float16, "fp16", tf32=True)
    run(torch.bfloat16, "bf16", tf32=True)


if __name__ == "__main__":
    raise SystemExit(main())
