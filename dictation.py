# -*- coding: utf-8 -*-
"""本地听写托盘工具（v2.5）
按住 Ctrl+Space 说话 → 松开 → 文字打进当前光标所在输入框。识别全程离线本机。
- 触发：15ms 轮询物理按键状态（GetAsyncKeyState），按下顺序/事件丢失/IME 吞键均不影响
- 反馈：Wispr 风格小胶囊声波浮窗（Win32 分层窗口，逐像素真透明，跟随前台所在屏，
  贴该屏任务栏上方）；每次录音会话用全新窗口（内联毫秒级重建），杜绝休眠/切屏后的陈旧状态
- 托盘：蓝=待命，红=录音中，灰=已暂停；右键可暂停/退出
自检模式：python main.py --selftest（3 秒后向焦点窗口打字）
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque

import numpy as np
import sherpa_onnx
import sounddevice as sd
from PIL import Image, ImageDraw
import pystray
from pystray import Menu, MenuItem

# ---------------- 自定位路径 ----------------
ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(ROOT, "models", "sense-voice-int8", "sv-int8.onnx")
TOKENS = os.path.join(ROOT, "models", "sense-voice-int8", "tokens.txt")
LOG_PATH = os.path.join(ROOT, "dictation.log")

SR = 16000
RMS_GATE = 0.002
TYPE_INTERVAL_S = 0.003
MIN_DUR_S = 0.25
REPL_PATH = os.path.join(ROOT, "replacements.json")   # 专有名词替换表（用户可编辑，每次听写自动重载）
LLM_PATH = os.path.join(ROOT, "models", "llm", "qwen2.5-3b-instruct-q4_k_m.gguf")  # 本地润色模型
QWEN3_PATH = os.path.join(ROOT, "models", "llm", "qwen3-4b-instruct-2507-q4_k_m.gguf")  # 升级候选
LLAMA_SERVER_EXE = os.path.join(ROOT, "llama-cpp", "llama-server.exe")
LLAMA_PORT = 8750
LLAMA_BASE = f"http://127.0.0.1:{LLAMA_PORT}"
SETTINGS_PATH = os.path.join(ROOT, "settings.json")   # 记住可用人声的麦克风设备
WHISPER_DIR = os.path.join(ROOT, "models", "whisper-turbo-ct2")   # 主力 ASR（GPU）
DEFAULT_REPL = {
    "get up": "GitHub",
    "get hub": "GitHub",
    "github": "GitHub",
    "mac": "Mac",
    "macbook": "MacBook",
    "vs code": "VSCode",
    "vscode": "VSCode",
    "api": "API",
    "ui": "UI",
    "ux": "UX",
    "cpu": "CPU",
    "gpu": "GPU",
    "llm": "LLM",
    "ai": "AI",
    "chat gpt": "ChatGPT",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "cursor": "Cursor",
    "grok": "Grok",
    "open ai": "OpenAI",
    "openai": "OpenAI",
    "python": "Python",
    "docker": "Docker",
    "linux": "Linux",
    "wispr": "Wispr",
    "wispr flow": "Wispr Flow",
    "local dictation": "LocalDictation",
    "qq": "QQ",
}


def load_replacements():
    """读替换表；文件缺失则写入默认表。键→值，拉丁词按整词忽略大小写匹配，中文按原文替换。"""
    try:
        with open(REPL_PATH, encoding="utf-8") as f:
            m = json.load(f)
    except Exception:
        try:
            with open(REPL_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_REPL, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        m = dict(DEFAULT_REPL)
    return sorted(m.items(), key=lambda kv: -len(kv[0]))   # 长键优先，防止短键截胡


def apply_replacements(text):
    for k, v in load_replacements():
        if re.fullmatch(r"[A-Za-z0-9_\- ]+", k):   # 拉丁词：整词匹配，大小写不敏感
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])",
                          v, text, flags=re.IGNORECASE)
        elif k in text:                            # 中文等：原文替换
            text = text.replace(k, v)
    return text


# ---------------- 本地小模型润色层（Phase 2，llama-server GPU 推理 + HTTP 调用） ----------------
_llm_proc = {"popen": None}
POLISH_SYS = ("你是听写文本清理器。严格遵守：逐字保留用户说出的每一个实词，禁止改写句式、"
              "替换词语、增删任何内容、改变语气人称。只允许三类最小修改："
              "①删除纯口头语（嗯、呃、那个、就是说）②为断句补标点 ③按对照表修正专有名词写法。"
              "输出必须是基于原文的最小修改版本，不要任何解释。")
POLISH_FEWSHOT = [
    ("润色层我觉得现在做的非常不好", "润色层我觉得现在做得非常不好。"),
    ("那个今天下午三点开会 呃 别迟到", "今天下午三点开会，别迟到。"),
    ("帮我打开get up提交一下代码", "帮我打开 GitHub 提交一下代码。"),
]


def _port_alive():
    import urllib.request
    try:
        with urllib.request.urlopen(LLAMA_BASE + "/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def get_llm():
    """确保 llama-server（GPU）已启动且健康；失败只降级一次并留日志，之后直接跳过润色。"""
    if _llm_proc["popen"] is not None:
        if _port_alive():
            return True
        log("润色服务失联，尝试重启……")
        try:
            _llm_proc["popen"].kill()
        except Exception:
            pass
        _llm_proc["popen"] = None
    if not os.path.exists(LLAMA_SERVER_EXE):
        log(f"润色层：未找到 {LLAMA_SERVER_EXE}，润色关闭")
        _llm_proc["popen"] = -1   # 标记永久不可用
        return None
    if not os.path.exists(LLM_PATH):
        log(f"润色层：未找到模型 {LLM_PATH}，润色关闭")
        _llm_proc["popen"] = -1
        return None
    try:
        flags = 0x08000000   # CREATE_NO_WINDOW
        p = subprocess.Popen(
            [LLAMA_SERVER_EXE, "-m", LLM_PATH, "-ngl", "99", "-c", "2048",
             "--port", str(LLAMA_PORT), "--threads", "8"],
            creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):           # 最长等 30 秒加载模型
            time.sleep(0.5)
            if _port_alive():
                _llm_proc["popen"] = p
                log(f"润色服务已启动（GPU，PID={p.pid}）")
                return True
            if p.poll() is not None:
                break
        log("润色服务启动超时/退出，润色关闭")
        _llm_proc["popen"] = -1
        return None
    except Exception as e:
        log(f"润色服务启动失败，已停用：{type(e).__name__}: {e}")
        _llm_proc["popen"] = -1
        return None


def cer_norm(a, b):
    """归一化编辑距离（去标点/空白/大小写后），作"是否面目全非"的机械判据。"""
    def _n(s):
        s = re.sub(r"[，。？！、；：,.?!;:\s'\"（）]+", "", s)
        return s.lower()
    a, b = _n(a), _n(b)
    if not a:
        return 0.0 if b else 1.0
    dp = list(range(len(b) + 1))
    for i, ac in enumerate(a, 1):
        prev, dp[0] = dp[0], i
        for j, bc in enumerate(b, 1):
            cur = min(dp[j] + 1, dp[j-1] + 1, prev + (ac != bc))
            prev, dp[j] = dp[j], cur
    return dp[-1] / max(len(a), 1)


def polish_text(text):
    if not state.get("polish", False) or len(text) < 2:
        return text
    llm = get_llm()
    if not llm:
        return text
    try:
        import urllib.request
        vocab = "；".join(f"{k}→{v}" for k, v in load_replacements()[:40])
        prompt = (POLISH_SYS + "\n专有名词标准写法（发现原文包含以下写法或其同音误识时，必须改成右侧写法）："
                  + vocab + "\n\n")
        for a, b in POLISH_FEWSHOT:
            prompt += f"原文：{a}\n改写：{b}\n\n"
        prompt += f"原文：{text}\n改写："
        body = json.dumps({"prompt": prompt,
                           "n_predict": min(160, len(text) * 3 + 32),
                           "temperature": 0.1,
                           "stop": ["\n原文：", "\n\n", "原文："]}).encode("utf-8")
        req = urllib.request.Request(LLAMA_BASE + "/completion", data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            fixed = json.loads(r.read().decode("utf-8"))["content"].strip().strip('"')
        ms = (time.time() - t0) * 1000
        # 守护：空/异常长/疑似回答原文 → 回退不改写
        if not fixed or len(fixed) > len(text) * 3 + 20:
            log(f"润色输出异常，回退原文（{ms:.0f}ms）：{fixed!r}")
            return text
        # 意思不变守卫：润色结果与原文差异过大（改写过度/删词）→ 判定面目全非，回退原文
        _d = cer_norm(text, fixed)
        if _d > 0.35:
            log(f"润色被守卫拦截（与原文差异 {_d:.0%} > 35%），回退原文（{ms:.0f}ms）：{fixed!r}")
            return text
        log(f"润色 {ms:.0f}ms：{text!r} → {fixed!r}")
        return fixed
    except Exception as e:
        log(f"润色出错，回退原文：{type(e).__name__}: {e}")
        return text

state = {"enabled": True, "recording": False, "rec_start": 0.0, "rec_gen": 0, "polish": True}
frames = []
levels = deque(maxlen=75)   # 20ms/块 → 1.5s 波形历史
icon_ref = {}
stream_health = {"last_cb": time.time(), "stream": None}   # 录音流心跳（僵死自动重开）


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


# ---------------- 打字注入 ----------------
SendInput = ctypes.windll.user32.SendInput
PUL = ctypes.POINTER(ctypes.c_ulong)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 8)]


KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004


def type_text(text):
    for ch in text:
        code = 13 if ch == "\n" else ord(ch)
        for up in (False, True):
            ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, None)
            SendInput(1, ctypes.byref(INPUT(1, ki)), ctypes.sizeof(INPUT))
        time.sleep(TYPE_INTERVAL_S)


# ---------------- 托盘 ----------------
def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=color)
    return img


ICON_IDLE = make_icon((30, 144, 255, 255))
ICON_REC = make_icon((235, 64, 52, 255))
ICON_OFF = make_icon((130, 130, 130, 255))


def notify(msg):
    ic = icon_ref.get("icon")
    if ic:
        try:
            ic.notify(msg, "本地听写")
        except Exception:
            pass


# ---------------- 热键轮询（按序无关、不丢事件） ----------------
GetAsyncKeyState = ctypes.windll.user32.GetAsyncKeyState
VK_CTRL, VK_SPACE = 0x11, 0x20


def hotkey_poll_thread(asr):
    """Ctrl+Space 全局占用（RegisterHotKey）+ 物理轮询判定按住/松开。
    注册后该组合键被系统截住不再传给输入法——根治"说完话中英输入法被切换"
    （Wispr 同款行为）。暂停听写时自动注销，把组合键还给系统。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    import ctypes.wintypes as wt
    WM_HOTKEY, MOD_CONTROL, ID = 0x0312, 0x0002, 1
    registered = False
    was_down = False
    msg = wt.MSG()
    while True:
        try:
            want = state["enabled"]
            if want and not registered:
                registered = bool(user32.RegisterHotKey(None, ID, MOD_CONTROL, VK_SPACE))
                if registered:
                    log("已注册并接管 Ctrl+Space（该组合键不再触达输入法，中英切换不会被误触）")
                else:
                    log(f"注册 Ctrl+Space 失败（GetLastError={kernel32.GetLastError()}），"
                        f"可能被其他程序占用：退回纯轮询模式，输入法切换不受影响")
            elif not want and registered:
                user32.UnregisterHotKey(None, ID)
                registered = False
                log("已注销 Ctrl+Space 占用（暂停听写：组合键还给系统）")
            if registered:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    pass   # 取走 WM_HOTKEY 即可（按下时序判定仍由下面的物理轮询负责）
            ctrl = bool(GetAsyncKeyState(VK_CTRL) & 0x8000)
            space = bool(GetAsyncKeyState(VK_SPACE) & 0x8000)
            down = ctrl and space and state["enabled"]
            if down and not was_down and not state["recording"]:
                start_rec()
            elif was_down and not down and state["recording"]:
                stop_rec(asr)
            was_down = down
        except Exception as e:
            log(f"热键轮询异常：{type(e).__name__}: {e}")
        time.sleep(0.015)


