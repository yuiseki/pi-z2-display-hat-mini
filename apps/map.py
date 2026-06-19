#!/usr/bin/env python3
"""Display HAT Mini の4ボタンで操作する地図ページャ (0.5タイル離散 + 強キャッシュ + プリレンダ)。

- ナビは「ハーフタイル単位」の整数インデックス (x,y)。1ステップ = 0.5タイル
- 各ビューを mbgl-render で 512x512 描画し cache/{style-hash}/z/x/y.png に保存
  (x,y はハーフタイルインデックス)
- キャッシュヒットは即表示。アイドル中は隣接(上下左右+ズーム±)をプリレンダ
- ボタン:
    単押し : 左列(A/B)=←(x-1)  右列(X/Y)=→(x+1)   [0.5タイル]
    ダブル : 上段(A/X)=↑(y-1)  下段(B/Y)=↓(y+1)   [0.5タイル]
    長押し : 上段(A/X)=ズームイン(z+1)  下段(B/Y)=ズームアウト(z-1)
- LED: 押下=緑 / ダブル=紫 / 長押し=橙 / 操作描画=青点滅 / 背景プリレンダ=青弱点灯 / 待機=消灯
"""
import hashlib
import math
import os
import select
import subprocess
import sys
import time
import urllib.request

import evdev
import RPi.GPIO as GPIO
from evdev import InputDevice, ecodes
from PIL import Image, ImageDraw, ImageFont
from displayhatmini import DisplayHATMini

from screensaver import Screensaver

# スーパバイザへの切替要求チャネル
REQUEST = "/tmp/pi-display/request"
CTRLC_WINDOW = 1.5  # Ctrl+C を連続2回とみなす最大間隔(秒)

WIDTH, HEIGHT = 320, 240
HOME = os.path.expanduser("~")
RENDER = os.path.join(HOME, "mbgl-render")
STYLE = "https://yuiseki.dev/static/styles/osm-bright.json"
TILE_CACHE = os.path.join(HOME, "map-cache")
MBGL_CACHE = "/tmp/pager-cache.db"

ENV = {
    **os.environ,
    "LIBGL_ALWAYS_SOFTWARE": "1",
    "GALLIUM_DRIVER": "llvmpipe",
    "EGL_PLATFORM": "surfaceless",
    "LD_LIBRARY_PATH": os.path.join(HOME, "mbgl-libs"),
}

ZMIN, ZMAX = 3, 16
LONGPRESS = 0.6
DOUBLE_WINDOW = 0.35
PRERENDER_RADIUS = 4   # 現zoomで中心から先読みする半タイル半径(貪欲。±2タイル≈81枚)
PRERENDER_ZOOMS = (1, -1, 2, -2)  # 先読みする隣接ズーム差

font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 12)
font_s = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 11)

display = DisplayHATMini(None)
display.set_backlight(1.0)
display.set_led(0.0, 0.0, 0.0)

BTN = {"A": DisplayHATMini.BUTTON_A, "B": DisplayHATMini.BUTTON_B,
       "X": DisplayHATMini.BUTTON_X, "Y": DisplayHATMini.BUTTON_Y}

def style_hash():
    try:
        data = urllib.request.urlopen(STYLE, timeout=10).read()
        return hashlib.sha256(data).hexdigest()[:12]
    except Exception:
        return hashlib.sha256(STYLE.encode()).hexdigest()[:12]


HASH = style_hash()
CACHE_DIR = os.path.join(TILE_CACHE, HASH)


# ---- ハーフタイルインデックス(整数) <-> 緯度経度。1ステップ = 0.5タイル ----
def view_center(z, hx, hy):
    n = 2 ** z
    fx, fy = hx / 2.0, hy / 2.0  # 分数タイル座標(ビュー中心の点)
    lon = fx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * fy / n))))
    return lon, lat


def lonlat_to_half(z, lon, lat):
    n = 2 ** z
    lat = max(-85.05, min(85.05, lat))
    fx = (lon + 180.0) / 360.0 * n
    fy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return round(fx * 2), round(fy * 2)


def half_extent(z):
    return 2 * (2 ** z)  # x の周期 / y の最大


def tile_path(z, x, y):
    return os.path.join(CACHE_DIR, str(z), str(x), f"{y}.png")


def is_cached(z, x, y):
    p = tile_path(z, x, y)
    return os.path.exists(p) and os.path.getsize(p) > 1000


def is_pressed(name):
    return GPIO.input(BTN[name]) == 0


def any_pressed():
    return any(is_pressed(n) for n in BTN)


