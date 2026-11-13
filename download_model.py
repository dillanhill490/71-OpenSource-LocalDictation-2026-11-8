#!/usr/bin/env python3
"""Download the SenseVoice Small int8 ONNX model for sherpa-onnx (~155 MB download,
239 MB on disk). Cross-platform: Windows / macOS / Linux."""
import os
import sys
import tarfile
import urllib.request

URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
       "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2")
QWEN3_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2")
MIRROR_NOTE = ("If GitHub is slow in your region, download the same archive manually and "
               "extract model.int8.onnx + tokens.txt into models/sense-voice-int8/, "
               "renaming model.int8.onnx to sv-int8.onnx.")
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "sense-voice-int8")
QWEN3_DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "models", "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25")


def download(url: str, dst: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "LocalDictation/1.0"})
    with urllib.request.urlopen(req) as r, open(dst, "wb") as f:
        total = int(r.headers.get("content-length", 0))
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done/1e6:6.1f} / {total/1e6:.1f} MB", end="", flush=True)
    print()


def fetch_sensevoice_fallback() -> bool:
    """备用引擎 SenseVoice（主力 Qwen3-ASR 不可用时自动接管）。"""
    onnx = os.path.join(DEST, "sv-int8.onnx")
    tokens = os.path.join(DEST, "tokens.txt")
    if os.path.exists(onnx) and os.path.exists(tokens):
        print("SenseVoice fallback model already present:", DEST)
        return True
    os.makedirs(DEST, exist_ok=True)
    tbz = os.path.join(DEST, "model.tar.bz2")
    print("Downloading SenseVoice Small fallback model (~155 MB)...")
    try:
        download(URL, tbz)
    except Exception as e:
        print(f"Download failed: {e}\n{MIRROR_NOTE}")
        return False
    with tarfile.open(tbz, "r:bz2") as t:
        for member in t.getmembers():
            name = os.path.basename(member.name)
            if name == "model.int8.onnx":
                member.name = "sv-int8.onnx"
                t.extract(member, DEST)
            elif name == "tokens.txt":
                t.extract(member, DEST)
    os.remove(tbz)
    return os.path.exists(onnx) and os.path.exists(tokens)


def fetch_qwen3_primary() -> bool:
    """主力引擎 Qwen3-ASR 0.6B int8（约 800 MB 压缩包 / 900 MB 解压）。"""
    need = ["conv_frontend.onnx", "encoder.int8.onnx", "decoder.int8.onnx"]
    tok = os.path.join(QWEN3_DEST, "tokenizer")
    done = all(os.path.exists(os.path.join(QWEN3_DEST, f)) for f in need) \
        and os.path.isdir(tok)
    if done:
        print("Qwen3-ASR model already present:", QWEN3_DEST)
        return True
    os.makedirs(QWEN3_DEST, exist_ok=True)
    tbz = os.path.join(QWEN3_DEST, "model.tar.bz2")
    print("Downloading Qwen3-ASR 0.6B primary model (~800 MB)...")
    try:
        download(QWEN3_URL, tbz)
    except Exception as e:
        print(f"Download failed: {e}\n{MIRROR_NOTE}")
        return False
    print("Extracting...")
    with tarfile.open(tbz, "r:bz2") as t:
        t.extractall(QWEN3_DEST)
    os.remove(tbz)
    return all(os.path.exists(os.path.join(QWEN3_DEST, f)) for f in need) \
        and os.path.isdir(tok)


def main() -> int:
    ok = fetch_qwen3_primary()
    ok2 = fetch_sensevoice_fallback()
    if ok and ok2:
        print("全部模型就绪（主力 Qwen3-ASR + 备用 SenseVoice）")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
