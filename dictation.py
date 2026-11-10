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
DEFAULT_REPL = {
    "get up": "GitHub",
    "get hub": "GitHub",
    "github": "GitHub",
    "mac": "Mac",
    "vs code": "VSCode",
    "vscode": "VSCode",
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

state = {"enabled": True, "recording": False, "rec_start": 0.0, "rec_gen": 0}
frames = []
levels = deque(maxlen=75)   # 20ms/块 → 1.5s 波形历史
icon_ref = {}


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


def hotkey_poll_thread(rec):
    was_down = False
    while True:
        try:
            ctrl = bool(GetAsyncKeyState(VK_CTRL) & 0x8000)
            space = bool(GetAsyncKeyState(VK_SPACE) & 0x8000)
            down = ctrl and space and state["enabled"]
            if down and not was_down and not state["recording"]:
                start_rec()
            elif was_down and not down and state["recording"]:
                stop_rec(rec)
            was_down = down
        except Exception as e:
            log(f"热键轮询异常：{type(e).__name__}: {e}")
        time.sleep(0.015)


# ---------------- 识别与上屏 ----------------
def finish_recording(rec):
    audio = np.concatenate(frames).reshape(-1) if frames else np.zeros(0, dtype="float32")
    icon_ref["icon"].icon = ICON_IDLE
    dur = len(audio) / SR
    if dur < MIN_DUR_S:
        notify("按压太短（<0.25s），未录音")
        return
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms < RMS_GATE:
        notify("未检测到语音（音量过低），未上屏")
        return
    try:
        t0 = time.time()
        s = rec.create_stream()
        s.accept_waveform(SR, audio)
        rec.decode_stream(s)
        text = s.result.text.strip()
        ms = (time.time() - t0) * 1000
        if not text:
            notify("识别结果为空，未上屏")
            return
        fixed = apply_replacements(text)
        type_text(fixed)
        log(f"{dur:.1f}s 音频 解码{ms:.0f}ms：{text}" + (f"（替换为：{fixed}）" if fixed != text else ""))
    except Exception as e:
        notify(f"出错了：{type(e).__name__}：{e}")


def start_rec():
    state["recording"] = True
    state["rec_gen"] += 1   # 每次录音=新代际：浮窗整窗内联重建（毫秒级），杜绝陈旧状态
    state["rec_start"] = time.time()
    frames.clear()
    levels.clear()
    icon_ref["icon"].icon = ICON_REC


def stop_rec(rec):
    state["recording"] = False
    threading.Thread(target=finish_recording, args=(rec,), daemon=True).start()


def audio_callback(indata, _n, _t, _st):
    if state["recording"]:
        frames.append(indata[:, 0].copy())
        levels.append(float(np.sqrt(np.mean(indata ** 2))))


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
    )
    log("识别器就绪，托盘常驻中（按住 Ctrl+Space 说话）")

    threading.Thread(target=hotkey_poll_thread, args=(rec,), daemon=True).start()
    threading.Thread(target=_overlay_thread, daemon=True).start()

    with sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                        blocksize=320, callback=audio_callback):
        def toggle(icon, item):
            state["enabled"] = not state["enabled"]
            icon.icon = ICON_IDLE if state["enabled"] else ICON_OFF
            notify("已恢复 Ctrl+Space 听写" if state["enabled"] else "已暂停：Ctrl+Space 恢复自由")

        menu = Menu(
            MenuItem(lambda item: "暂停听写（释放 Ctrl+Space）" if state["enabled"]
                     else "恢复听写", toggle),
            MenuItem("退出", lambda icon: icon.stop()),
        )
        icon = pystray.Icon("LocalDictation", ICON_IDLE, "本地听写：按住 Ctrl+Space 说话", menu)
        icon_ref["icon"] = icon
        icon.run()
    log("已退出")


if __name__ == "__main__":
    main()
