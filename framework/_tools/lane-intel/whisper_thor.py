#!/usr/bin/env python3
"""whisper_thor.py: runs ON THOR inside ~/.local/lane-intel/venv (fed over ssh stdin by theo_watch.py).
Transcribes one audio file with faster-whisper on the main GPU and prints "[mm:ss] text" lines.
Usage: whisper_thor.py <audio> [model]   |   whisper_thor.py --self-test   |   whisper_thor.py --dry-run (GPU/Ollama state only)"""
import json, os, subprocess, sys, time, urllib.request

OLLAMA = "http://127.0.0.1:11434"


def stamp(sec):
    return f"[{int(sec) // 60:02d}:{int(sec) % 60:02d}]"


def gpu_free():
    """MiB free per GPU ([] when nvidia-smi fails)."""
    try:
        return [int(x) for x in subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                                               capture_output=True, text=True, timeout=10).stdout.split()]
    except Exception:
        return []


def free_ollama(base=OLLAMA, dry=False):
    """VRAM short: one keep_alive:0 per loaded Ollama model (Ollama unloads it once an in-flight request ends).
    Returns the model names it asked to unload (or would, with dry); never raises."""
    try:
        with urllib.request.urlopen(base + "/api/ps", timeout=5) as f:
            names = [m["name"] for m in json.load(f).get("models", [])]
        for n in [] if dry else names:
            urllib.request.urlopen(urllib.request.Request(base + "/api/generate", json.dumps({"model": n, "keep_alive": 0}).encode(),
                                                          {"Content-Type": "application/json"}), timeout=30).read()
        return names
    except Exception as e:
        print(f"ollama unload skipped: {e}", file=sys.stderr); return []


def load_gpu(WhisperModel, name):
    """The freest GPU first, then the other; None when every GPU is out of memory."""
    free = gpu_free()
    for idx in sorted(range(len(free)), key=lambda i: -free[i]) or [0]:
        for ct in ("int8_float16", "int8"):   # an older Pascal card has no efficient int8_float16
            try:
                return WhisperModel(name, device="cuda", device_index=idx, compute_type=ct)
            except (RuntimeError, ValueError) as e:
                msg = str(e).lower()
                if "out of memory" in msg:
                    print(f"gpu {idx} full, trying next", file=sys.stderr); break
                if "compute type" in msg or "not support" in msg:
                    continue
                raise
    return None


def self_test():
    import contextlib, io
    from unittest import mock
    assert stamp(0) == "[00:00]" and stamp(754.9) == "[12:34]" and stamp(4512) == "[75:12]"
    seen = []

    def reply(request, **kw):
        if isinstance(request, str):
            assert request == OLLAMA + "/api/ps"
            return io.BytesIO(b'{"models": [{"name": "qwen3.8-text:27b"}]}')
        seen.append((request.full_url, json.loads(request.data)))
        return io.BytesIO(b'{"done_reason":"unload"}')

    with mock.patch.object(urllib.request, "urlopen", side_effect=reply):
        assert free_ollama(dry=True) == ["qwen3.8-text:27b"] and seen == []
        assert free_ollama() == ["qwen3.8-text:27b"]
    assert seen == [(OLLAMA + "/api/generate", {"model": "qwen3.8-text:27b", "keep_alive": 0})], seen
    with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("offline")), contextlib.redirect_stderr(io.StringIO()):
        assert free_ollama() == []
    print("self-test passed"); return 0


def main(argv):
    if argv == ["--self-test"]:
        return self_test()
    if argv == ["--dry-run"]:
        print(f"gpu free MiB: {gpu_free()}; if every GPU is full, would unload: {free_ollama(dry=True)}"); return 0
    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")  # index 0 = the main GPU (an older Pascal card has no fast fp16)
    from faster_whisper import WhisperModel
    t = time.time()
    name = argv[1] if len(argv) > 1 else "distil-large-v3"
    # an Ollama model can fill every GPU (CUDA OOM): freest GPU, other GPU, unload Ollama + retry, CPU
    model = load_gpu(WhisperModel, name)
    if model is None:
        before = sum(gpu_free())
        if free_ollama():
            print("gpus full: asked Ollama to unload, retrying", file=sys.stderr)
            for _ in range(15):  # ponytail: quick when the model is idle; an in-flight request defers it, then CPU
                time.sleep(2)
                if sum(gpu_free()) > before + 1024:
                    break
            model = load_gpu(WhisperModel, name)
    if model is None:  # ponytail: CPU small.en is ~5x slower but never fails; fine for a daily job
        model = WhisperModel("small.en", device="cpu", compute_type="int8")
        print("whisper on CPU (small.en)", file=sys.stderr)
    segs, info = model.transcribe(argv[0], language="en", vad_filter=True)
    n = 0
    for s in segs:
        print(f"{stamp(s.start)} {s.text.strip()}", flush=True); n += 1
    print(f"whisper: {n} segments, {info.duration:.0f}s audio in {time.time() - t:.0f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
