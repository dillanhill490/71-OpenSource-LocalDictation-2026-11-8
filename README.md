# LocalDictation

> 🇨🇳 [中文说明](README_zh-CN.md)

**The free & open-source Wispr Flow alternative** — fully offline push-to-talk dictation for Windows. Hold a hotkey, speak,
release — the text is typed into whatever your cursor is in. No cloud, no lag, no
subscription.

![overlay](assets/pill_hero.png)

## Why I built this

First, a tribute: I'm a paying Pro user of [Wispr Flow](https://wisprflow.ai), and
it's a wonderful product — it made hold-to-talk dictation mainstream, and this
project openly borrows its interaction design. All credit to the team.

That said, I wanted three things a cloud tool by design cannot give me:

1. **Latency** — cloud round-trips stall whenever the network does.
2. **Privacy** — every dictated word is uploaded to a third-party cloud.
3. **Subscription** — Pro runs $15/month ($12 billed annually), indefinitely.

So I built LocalDictation as a local-first homage: same hold-to-talk flow,
everything on your machine.

| | Cloud dictation (Wispr Flow Pro) | **LocalDictation** |
|---|---|---|
| Latency | network-dependent | **~0.1 s decode, pure CPU** |
| Voice data | uploaded to cloud | **never leaves your machine** |
| Cost | $15/mo ($12 annual) | **$0** |
| Disk | — | **~320 MB** (model + runtime) |

## Features

- **Push-to-talk** on `Ctrl+Space`: hold to record, release to type (order-agnostic
  physical key polling — no missed presses, IME-safe)
- **Wispr-style waveform pill**: tiny rounded overlay above the taskbar of the
  monitor you're working on; follows you across displays; never steals focus
- **Chinese-first accuracy**: primary engine Qwen3-ASR 0.6B — picked from a
  **six-engine benchmark** at **0.65% CER overall, 0% errors on Chinese-English
  mixed speech** (36 test utterances); SenseVoice ships as automatic fallback
- **Editable hotword table** (`replacements.json`): fix your own mis-recognized
  jargon ("get up" → `GitHub`), hot-reloaded after every utterance
- Tray icon with pause/exit; optional start-up shortcut
- English/Japanese/Korean/Cantonese also recognized (auto language detection)

## Install (Windows 10/11 — macOS version in active development)

```bat
git clone https://github.com/dillanhill490/71-OpenSource-LocalDictation-2026-11-8.git
cd 71-OpenSource-LocalDictation-2026-11-8
pip install -r requirements.txt
python download_model.py      :: ~950 MB downloads (primary + fallback), one time
python dictation.py
```

For start-up: create a shortcut to `pythonw.exe dictation.py` in your Startup
folder (`Win+R` → `shell:startup`).

## Configure

- **Hotwords**: edit `replacements.json` next to the script — `"mistranscribed": "correct"`.
  Latin keys match whole words case-insensitively; CJK keys replace literally.
  Reloaded automatically after every utterance.
- **Tray menu**: pause (releases `Ctrl+Space`) or quit.

## How it works

- ASR: primary [Qwen3-ASR 0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B) int8 ONNX
  (LLM-decoder architecture: fresh 2026 vocabulary, native punctuation & hotwords,
  ~0.6 s/utt on laptop CPU) via [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx);
  [SenseVoice Small](https://github.com/FunAudioLLM/SenseVoice) int8 as automatic
  fallback (~100 ms/utt)
- Hotkey: 15 ms polling of physical key state (`GetAsyncKeyState`) — press order,
  dropped events and IME interference can't miss a trigger
- Typing: `SendInput` unicode injection into the current focus
- Overlay: a Win32 layered window (`UpdateLayeredWindow`, per-pixel alpha),
  rebuilt fresh for every recording session — no stale-state failures across
  sleep/resume or display changes

## Roadmap

- [ ] **macOS support — planned and on the roadmap.** The ASR core (SenseVoice +
      sherpa-onnx) is already cross-platform; typing injection, hotkey capture and
      the overlay need native rewrites. This project is intended to be
      cross-platform, not Windows-only.
- [ ] Clipboard-paste fallback for elevated/sandboxed windows
- [ ] Curated Chinese developer hotword pack

## Credits & licenses

- **Qwen3-ASR 0.6B** (primary ASR) © Alibaba Qwen team — **Apache-2.0**. Not
  redistributed here; `download_model.py` fetches the official build.
- **SenseVoice Small** (fallback ASR) © Alibaba FunAudioLLM — model weights under the
  [FunASR Model Open Source License](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE);
  source code MIT. Not redistributed here; `download_model.py` fetches the
  official build.
- **sherpa-onnx** — Apache-2.0.
- This project's own code — [MIT](LICENSE). (Model weights keep their own
  licenses above; the MIT grant covers this repository's code only.)

This project is not affiliated with Alibaba, k2-fsa, or Wispr (Flow).

## Benchmarks

36 synthesized utterances (plain Chinese / numbers / Chinese-English mixed /
tech terms / long sentences × 2 voices), 3 runs each, median:

| Category | CER |
|---|---|
| Plain Chinese | 0% |
| Numbers & dates | 0% |
| Chinese-English mixed | 0% |
| Long sentences | 0% |
| Tech terms | 5.88% |
| **Overall** | **0.65%** |

(The few errors come from TTS mispronouncing English words in the test set —
real speech fares better; see `replacements.json` for the ergonomic fix.)