# ---------------- 识别与上屏 ----------------
def finish_recording(asr):
    audio = np.concatenate(frames).reshape(-1) if frames else np.zeros(0, dtype="float32")
    ic = icon_ref.get("icon")
    if ic:
        ic.icon = ICON_IDLE
    dur = len(audio) / SR
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if dur < MIN_DUR_S:
        log(f"录音丢弃：dur={dur:.2f}s frames={len(frames)}")
        notify("按压太短（<0.25s），未录音")
        return
    if rms < RMS_GATE:
        if dur >= 1.0 and not state.get("scanning"):
            state["scanning"] = True
            log(f"按住 {dur:.1f}s 全程静音（RMS={rms:.4f}）→ 启动麦克风自动检测")
            notify("麦克风没有听到声音：请对着电脑持续说话约 10 秒，正在自动检测……")
            try:
                auto_pick_and_switch()
            finally:
                state["scanning"] = False
        else:
            notify("未检测到语音（音量过低），未上屏")
        return
    # 成功听写：记住这台真能听到用户的麦克风（跨重启持久）
    s_cur = stream_health.get("stream")
    if s_cur:
        try:
            dev = s_cur.device
            st = load_settings()
            if st.get("device_index") != dev:
                st["device_index"] = dev
                st["device_name"] = sd.query_devices(dev)["name"]
                save_settings(st)
        except Exception:
            pass
    try:
        t0 = time.time()
        if asr["engine"] == "whisper":
            segments, _ = asr["whisper"].transcribe(
                audio, beam_size=1, temperature=0.0, condition_on_previous_text=False)
            text = "".join(s.text for s in segments).strip()
        else:
            s = asr["sensevoice"].create_stream()
            s.accept_waveform(SR, audio)
            asr["sensevoice"].decode_stream(s)
            text = s.result.text.strip()
        ms = (time.time() - t0) * 1000
        if not text:
            notify("识别结果为空，未上屏")
            return
        fixed = apply_replacements(text)
        fixed = polish_text(fixed)
        type_text(fixed)
        log(f"{dur:.1f}s 音频 解码{ms:.0f}ms：{text}" + (f"（上屏：{fixed}）" if fixed != text else ""))
    except Exception as e:
        notify(f"出错了：{type(e).__name__}：{e}")