# ---- スーパバイザへの切替要求 / キーボード(A+X, Ctrl+C連続2回 → ターミナルへ) ----
def request_mode(mode):
    try:
        os.makedirs(os.path.dirname(REQUEST), exist_ok=True)
        with open(REQUEST, "w") as f:
            f.write(mode)
    except Exception:
        pass


def switch_to_terminal():
    # 直前の表示に "Exiting..." を被せてから切替
    base = _ui["last"].copy() if _ui["last"] is not None else Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    d = ImageDraw.Draw(base)
    msg = "Exiting..."
    mb = d.textbbox((0, 0), msg, font=font)
    mw, mh = mb[2] - mb[0], mb[3] - mb[1]
    mx, my = (WIDTH - mw) // 2, (HEIGHT - mh) // 2
    d.rectangle((mx - 12, my - 8, mx + mw + 12, my + mh + 10), fill=(150, 0, 0))
    d.text((mx - mb[0], my - mb[1]), msg, font=font, fill=(255, 255, 255))
    display.buffer = base
    display.display()
    time.sleep(0.5)
    request_mode("terminal")
    display.set_led(0.0, 0.0, 0.0)
    sys.exit(0)


def acquire_keyboard():
    for p in sorted(evdev.list_devices()):
        try:
            d = InputDevice(p)
            keys = d.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_A in keys and ecodes.KEY_SPACE in keys:
                return d
        except Exception:
            continue
    return None


_kbd = {"dev": None, "t": -999.0, "ctrl": False, "cc": []}
ctrlc_hint_until = 0.0  # この時刻まで「Press Ctrl+C again」を表示
_ui = {"last": None}    # 直近の合成画像 (Exiting... 表示の下地に使う)


def keyboard_present():
    return _kbd["dev"] is not None


def poll_keyboard():
    """KBの再取得(ホットプラグ)と Ctrl+C 検出。
    戻り値: "exit"(連続2回) / "first"(1回目) / "key"(その他のキー入力=活動) / None。"""
    if time.time() - _kbd["t"] > 1.0:
        _kbd["t"] = time.time()
        if _kbd["dev"] is None:
            _kbd["dev"] = acquire_keyboard()
    dev = _kbd["dev"]
    if dev is None:
        return None
    result = None
    saw_key = False
    try:
        if select.select([dev.fd], [], [], 0)[0]:
            for ev in dev.read():
                if ev.type != ecodes.EV_KEY:
                    continue
                if ev.value == 1:
                    saw_key = True  # スクリーンセーバー復帰判定用 (どのキーでも活動)
                if ev.code in (ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL):
                    _kbd["ctrl"] = ev.value != 0
                elif ev.code == ecodes.KEY_C and ev.value == 1 and _kbd["ctrl"]:
                    now = time.time()
                    _kbd["cc"] = [t for t in _kbd["cc"] if now - t < CTRLC_WINDOW]
                    _kbd["cc"].append(now)
                    result = "exit" if len(_kbd["cc"]) >= 2 else "first"
    except (BlockingIOError, OSError):
        _kbd["dev"] = None  # 切断 → 次回再取得
    return result or ("key" if saw_key else None)


# ---- 電源状態 (PiSugar): 給電中のみ貪欲プリレンダ ----
PI_POWER = os.path.join(HOME, ".local/bin/pi-power")
_power = {"plugged": True, "pct": -1, "t": -999.0}


