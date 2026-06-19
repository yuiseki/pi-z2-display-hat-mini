#!/usr/bin/env python3
"""pi-z2-display-hat-mini スーパバイザ。

ディスプレイとキーボードの有無に応じて MAP / TERMINAL の子アプリを起動・停止・切替する。
描画や入力処理は子アプリが行い、本プロセスはオーケストレーションに徹する。

モード:
  STANDBY  : ディスプレイ無し。何もしないが常駐し再検出を継続。
  MAP      : apps/map.py
  TERMINAL : apps/terminal.py

切替トリガ:
  - 子アプリ / pi-map が /tmp/pi-display/request に "map" / "terminal" を書く
  - TERMINAL 中にキーボードが切断 → MAP へ自動移行
詳細は docs/spec.md を参照。
"""
import os
import signal
import subprocess
import sys
import time

import evdev
from evdev import InputDevice, ecodes

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable  # supervisor を起動した python (venv) を子にも使う
TERMINAL_APP = os.path.join(HERE, "apps", "terminal.py")
MAP_APP = os.path.join(HERE, "apps", "map.py")
REQUEST = "/tmp/pi-display/request"
SPIDEV = "/dev/spidev0.1"  # Display HAT Mini の SPI (CE1)


def log(*a):
    print("[supervisor]", *a, flush=True)


def display_present():
    # HAT に EEPROM が無いため厳密検出は不可。SPI 有効を proxy とする (docs/spec.md 6.1)。
    return os.path.exists(SPIDEV)


def keyboard_present():
    for p in evdev.list_devices():
        try:
            d = InputDevice(p)
            keys = d.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_A in keys and ecodes.KEY_SPACE in keys:
                return True
        except Exception:
            continue
    return False


def read_request():
    try:
        if os.path.exists(REQUEST):
            with open(REQUEST) as f:
                m = f.read().strip()
            os.remove(REQUEST)
            return m or None
    except Exception:
        pass
    return None


def clear_request():
    try:
        os.remove(REQUEST)
    except OSError:
        pass


def start(mode):
    app = TERMINAL_APP if mode == "terminal" else MAP_APP
    log("start", mode)
    return subprocess.Popen([PY, "-u", app], start_new_session=True)


def stop(proc):
    """子プロセスグループを停止し、HAT/GPIO/KB が解放されるまで待つ。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    for _ in range(30):
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
    time.sleep(0.8)  # settle (fd 解放待ち)


def decide_initial():
    # ディスプレイ+KB → TERMINAL / ディスプレイのみ → MAP (docs/spec.md 4.2)
    return "terminal" if keyboard_present() else "map"


def main():
    clear_request()
    child = None
    mode = None
    log("starting. display=%s kbd=%s" % (display_present(), keyboard_present()))
    try:
        while True:
            # ディスプレイ無し → STANDBY (常駐し再検出)
            if not display_present():
                if child is not None:
                    stop(child)
                    child, mode = None, None
                    log("display gone -> STANDBY")
                time.sleep(3)
                continue

            # 切替要求 (pi-map / 子アプリ) を最優先で処理
            req = read_request()
            if req in ("map", "terminal"):
                if req != mode or child is None:
                    stop(child)
                    mode = req
                    child = start(mode)
                    time.sleep(0.5)
                continue

            # 子が居なければモード決定して起動
            if child is None:
                mode = decide_initial()
                child = start(mode)
                time.sleep(0.5)
                continue

            # 子が自発終了 (シェル exit など) → 次ループで要求/再決定
            if child.poll() is not None:
                log("child exited (mode=%s rc=%s)" % (mode, child.returncode))
                child, mode = None, None
                continue

            # TERMINAL 中に KB 切断 → MAP へ自動移行 (docs/spec.md 11.1)
            if mode == "terminal" and not keyboard_present():
                log("keyboard removed in TERMINAL -> MAP")
                stop(child)
                mode = "map"
                child = start(mode)
                continue

            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        stop(child)
        log("bye")


if __name__ == "__main__":
    main()