def start_rec():
    state["recording"] = True
    state["rec_gen"] += 1   # 每次录音=新代际：浮窗整窗内联重建（毫秒级），杜绝陈旧状态
    state["rec_start"] = time.time()
    frames.clear()
    levels.clear()
    ic = icon_ref.get("icon")   # 启动头几秒托盘未就绪时按键不报错
    if ic:
        ic.icon = ICON_REC


def stop_rec(asr):
    state["recording"] = False
    threading.Thread(target=finish_recording, args=(asr,), daemon=True).start()


def audio_callback(indata, _n, _t, _st):
    stream_health["last_cb"] = time.time()   # 心跳：回调持续到达=流健康
    if state["recording"]:
        frames.append(indata[:, 0].copy())
        levels.append(float(np.sqrt(np.mean(indata ** 2))))


def reopen_stream(device=None):
    """重开录音流（设备变更/驱动休眠后旧流会僵尸）。优先用记住的可用麦克风；
    记住的设备失效时自动回退系统默认。"""
    if device is None:
        device = load_settings().get("device_index")
    s = stream_health.get("stream")
    if s:
        try:
            s.stop()
            s.close()
        except Exception:
            pass
    kwargs = dict(samplerate=SR, channels=1, dtype="float32",
                  blocksize=320, callback=audio_callback)
    if device is not None:
        kwargs["device"] = device
    try:
        s2 = sd.InputStream(**kwargs)
    except Exception:
        if device is None:
            raise
        kwargs.pop("device")
        s2 = sd.InputStream(**kwargs)
    s2.start()
    stream_health["stream"] = s2
    stream_health["last_cb"] = time.time()
    return s2