def power_plugged():
    """battery_power_plugged + battery% を 10秒 TTL でキャッシュ取得。失敗時は前回値。"""
    if time.time() - _power["t"] > 10:
        _power["t"] = time.time()
        try:
            out = subprocess.run([PI_POWER], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "battery_power_plugged" in line:
                    _power["plugged"] = "true" in line.lower()
                elif line.strip().startswith("battery:"):
                    try:
                        _power["pct"] = int(round(float(line.split(":", 1)[1])))
                    except ValueError:
                        pass
        except Exception:
            pass
    return _power["plugged"]


def power_label():
    pct = _power["pct"]
    tag = "AC" if _power["plugged"] else "BAT"
    return f"{tag} {pct}%" if pct >= 0 else tag


# 起動位置: 東京駅 z12
cam = {"z": 12}
cam["x"], cam["y"] = lonlat_to_half(12, 139.767, 35.681)


def render_tile(z, x, y, background):
    if is_cached(z, x, y):
        return "cached"
    path = tile_path(z, x, y)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lon, lat = view_center(z, x, y)
    proc = subprocess.Popen(
        [RENDER, "-s", STYLE, "-o", path, "-c", MBGL_CACHE,
         "-z", str(z), "-x", f"{lon:.6f}", "-y", f"{lat:.6f}"],
        env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.time() + 120
    on = True
    i = 0
    while proc.poll() is None:
        if background:
            if any_pressed():
                proc.kill()
                try:
                    os.remove(path)
                except OSError:
                    pass
                display.set_led(0.0, 0.0, 0.0)
                return "interrupted"
            display.set_led(0.0, 0.0, 0.18)  # 弱点灯 = 背景プリレンダ
        else:
            if i % 4 == 0:
                display.set_led(0.0, 0.0, 0.9 if on else 0.0)  # 点滅 = 操作描画
                on = not on
        i += 1
        time.sleep(0.05)
        if time.time() > deadline:
            proc.kill()
            break
    display.set_led(0.0, 0.0, 0.0)
    return "rendered" if is_cached(z, x, y) else "interrupted"


def display_current():
    render_tile(cam["z"], cam["x"], cam["y"], background=False)
    path = tile_path(cam["z"], cam["x"], cam["y"])
    try:
        src = Image.open(path).convert("RGB")
        sw, sh = src.size
        scale = max(WIDTH / sw, HEIGHT / sh)
        img = src.resize((int(sw * scale), int(sh * scale)))
        iw, ih = img.size
        l, t = (iw - WIDTH) // 2, (ih - HEIGHT) // 2
        img = img.crop((l, t, l + WIDTH, t + HEIGHT))
    except Exception as e:  # noqa: BLE001
        img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        ImageDraw.Draw(img).text((4, 20), f"err:\n{e}", font=font, fill=(255, 90, 90))

    d = ImageDraw.Draw(img)
    lon, lat = view_center(cam["z"], cam["x"], cam["y"])
    power_plugged()  # キャッシュ更新
    hud = f"z{cam['z']} {lat:.3f},{lon:.3f} {power_label()}"
    if keyboard_present():
        hud += " KYBD"  # キーボード認識中 (A+X / Ctrl+C 有効)
    hb = d.textbbox((0, 0), hud, font=font)
    hx = (WIDTH - (hb[2] - hb[0])) // 2
    d.rectangle((hx - 2, 0, hx + (hb[2] - hb[0]) + 2, 15), fill=(0, 0, 0))
    d.text((hx - hb[0], 1 - hb[1] + 1), hud, font=font, fill=(255, 230, 0))
    # 下部バー: Ctrl+C 1回目はヒント(赤帯)を凡例に被せる。それ以外は操作凡例。
    if time.time() < ctrlc_hint_until:
        msg = "Press Ctrl+C again to exit"
        mb = d.textbbox((0, 0), msg, font=font_s)
        mx = (WIDTH - (mb[2] - mb[0])) // 2
        d.rectangle((0, HEIGHT - 16, WIDTH, HEIGHT), fill=(150, 0, 0))
        d.text((mx, HEIGHT - 14), msg, font=font_s, fill=(255, 255, 255))
    else:
        legend = "1: ← →   2: ↑ ↓   L: + -"
        lb = d.textbbox((0, 0), legend, font=font_s)
        lx = (WIDTH - (lb[2] - lb[0])) // 2
        d.rectangle((0, HEIGHT - 16, WIDTH, HEIGHT), fill=(0, 0, 0))
        d.text((lx, HEIGHT - 14), legend, font=font_s, fill=(150, 210, 255))
    _ui["last"] = img  # Exiting... 表示の下地
    try:
        img.save("/tmp/pager_hud.png")
    except Exception:
        pass
    display.buffer = img
    display.display()


def ring(cx, cy, d, m):
    """中心(cx,cy)から Chebyshev 距離 d の半タイル枠を生成。"""
    for dx in range(-d, d + 1):
        for dy in range(-d, d + 1):
            if max(abs(dx), abs(dy)) != d:
                continue
            ny = cy + dy
            if 0 <= ny <= m:
                yield ((cx + dx) % m, ny)


def minimal_targets():
    """バッテリー駆動中: 直近の上下左右4枚だけ先読み(省電力)。"""
    z, x, y = cam["z"], cam["x"], cam["y"]
    m = half_extent(z)
    yield (z, (x - 1) % m, y)
    yield (z, (x + 1) % m, y)
    if y - 1 >= 0:
        yield (z, x, y - 1)
    if y + 1 <= m:
        yield (z, x, y + 1)


def prerender_targets():
    """貪欲な先読み候補を優先度(近い順)に生成。"""
    z, x, y = cam["z"], cam["x"], cam["y"]
    m = half_extent(z)
    lon, lat = view_center(z, x, y)
    for d in range(1, PRERENDER_RADIUS + 1):
        for nx, ny in ring(x, y, d, m):
            yield (z, nx, ny)
        if d == 1:
            # 直近リングの後に隣接ズームの中心+周辺も温める
            for dz in PRERENDER_ZOOMS:
                nz = z + dz
                if not (ZMIN <= nz <= ZMAX):
                    continue
                zx, zy = lonlat_to_half(nz, lon, lat)
                zm = half_extent(nz)
                yield (nz, zx, zy)
                for r in (1, 2):
                    for nx, ny in ring(zx, zy, r, zm):
                        yield (nz, nx, ny)


def capture_gesture(name):
    display.set_led(0.0, 1.0, 0.0)  # 緑
    t_down = time.time()
    while is_pressed(name):
        if time.time() - t_down >= LONGPRESS:
            display.set_led(1.0, 0.4, 0.0)  # 橙
            time.sleep(0.5)
            while is_pressed(name):
                time.sleep(0.02)
            return "long"
        time.sleep(0.01)
    t_up = time.time()
    while time.time() - t_up < DOUBLE_WINDOW:
        if is_pressed(name):
            display.set_led(0.8, 0.0, 1.0)  # 紫
            while is_pressed(name):
                time.sleep(0.02)
            time.sleep(0.35)
            return "double"
        time.sleep(0.01)
    return "single"


def apply_gesture(name, g):
    m = half_extent(cam["z"])
    left = name in ("A", "B")
    top = name in ("A", "X")
    if g == "single":
        cam["x"] = (cam["x"] + (-1 if left else 1)) % m
    elif g == "double":
        cam["y"] = max(0, min(m, cam["y"] + (-1 if top else 1)))
    elif g == "long":
        lon, lat = view_center(cam["z"], cam["x"], cam["y"])
        cam["z"] = max(ZMIN, min(ZMAX, cam["z"] + (1 if top else -1)))
        cam["x"], cam["y"] = lonlat_to_half(cam["z"], lon, lat)


def main():
    print(f"map pager (cache={CACHE_DIR}, half-tile steps)")
    global ctrlc_hint_until
    display_current()
    last_pw = power_plugged()
    last_pct = _power["pct"]
    last_kbd = keyboard_present()
    saver = Screensaver(display, on_wake=display_current)
    while True:
        kev = poll_keyboard()
        pressed = any_pressed()

        # --- スクリーンセーバー/消灯中: 入力で復帰、なければロゴを進める ---
        if not saver.awake:
            if pressed or kev is not None:
                saver.note_activity()       # 通常画面へ復帰 (on_wake=display_current)
                while any_pressed():         # 復帰させた入力は消費 (移動・切替に使わない)
                    time.sleep(0.02)
            else:
                saver.tick()                 # DVD ロゴを 1 フレーム進める / 消灯維持
                time.sleep(0.03)
            continue

        # 操作があればアイドルタイマを更新
        if pressed or kev is not None:
            saver.note_activity()

        # KB あり時のみ: A+X 同時押し / Ctrl+C でターミナルへ切替
        if keyboard_present():
            if is_pressed("A") and is_pressed("X"):
                switch_to_terminal()
            if kev == "exit":
                switch_to_terminal()
            elif kev == "first":
                ctrlc_hint_until = time.time() + CTRLC_WINDOW
                display_current()  # ヒント表示
        # ヒント期限切れ → 消すため再描画
        if ctrlc_hint_until and time.time() > ctrlc_hint_until:
            ctrlc_hint_until = 0.0
            display_current()
        # キーボードの抜き差しで KYBD 表示を更新
        if keyboard_present() != last_kbd:
            last_kbd = keyboard_present()
            display_current()
        handled = False
        for name in ("A", "B", "X", "Y"):
            if is_pressed(name):
                g = capture_gesture(name)
                display.set_led(0.0, 0.0, 0.0)
                apply_gesture(name, g)
                display_current()
                while any_pressed():
                    time.sleep(0.02)
                handled = True
                break
        if handled:
            continue
        # 電源状態が変わったらアイドル中でも HUD を更新 (AC/BAT)
        plugged = power_plugged()
        if plugged != last_pw or _power["pct"] != last_pct:
            last_pw = plugged
            last_pct = _power["pct"]
            display_current()
        # 給電中は貪欲、バッテリー駆動中は最小限の先読み (awake 時のみ)
        gen = prerender_targets() if plugged else minimal_targets()
        nxt = next((t for t in gen if not is_cached(*t)), None)
        if nxt:
            render_tile(*nxt, background=True)
        else:
            time.sleep(0.2)  # 周辺すべて温まったら省エネ待機
        # ループ末尾でアイドル判定 (5分でスクリーンセーバー, 15分で消灯)
        saver.tick()


if __name__ == "__main__":
    main()
