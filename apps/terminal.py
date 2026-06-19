#!/usr/bin/env python3
"""USBキーボード + Display HAT Mini (ST7789 320x240) をターミナルとして使う。

- 物理 USB キーボードを evdev で読む (/dev/input/by-id/*event-kbd)
- キーコード→文字変換は xkbcommon に任せる (JIS/US 等の配列を正しく扱う)
- pty で bash を起動し、pyte で VT100 画面状態をエミュレート (htop/vim も動く)
- pyte の画面バッファを Display HAT Mini に等幅描画 (カーソル付き)
- select() で PTY 出力 / キー入力を同時に捌く

カーネルドライバ・fbcon 不要のユーザー空間ターミナル。
終了はシェルで `exit` (または外部から kill)。アプリ側の終了ホットキーは持たない
(vim 等で ESC を多用するため、キー連打による誤終了を避ける)。

環境変数:
  CONSOLE_XKB_LAYOUT  キーボード配列 (既定: jp。US配列なら us)
  CONSOLE_FONT_SIZE   フォントサイズ (既定: 14)
"""
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

import evdev
from evdev import InputDevice, ecodes
from PIL import Image, ImageDraw, ImageFont
from displayhatmini import DisplayHATMini
from xkbcommon import xkb

from screensaver import Screensaver

WIDTH, HEIGHT = 320, 240
BG = (0, 0, 0)
# 色指定の無い文字 (コマンド入力・通常出力・プロンプトのリセット部) の色。
# 普通のターミナルに合わせてソフトな白にする (純緑だと全体が緑すぎるため)。
DEFAULT_FG = (220, 220, 220)

ANSI = {
    "black": (40, 40, 40), "red": (255, 90, 90), "green": (90, 220, 110),
    "brown": (200, 180, 60), "yellow": (255, 220, 90), "blue": (110, 150, 255),
    "magenta": (220, 120, 220), "cyan": (120, 220, 220), "white": (230, 230, 230),
    "default": DEFAULT_FG,
}


def fg_color(name):
    """pyte の前景色 (色名 / 6桁hex / "default") を RGB に変換する。"""
    rgb = ANSI.get(name)
    if rgb is not None:
        return rgb
    if isinstance(name, str) and len(name) == 6:
        try:
            return (int(name[0:2], 16), int(name[2:4], 16), int(name[4:6], 16))
        except ValueError:
            pass
    return DEFAULT_FG


def acquire_keyboard():
    """KEY_A と SPACE を持つ実キーボードを探して grab し InputDevice を返す。
    無ければ None。USB/BT どちらでも、抜き差し後でも拾える。"""
    for path in sorted(evdev.list_devices()):
        try:
            d = InputDevice(path)
            keys = d.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_A in keys and ecodes.KEY_SPACE in keys:
                try:
                    d.grab()
                except OSError:
                    pass
                return d
        except Exception:
            continue
    return None


def build_keysym_seq():
    """非印字キー / 特殊キーの keysym -> 送出バイト列。"""
    m = {}

    def add(name, seq):
        try:
            m[xkb.keysym_from_name(name)] = seq
        except Exception:
            pass

    add("Up", "\x1b[A"); add("Down", "\x1b[B")
    add("Right", "\x1b[C"); add("Left", "\x1b[D")
    add("Home", "\x1b[H"); add("End", "\x1b[F")
    add("Prior", "\x1b[5~"); add("Next", "\x1b[6~")  # PageUp / PageDown
    add("Delete", "\x1b[3~")
    add("BackSpace", "\x7f")
    add("Return", "\r"); add("KP_Enter", "\r")
    add("Tab", "\t")
    add("Escape", "\x1b")
    return m