def audio_watchdog_thread():
    """每 3 秒检查录音流心跳，>5 秒无回调自动重开（自愈），事件写日志。"""
    while True:
        time.sleep(3)
        try:
            if stream_health.get("stream") is None:
                continue
            idle = time.time() - stream_health["last_cb"]
            if idle > 5:
                log(f"录音流 {idle:.0f}s 无数据（设备可能已变更/驱动休眠），自动重开")
                reopen_stream()
                log("录音流已重开")
        except Exception as e:
            log(f"录音流看门狗异常：{type(e).__name__}: {e}")


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def pick_best_device(total_seconds=12.0):
    """逐台试录输入设备并挑实时响度最高者。检测期间请用户持续说话。"""
    best = None
    tried = set()
    deadline = time.time() + total_seconds
    while time.time() < deadline:
        for i in range(len(sd.query_devices())):
            if i in tried:
                continue
            tried.add(i)
            q = sd.query_devices(i)
            if q["max_input_channels"] < 1:
                continue
            frames = []
            try:
                s = sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                   blocksize=320, device=i,
                                   callback=lambda d, n, t, st: frames.append(d[:, 0].copy()))
                s.start()
                time.sleep(1.0)
                s.stop()
                s.close()
            except Exception:
                continue
            if not frames:
                continue
            audio = np.concatenate(frames)
            rms = float(np.sqrt(np.mean(audio ** 2)))
            if best is None or rms > best[1]:
                best = (i, rms, q["name"])
            if best and best[1] > RMS_GATE:
                return best
    return best


