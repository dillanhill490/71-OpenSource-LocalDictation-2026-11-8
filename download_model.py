#!/usr/bin/env python3
"""Download the SenseVoice Small int8 ONNX model for sherpa-onnx (~155 MB download,
239 MB on disk). Cross-platform: Windows / macOS / Linux."""
import os
import sys
import tarfile
import urllib.request

URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
       "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2")
MIRROR_NOTE = ("If GitHub is slow in your region, download the same archive manually and "
               "extract model.int8.onnx + tokens.txt into models/sense-voice-int8/, "
               "renaming model.int8.onnx to sv-int8.onnx.")
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "sense-voice-int8")


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


def main() -> int:
    onnx = os.path.join(DEST, "sv-int8.onnx")
    tokens = os.path.join(DEST, "tokens.txt")
    if os.path.exists(onnx) and os.path.exists(tokens):
        print("Model already present:", DEST)
        return 0
    os.makedirs(DEST, exist_ok=True)
    tbz = os.path.join(DEST, "model.tar.bz2")
    print("Downloading SenseVoice Small int8 (~155 MB)...")
    try:
        download(URL, tbz)
    except Exception as e:
        print(f"Download failed: {e}\n{MIRROR_NOTE}")
        return 1
    print("Extracting...")
    with tarfile.open(tbz, "r:bz2") as t:
        for member in t.getmembers():
            name = os.path.basename(member.name)
            if name == "model.int8.onnx":
                member.name = "sv-int8.onnx"
                t.extract(member, DEST)
            elif name == "tokens.txt":
                t.extract(member, DEST)
    os.remove(tbz)
    ok = os.path.exists(onnx) and os.path.exists(tokens)
    print("Model ready at", DEST if ok else "— extraction FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