def main():
    import pyte

    layout = os.environ.get("CONSOLE_XKB_LAYOUT", "jp")
    size = int(os.environ.get("CONSOLE_FONT_SIZE", "14"))

    # --- xkb: キーコード -> 文字 (配列対応) ---
    ctx = xkb.Context()
    keymap = ctx.keymap_new_from_names(rules="evdev", model="pc105", layout=layout)
    xstate = keymap.state_new()
    KEYSYM_SEQ = build_keysym_seq()
    print(f"xkb layout: {layout}")

    # --- フォント / グリッド ---
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    font = ImageFont.truetype(font_path, size)
    bbox = font.getbbox("M")
    char_w = max(1, int(round(font.getlength("M"))))
    line_h = (bbox[3] - bbox[1]) + 3
    cols = WIDTH // char_w
    rows = HEIGHT // line_h
    print(f"grid: {cols}x{rows}")

    # --- キーボード (出現待ち。後でホットプラグ再取得もする) ---
    kbd = acquire_keyboard()
    if kbd:
        print(f"keyboard: {kbd.path} ({kbd.name})")
    else:
        print("keyboard: not connected yet (waiting; USB/BTを繋げば自動で拾います)")

    # --- ディスプレイ ---
    display = DisplayHATMini(None)
    display.set_backlight(1.0)
    display.set_led(0.0, 0.0, 0.0)

    # --- pyte 画面 ---
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    # --- pty で bash ---
    pid, master_fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "linux"
        os.environ["LANG"] = os.environ.get("LANG", "C.UTF-8")
        # ~/.bashrc を読み込みつつ、プロンプト表示名だけ差し替える rcfile を使う
        # (色付きプロンプト / エイリアス / ls --color などは .bashrc 由来)。
        rc = os.path.join(os.path.dirname(os.path.abspath(__file__)), "term.bashrc")
        os.chdir(os.path.expanduser("~"))  # 起動時の pwd をホーム (~) にする
        os.execvp("bash", ["bash", "--rcfile", rc, "-i"])
        os._exit(1)
    winsize = struct.pack("HHHH", rows, cols, WIDTH, HEIGHT)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
    # Ctrl+S/Ctrl+Q (XON/XOFF) で出力が固まるのを防ぐ: ソフトフロー制御を無効化
    attrs = termios.tcgetattr(master_fd)
    attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
    termios.tcsetattr(master_fd, termios.TCSANOW, attrs)

    def render():
        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        d = ImageDraw.Draw(img)
        for y in range(rows):
            line = screen.buffer[y]
            for x in range(cols):
                ch = line[x]
                c = ch.data
                if c and c != " ":
                    d.text((x * char_w, y * line_h), c, font=font,
                           fill=fg_color(ch.fg))
        if not screen.cursor.hidden:
            cx = screen.cursor.x * char_w
            cy = screen.cursor.y * line_h
            d.rectangle((cx, cy, cx + char_w - 1, cy + line_h - 1), outline=DEFAULT_FG)
        display.buffer = img
        display.display()

    def send(data: bytes):
        os.write(master_fd, data)

    ctrl_held = [False]  # Ctrl は evdev 側で自前追跡 (xkb のmod定数に依存しない)
    dbg = open("/tmp/console-debug.log", "w", buffering=1)

    def handle_key(code, value):
        # value: 1=down 2=repeat 0=up
        xkc = code + 8  # evdev -> xkb keycode
        # Ctrl の上げ下げを追跡 (xkb state も一応更新)
        if code in (ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL):
            ctrl_held[0] = value != 0
            xstate.update_key(xkc, xkb.XKB_KEY_DOWN if value else xkb.XKB_KEY_UP)
            return
        if value == 0:
            xstate.update_key(xkc, xkb.XKB_KEY_UP)
            return
        if value == 1:
            xstate.update_key(xkc, xkb.XKB_KEY_DOWN)
        # down / repeat: 文字を送出
        syms = xstate.key_get_syms(xkc)
        sym = syms[0] if syms else 0
        sent = None
        if sym in KEYSYM_SEQ:
            sent = KEYSYM_SEQ[sym].encode()
            send(sent)
        else:
            text = xstate.key_get_string(xkc)
            if ctrl_held[0] and len(text) == 1 and text.isalpha():
                sent = bytes([ord(text.lower()) - ord("a") + 1])
                send(sent)
            elif text:
                sent = text.encode()
                send(sent)
        dbg.write(f"key code={code} val={value} sym={sym:#x} "
                  f"utf8={xstate.key_get_string(xkc)!r} sent={sent!r}\n")

    render()
    print("console ready. (exit the shell to quit)")
    dirty = False
    last_render = 0.0
    last_kbd_try = 0.0
    # キー入力のみを活動とみなす (画面出力 = htop/tail -f 等は活動に含めない)
    saver = Screensaver(display, on_wake=render)
    while True:
        # キーボード未取得なら 1秒ごとに再取得を試みる (USB/BT ホットプラグ)
        if kbd is None and (time.time() - last_kbd_try) >= 1.0:
            last_kbd_try = time.time()
            kbd = acquire_keyboard()
            if kbd:
                dbg.write(f"keyboard acquired: {kbd.path} {kbd.name}\n")

        fds = [master_fd] + ([kbd.fd] if kbd else [])
        try:
            r, _, _ = select.select(fds, [], [], 0.05 if not saver.awake else 0.1)
        except (InterruptedError, OSError):
            # kbd.fd が無効化(切断)された可能性
            if kbd is not None:
                try:
                    kbd.close()
                except Exception:
                    pass
                kbd = None
            continue
        if master_fd in r:
            try:
                data = os.read(master_fd, 8192)
            except OSError:
                data = b""
            if not data:
                break  # シェル終了
            stream.feed(data)
            dirty = True
        if kbd is not None and kbd.fd in r:
            try:
                woke = False
                for ev in kbd.read():
                    if ev.type != ecodes.EV_KEY:
                        continue
                    if not saver.awake:
                        # スクリーンセーバー/消灯中: 入力は復帰のみに使い PTY へ送らない
                        if ev.value in (1, 2):
                            saver.note_activity()
                            woke = True
                        continue
                    if woke:
                        continue  # 復帰させたバッチの残りキーも消費する
                    if ev.value in (1, 2):
                        saver.note_activity()  # キー入力 = 活動
                    handle_key(ev.code, ev.value)
            except BlockingIOError:
                pass
            except OSError:
                # キーボード切断 -> 解放して再取得待ちへ
                try:
                    kbd.close()
                except Exception:
                    pass
                kbd = None
        saver.tick()  # アイドル判定 (5分でDVDロゴ, 15分で消灯) / ロゴ更新
        now = time.time()
        if saver.awake and dirty and (now - last_render) >= 0.04:
            render()
            dirty = False
            last_render = now

    try:
        if kbd is not None:
            kbd.ungrab()
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    ImageDraw.Draw(img).text((4, 4), "console closed", font=font, fill=(255, 120, 120))
    display.buffer = img
    display.display()
    print("bye")


if __name__ == "__main__":
    main()