def auto_pick_and_switch():
    """静音自检：逐台试麦，切到真能听到用户说话的设备；全部无声则给出排查指引。"""
    if state["recording"]:
        return
    notify("正在自动检测麦克风，请对着电脑持续说话约 10 秒……")
    best = pick_best_device(12.0)
    if best and best[1] > RMS_GATE:
        idx, rms, name = best
        st = load_settings()
        st["device_index"], st["device_name"] = idx, name
        save_settings(st)
        reopen_stream(device=idx)
        notify(f"已切换到能听到你的麦克风：{name}")
        log(f"麦克风自动切换 → [{idx}] {name} (RMS={rms:.4f})")
    else:
        notify("所有麦克风都没听到声音。请检查：①键盘麦克风静音键 ②系统设置-隐私-麦克风权限 ③新插 USB 麦克风的本机开关/旋钮")
        log(f"麦克风自动检测：无可用人声设备（best={best}）")


# ---------------- Wispr 风格声波浮窗（Win32 分层窗口 + 会话级内联重建） ----------------
class _Rebuild(Exception):
    """窗口代际过期： teardown→setup 内联重建，不跨线程等待。"""


def _overlay_thread():
    """守护：异常重建（短退避，持续自愈，绝不放弃）。"""
    fails = 0
    while True:
        try:
            _run_overlay()
            fails = 0
        except Exception as e:
            fails += 1
            log(f"浮窗重建（第{fails}次）：{type(e).__name__}: {e}")
            time.sleep(0.05)


def _run_overlay():
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    import ctypes.wintypes as wt
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    k = user32.GetDpiForSystem() / 96.0

    W, H = int(64 * k), int(17 * k)
    SS = 2
    WS, HS = W * SS, H * SS
    N_BARS = 19
    BAR_W = max(SS, int(1.6 * k * SS))
    BAR_GAP = BAR_W
    PAD = max(SS, int(HS * 0.16))

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                    ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]

    class BF(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                    ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]

    class BMIHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_int),
                    ("biHeight", ctypes.c_int), ("biPlanes", ctypes.c_ushort),
                    ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint),
                    ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_int),
                    ("biYPelsPerMeter", ctypes.c_int), ("biClrUsed", ctypes.c_uint),
                    ("biClrImportant", ctypes.c_uint)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("rcMonitor", wt.RECT),
                    ("rcWork", wt.RECT), ("dwFlags", ctypes.c_uint)]

    hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
    proc_addr = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value
    base_proc = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_void_p)(proc_addr)
    cls = WNDCLASSW()
    cls.lpszClassName = "LDWaveOverlay"
    cls.hInstance = hinst
    cls.lpfnWndProc = ctypes.cast(base_proc, ctypes.c_void_p)
    user32.RegisterClassW(ctypes.byref(cls))   # 重复注册失败无害，类保持可用

    blend = BF(0, 0, 255, 1)   # AC_SRC_OVER + AC_SRC_ALPHA（必须传，NULL 会报错误 31）
    EX = 0x08000000 | 0x80 | 0x08 | 0x00080000  # NOACTIVATE|TOOLWINDOW|TOPMOST|LAYERED
    WS_POPUP = 0x80000000
    user32.UpdateLayeredWindow.restype = ctypes.c_int

    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vcy = user32.GetSystemMetrics(79)
    if vcy == 0:
        vx, vy, vcy = 0, 0, sh
    park_x, park_y = vx, vy + vcy + 200   # 停靠在所有显示器之外

    bg = Image.new("RGBA", (WS, HS), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    bd.rounded_rectangle([0, 0, WS - 1, HS - 1], radius=HS // 2 - SS,
                         fill=(32, 32, 40, 236), outline=(96, 96, 110, 255), width=SS)

    def target_monitor_workarea():
        fg = user32.GetForegroundWindow()
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        hmon = user32.MonitorFromWindow(fg, 1)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return mi.rcWork.left, mi.rcWork.right, mi.rcWork.bottom
        rc = wt.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rc), 0)
        return rc.left, rc.right, rc.bottom

    def setup():
        """创建全新窗口+渲染面并自检（停靠位置），失败抛异常交守护重建。"""
        hwnd = user32.CreateWindowExW(EX, cls.lpszClassName, "", WS_POPUP,
                                      0, 0, W, H, None, None, hinst, None)
        if not hwnd:
            raise RuntimeError(f"CreateWindowExW 失败 GetLastError={ctypes.GetLastError()}")
        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        bi = BMIHEADER(ctypes.sizeof(BMIHEADER), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        gdi32.SelectObject(hdc_mem, hbmp)
        c = {"hwnd": hwnd, "hdc_screen": hdc_screen, "hdc_mem": hdc_mem,
             "bits": bits, "hbmp": hbmp, "diag_done": False, "shown": False}
        # 创建后保持隐藏（无 WS_VISIBLE）；录音开始才 SW_SHOWNA——待命时桌面上不留任何东西
        _ok, _tries = False, 0
        while not _ok and _tries < 5:
            _tries += 1
            _ok = push(c, park_x, park_y, bg)
            if not _ok:
                time.sleep(0.15)
        if not _ok:
            teardown(c)
            raise RuntimeError(f"自检失败（重试{_tries}次）GetLastError={ctypes.GetLastError()}")
        c["ok"] = True
        return c

    def teardown(c):
        try:
            user32.DestroyWindow(c["hwnd"])
            gdi32.DeleteObject(c["hbmp"])
            gdi32.DeleteDC(c["hdc_mem"])
            user32.ReleaseDC(None, c["hdc_screen"])
        except Exception:
            pass

    def push(c, x, y, img):
        if img.size != (W, H):
            img = img.resize((W, H), Image.LANCZOS)
        a = np.asarray(img, dtype=np.uint16)
        al = a[..., 3]
        out = np.empty((H, W, 4), dtype=np.uint8)
        out[..., 0] = (a[..., 2] * al) >> 8
        out[..., 1] = (a[..., 1] * al) >> 8
        out[..., 2] = (a[..., 0] * al) >> 8
        out[..., 3] = al
        ctypes.memmove(c["bits"], out.ctypes.data, H * W * 4)
        pt_dst, pt_src, size = wt.POINT(x, y), wt.POINT(0, 0), wt.SIZE(W, H)
        return bool(user32.UpdateLayeredWindow(c["hwnd"], c["hdc_screen"],
                                               ctypes.byref(pt_dst), ctypes.byref(size),
                                               c["hdc_mem"], ctypes.byref(pt_src),
                                               0, ctypes.byref(blend), 2))

    def push_recover(c, x, y, img):
        if push(c, x, y, img):
            return
        user32.ReleaseDC(None, c["hdc_screen"])
        c["hdc_screen"] = user32.GetDC(None)   # 显示模式变化会使旧 DC 失效，重取重试
        if not push(c, x, y, img):
            raise RuntimeError(f"推屏失败（含DC重取）GetLastError={ctypes.GetLastError()}")

    bar_h = [0.0] * N_BARS
    ctx = None
    my_gen = None
    while True:
        try:
            need_gen = state["rec_gen"]
            if ctx is None or need_gen != my_gen:
                if ctx is not None:
                    teardown(ctx)
                    ctx = None
                ctx = setup()
                my_gen = need_gen
            if state["recording"]:
                if not ctx["shown"]:
                    user32.ShowWindow(ctx["hwnd"], 8)   # SW_SHOWNA：显示但不抢焦点
                    ctx["shown"] = True
                frame = bg.copy()
                fd = ImageDraw.Draw(frame)
                lv = list(levels)
                silent = (not lv) or lv[-1] < RMS_GATE
                mid = HS // 2
                max_half = (HS - 2 * PAD) // 2 - SS
                total = N_BARS * BAR_W + (N_BARS - 1) * BAR_GAP
                x = (WS - total) // 2
                now_t = time.time()
                for i in range(N_BARS):
                    if lv:
                        idx = max(0.0, min(float(len(lv) - 1), len(lv) - 1 - i * 0.06))
                        j = int(idx)
                        frac = idx - j
                        a, b = lv[j], lv[min(j + 1, len(lv) - 1)]
                        env = a + (b - a) * frac
                    else:
                        env = 0.0
                    scale = 0.55 + 0.45 * abs(np.sin(i * 1.7 + now_t * 5.0))
                    target = min(1.0, env * 13.0) * scale
                    if silent:
                        target = 0.20 + 0.09 * abs(np.sin(i * 0.55 + now_t * 2.2))
                    bar_h[i] += (target - bar_h[i]) * 0.35
                    half = max(SS, int(bar_h[i] * max_half))
                    color = (185, 185, 197, 235) if silent else (238, 238, 245, 255)
                    fd.rounded_rectangle([x, mid - half, x + BAR_W, mid + half],
                                         radius=BAR_W // 2, fill=color)
                    x += BAR_W + BAR_GAP
                _l, _r, _b = target_monitor_workarea()
                _sx = _l + ((_r - _l) - W) // 2
                _sy = _b - H - int(8 * k)
                push_recover(ctx, _sx, _sy, frame)
                if not ctx["diag_done"]:
                    rc = wt.RECT()
                    user32.GetWindowRect(ctx["hwnd"], ctypes.byref(rc))
                    log(f"浮窗显示: 工作区=({_l},{_r},{_b}) 目标=({_sx},{_sy}) "
                        f"矩形=({rc.left},{rc.top},{rc.right},{rc.bottom}) "
                        f"可见={bool(user32.IsWindowVisible(ctx['hwnd']))}")
                    ctx["diag_done"] = True
            else:
                if ctx["shown"]:
                    user32.ShowWindow(ctx["hwnd"], 0)   # SW_HIDE：待命时彻底从所有屏消失
                    ctx["shown"] = False
        except _Rebuild:
            raise
        except Exception as e:
            if ctx is not None:
                teardown(ctx)
            ctx = None
            raise RuntimeError(f"渲染循环异常：{type(e).__name__}: {e}")
        time.sleep(0.033)


# ---------------- 主流程 ----------------
def main():
    if "--selftest" in sys.argv:
        print("3 秒后向当前焦点窗口打字……", flush=True)
        time.sleep(3)
        type_text("部署自检OK：中文English数字123")
        print("自检打字完成", flush=True)
        return

    log("启动：加载识别器……")
    load_replacements()   # 启动即生成/校验替换表文件，方便用户直接编辑
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=MODEL, tokens=TOKENS, use_itn=True, num_threads=4, language="auto",
    )   # 备用引擎（CPU），Whisper 不可用时自动接管
    asr = {"engine": "sensevoice", "sensevoice": rec, "whisper": None}

    if os.path.isdir(WHISPER_DIR):
        try:
            _sp = os.path.join(os.path.dirname(sys.executable), "Lib", "site-packages")
            for _sub in (r"nvidia\cudnn\bin", r"nvidia\cublas\bin", r"nvidia\cuda_runtime\bin"):
                _p = os.path.join(_sp, _sub)
                if os.path.isdir(_p):
                    os.add_dll_directory(_p)
            from faster_whisper import WhisperModel
            asr["whisper"] = WhisperModel(WHISPER_DIR, device="cuda", compute_type="float16")
            asr["engine"] = "whisper"
            log("ASR 引擎：Whisper large-v3-turbo（GPU 常驻；SenseVoice 自动备用）")
        except Exception as e:
            log(f"Whisper 引擎加载失败，回退 SenseVoice：{type(e).__name__}: {e}")
    else:
        log("未找到 Whisper 模型目录，使用 SenseVoice（CPU）")
    log("识别器就绪，托盘常驻中（按住 Ctrl+Space 说话）")

    threading.Thread(target=hotkey_poll_thread, args=(asr,), daemon=True).start()
    threading.Thread(target=_overlay_thread, daemon=True).start()

    reopen_stream()   # 优先用 settings.json 记住的可用麦克风，失效回退系统默认
    threading.Thread(target=audio_watchdog_thread, daemon=True).start()

    def toggle(icon, item):
        state["enabled"] = not state["enabled"]
        icon.icon = ICON_IDLE if state["enabled"] else ICON_OFF
        notify("已恢复 Ctrl+Space 听写" if state["enabled"] else "已暂停：Ctrl+Space 恢复自由")

    def toggle_polish(icon, item):
        state["polish"] = not state.get("polish", False)
        notify(f"润色层：{'开（本地小模型纠错润色）' if state['polish'] else '关（原文直出）'}")

    menu = Menu(
        MenuItem(lambda item: "暂停听写（释放 Ctrl+Space）" if state["enabled"]
                 else "恢复听写", toggle),
        MenuItem(lambda item: "润色后上屏：开" if state.get("polish", False)
                 else "润色后上屏：关", toggle_polish),
        MenuItem("退出", lambda icon: icon.stop()),
    )
    icon = pystray.Icon("LocalDictation", ICON_IDLE, "本地听写：按住 Ctrl+Space 说话", menu)
    icon_ref["icon"] = icon
    icon.run()
    _p = _llm_proc.get("popen")
    if _p:   # 退出程序即释放润色服务占用的显存
        try:
            _p.terminate()
        except Exception:
            pass
    log("已退出")


if __name__ == "__main__":
    main()
