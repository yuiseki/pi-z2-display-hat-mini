#!/usr/bin/env python3
"""アイドル時のスクリーンセーバー (bouncing DVD logo) と消灯を司る共有ヘルパ。

map.py / terminal.py の両アプリが使う。HAT/画面の占有は呼び出し側 (子アプリ) に
従う設計で、supervisor は一切触れない (docs/spec.md 8 の排他方針)。

状態:
  ACTIVE : 通常表示。最後の操作からの経過時間で遷移する。
  SAVER  : 5 分無操作。画面いっぱいを跳ね回る "DVD" ロゴを描画 (壁で色変化)。
  TILE   : 10 分無操作。広域地図タイルを数枚プリレンダ→キャッシュし、正方形タイルを
           跳ね回らせる。壁にぶつかる度にキャッシュ画像を差し替える (maplibre 版と同仕様)。
  OFF    : 消灯。バッテリー駆動は 30 分、給電中 (battery_power_plugged) は 12 時間。

使い方 (ホスト側ループ):
  saver = Screensaver(display, on_wake=redraw_fn)
  ...
  if not saver.awake:           # SAVER / OFF 中
      if 入力あり:
          saver.note_activity() # 通常画面へ復帰 (on_wake が呼ばれる)。入力は消費する
      else:
          saver.tick()          # ロゴを 1 フレーム進める / 消灯維持
      continue
  if 入力あり:
      saver.note_activity()     # アイドルタイマ更新
  ...通常処理...
  saver.tick()                  # 末尾でアイドル判定 (5分→SAVER, 15分→OFF)

タイマは環境変数 PI_SAVER_AFTER / PI_OFF_AFTER (秒) で上書き可能 (動作確認・調整用)。
"""
import os
import random
import subprocess
import tempfile
import threading
import time

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 320, 240

# pi-screensaver / pi-saver が書く制御ファイル (手動トリガ / 動作確認用)
#   saver|on -> 直ちに SAVER / off -> 直ちに OFF / wake -> 通常表示へ復帰
CONTROL = "/tmp/pi-display/saver"

SAVER_AFTER = int(os.environ.get("PI_SAVER_AFTER", 5 * 60))    # 5 分: DVD ロゴ
TILE_AFTER = int(os.environ.get("PI_TILE_AFTER", 10 * 60))     # 10 分: 跳ねる地図タイル
OFF_AFTER = int(os.environ.get("PI_OFF_AFTER", 30 * 60))       # 30 分: 消灯 (バッテリー駆動時)
OFF_AFTER_AC = int(os.environ.get("PI_OFF_AFTER_AC", 12 * 3600))  # 12 時間: 消灯 (給電中)
FRAME_INTERVAL = 0.07                                          # ロゴ更新間隔 (約 14fps)

# PiSugar 給電判定 (battery_power_plugged)。給電中は消灯を遅らせる (OFF_AFTER_AC)。
PI_POWER = os.path.join(os.path.expanduser("~"), ".local/bin/pi-power")

# --- 跳ねる地図タイル段階 (maplibre 版と同仕様) ---
# mbgl-render (llvmpipe) で広域タイルを数枚プリレンダして画像キャッシュし、壁に
# ぶつかる度にキャッシュ画像を差し替える (描画ゼロ = プチフリーズ無し)。レンダリングは
# ブロッキングなので背景スレッドで行い、タッチ復帰の応答性を保つ。
_HOME = os.path.expanduser("~")
TILE_RENDER = os.path.join(_HOME, "mbgl-render")
TILE_MBGL_CACHE = "/tmp/saver-tiles.db"
TILE_ENV = {
    **os.environ,
    "LIBGL_ALWAYS_SOFTWARE": "1",
    "GALLIUM_DRIVER": "llvmpipe",
    "EGL_PLATFORM": "surfaceless",
    "LD_LIBRARY_PATH": os.path.join(_HOME, "mbgl-libs"),
}
TILE_STYLES = [
    "https://tile.openstreetmap.jp/styles/osm-bright-ja/style.json",
    "https://tile.openstreetmap.jp/styles/maptiler-basic-ja/style.json",
    "https://yuiseki.dev/static/styles/osm-fiord.json",
]
TILE_REGIONS = [  # (lat, lon)
    (48.8566, 2.3522),    # Paris
    (40.7128, -74.0060),  # New York
    (35.6895, 139.6917),  # Tokyo
    (34.3853, 132.4553),  # Hiroshima
]
TILE_ZOOM = float(os.environ.get("PI_TILE_ZOOM", 4))           # 広域 (小さいほど広い)
TILE_SIZE = 120                                                # 正方形タイルの一辺(px)
TILE_CACHE_N = int(os.environ.get("PI_TILE_CACHE", 8))         # プリレンダ枚数

# 壁にぶつかる度に切り替わる色 (DVD ロゴ風)
COLORS = [
    (255, 80, 80), (80, 200, 255), (120, 255, 120), (255, 220, 80),
    (220, 120, 255), (255, 150, 60), (120, 160, 255), (255, 255, 255),
]

HERE = os.path.dirname(os.path.abspath(__file__))
# 実物の DVD-Video ロゴ画像。アルファを色替え用のシルエットマスクに使う。
LOGO_PATH = os.environ.get("PI_DVD_LOGO", os.path.join(HERE, "..", "assets", "dvd-logo.png"))
LOGO_WIDTH = 104   # 画面内を跳ねるロゴの横幅(px)


def _load_font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


class Screensaver:
    ACTIVE, SAVER, TILE, OFF = "active", "saver", "tile", "off"

    def __init__(self, display, on_wake=None, brightness=1.0,
                 saver_after=SAVER_AFTER, tile_after=TILE_AFTER,
                 off_after=OFF_AFTER, off_after_ac=OFF_AFTER_AC):
        self.display = display
        self.on_wake = on_wake          # 復帰時にホストへ通常画面の再描画を促すコールバック
        self.brightness = brightness    # 通常時 / スクリーンセーバー時のバックライト
        self.saver_after = saver_after
        self.tile_after = tile_after
        self.off_after = off_after          # バッテリー駆動時の消灯までの秒数
        self.off_after_ac = off_after_ac    # 給電中の消灯までの秒数
        self.state = self.ACTIVE
        self.last = time.time()
        self._next_frame = 0.0
        # ロゴ (位置・速度・色)。実ロゴ画像 (assets/dvd-logo.png) を色替え用の
        # アルファマスクとして読み込む。失敗時は "DVD" テキストにフォールバック。
        self.logo_mask = None
        self.lw, self.lh = 0, 0
        self.font = None
        self._tox = self._toy = 0
        self._load_logo()
        self.x, self.y = 36.0, 36.0
        self.vx, self.vy = 3.0, 2.4
        self.ci = 0
        # 跳ねる地図タイル段階の状態 (位置・速度・キャッシュ・背景レンダスレッド)
        self.tx, self.ty = 30.0, 30.0
        self.tvx, self.tvy = 3.0, 2.4
        self.tci = 0
        self.tiles = []                 # プリレンダ済み正方形タイル (PIL Image)
        self.tile_show = 0
        self._tile_stop = threading.Event()
        self._tile_thread = None
        self._pw_t = -999.0             # pi-power の取得時刻 (10秒 TTL)
        self._pw_plugged = True         # 給電中か (既定: 給電とみなす)

    @property
    def awake(self):
        return self.state == self.ACTIVE

    def note_activity(self):
        """入力検出時に呼ぶ。アイドルタイマを更新し、必要なら通常表示へ復帰する。"""
        self.last = time.time()
        if self.state != self.ACTIVE:
            self._stop_tiles()
            self.state = self.ACTIVE
            self.display.set_backlight(self.brightness)
            print("[screensaver] -> ACTIVE", flush=True)
            if self.on_wake:
                try:
                    self.on_wake()
                except Exception:
                    pass

    def _plugged(self):
        """PiSugar の給電状態を 10 秒 TTL で取得 (pi-power)。失敗時は前回値。"""
        if time.time() - self._pw_t > 10:
            self._pw_t = time.time()
            try:
                out = subprocess.run([PI_POWER], capture_output=True, text=True,
                                     timeout=5).stdout
                for line in out.splitlines():
                    if "battery_power_plugged" in line:
                        self._pw_plugged = "true" in line.lower()
                        break
            except Exception:
                pass
        return self._pw_plugged

    def _current_off_after(self):
        """給電中は OFF_AFTER_AC (12h)、バッテリー駆動は OFF_AFTER (30m)。"""
        return self.off_after_ac if self._plugged() else self.off_after

    def _read_control(self):
        """pi-screensaver / pi-saver が書いた制御コマンドを読み取り、消費する。"""
        try:
            with open(CONTROL) as f:
                cmd = f.read().strip().lower()
            os.remove(CONTROL)
            return cmd or None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def tick(self):
        """状態を更新する。SAVER 中はロゴを 1 フレーム進める。現在の状態を返す。
        pi-screensaver / pi-saver の制御コマンド (CONTROL ファイル) も処理する。"""
        cmd = self._read_control()
        if cmd == "wake":
            self.note_activity()
        elif cmd in ("saver", "on"):
            self.last = time.time() - self.saver_after  # 直ちに SAVER 相当へ
        elif cmd == "tile":
            self.last = time.time() - self.tile_after   # 直ちに TILE 相当へ
        elif cmd == "off":
            self.last = time.time() - self._current_off_after()  # 直ちに OFF 相当へ

        idle = time.time() - self.last
        if idle >= self._current_off_after():
            if self.state != self.OFF:
                self._stop_tiles()
                self.state = self.OFF
                self._blank()
                self.display.set_backlight(0.0)
                print("[screensaver] -> OFF", flush=True)
            return self.state
        if idle >= self.tile_after:
            if self.state != self.TILE:
                self.state = self.TILE
                self.display.set_backlight(self.brightness)
                self._next_frame = 0.0
                self.tx, self.ty = 30.0, 30.0
                self.tvx, self.tvy = 3.0, 2.4
                self.tile_show = 0
                self._start_tile_prerender()
                print("[screensaver] -> TILE", flush=True)
            self._animate_tile()
            return self.state
        if idle >= self.saver_after:
            if self.state != self.SAVER:
                self._stop_tiles()  # SAVER は TILE より手前。TILE 由来の残スレッドを止める
                self.state = self.SAVER
                self.display.set_backlight(self.brightness)
                self._next_frame = 0.0
                print("[screensaver] -> SAVER", flush=True)
            self._animate()
            return self.state
        return self.state  # ACTIVE: 通常画面はホストが描く

    # ---- 描画 ----
    def _blank(self):
        img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        self.display.buffer = img
        self.display.display()

    def _load_logo(self):
        """DVD ロゴ画像をマスクとして読み込む。失敗時は文字ロゴにフォールバック。"""
        try:
            im = Image.open(LOGO_PATH).convert("RGBA")
            w, h = im.size
            nh = max(1, round(h * LOGO_WIDTH / w))
            im = im.resize((LOGO_WIDTH, nh))
            self.logo_mask = im.getchannel("A")  # 透過 → 色替え用シルエット
            self.lw, self.lh = LOGO_WIDTH, nh
        except Exception:
            self.logo_mask = None
            self.font = _load_font(30)
            d = ImageDraw.Draw(Image.new("RGB", (WIDTH, HEIGHT)))
            b = d.textbbox((0, 0), "DVD", font=self.font)
            self.lw, self.lh = b[2] - b[0], b[3] - b[1]
            self._tox, self._toy = b[0], b[1]

    def _animate(self):
        now = time.time()
        if now < self._next_frame:
            return
        self._next_frame = now + FRAME_INTERVAL

        self.x += self.vx
        self.y += self.vy
        bounced = False
        if self.x <= 0:
            self.x, self.vx, bounced = 0.0, abs(self.vx), True
        elif self.x + self.lw >= WIDTH:
            self.x, self.vx, bounced = WIDTH - self.lw, -abs(self.vx), True
        if self.y <= 0:
            self.y, self.vy, bounced = 0.0, abs(self.vy), True
        elif self.y + self.lh >= HEIGHT:
            self.y, self.vy, bounced = HEIGHT - self.lh, -abs(self.vy), True
        if bounced:
            self.ci = (self.ci + 1) % len(COLORS)

        img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        if self.logo_mask is not None:
            # ロゴのシルエットを現在の色で塗ってマスク合成 (色替えバウンス)
            tile = Image.new("RGB", (self.lw, self.lh), COLORS[self.ci])
            img.paste(tile, (int(self.x), int(self.y)), self.logo_mask)
        else:
            ImageDraw.Draw(img).text((self.x - self._tox, self.y - self._toy),
                                     "DVD", font=self.font, fill=COLORS[self.ci])
        self.display.buffer = img
        self.display.display()

    # ---- 跳ねる地図タイル段階 ----
    def _render_tile_image(self, style, lon, lat, stop):
        """mbgl-render で 1 ビュー描画し、中央を正方形クロップした PIL Image を返す。
        停止要求 (stop) が立ったら即中断して None を返す。"""
        fd, path = tempfile.mkstemp(suffix=".png", prefix="saver-tile-")
        os.close(fd)
        try:
            proc = subprocess.Popen(
                [TILE_RENDER, "-s", style, "-o", path, "-c", TILE_MBGL_CACHE,
                 "-z", str(TILE_ZOOM), "-x", f"{lon:.6f}", "-y", f"{lat:.6f}"],
                env=TILE_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.time() + 60
            while proc.poll() is None:
                if stop.is_set():
                    proc.kill()
                    return None
                if time.time() > deadline:
                    proc.kill()
                    break
                time.sleep(0.1)
            if not os.path.exists(path) or os.path.getsize(path) < 1000:
                return None
            src = Image.open(path).convert("RGB")
            sw, sh = src.size
            side = min(sw, sh)
            left, top = (sw - side) // 2, (sh - side) // 2
            return src.crop((left, top, left + side, top + side)).resize((TILE_SIZE, TILE_SIZE))
        except Exception:
            return None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _start_tile_prerender(self):
        """全 (スタイル×リージョン) をシャッフルし先頭 N 個を背景スレッドでプリレンダ。"""
        self._stop_tiles()  # 念のため前回スレッドを止める
        stop = threading.Event()  # この起動専用の停止フラグ (worker がローカル束縛で保持)
        self._tile_stop = stop
        self.tiles = []
        self.tile_show = 0
        combos = [(s, r) for s in range(len(TILE_STYLES)) for r in range(len(TILE_REGIONS))]
        random.shuffle(combos)
        combos = combos[:max(1, TILE_CACHE_N)]

        def worker():
            for si, ri in combos:
                if stop.is_set():
                    return
                lat, lon = TILE_REGIONS[ri]
                img = self._render_tile_image(TILE_STYLES[si], lon, lat, stop)
                if stop.is_set():
                    return
                if img is not None:
                    self.tiles.append(img)
                    print(f"[screensaver] tile cached {len(self.tiles)}/{len(combos)} "
                          f"style#{si} region={lat:.3f},{lon:.3f} z{TILE_ZOOM:g}", flush=True)
            print(f"[screensaver] tile prerender complete ({len(self.tiles)})", flush=True)

        self._tile_thread = threading.Thread(target=worker, daemon=True)
        self._tile_thread.start()

    def _stop_tiles(self):
        """背景プリレンダスレッドへ停止要求を出し、キャッシュを破棄する。"""
        try:
            self._tile_stop.set()
        except Exception:
            pass
        self.tiles = []
        self.tile_show = 0

    def _animate_tile(self):
        """正方形タイルを 1 フレーム跳ねさせる。壁にぶつかる度にキャッシュ画像を差し替え。
        キャッシュが空の間は色つきプレースホルダ正方形を表示する。"""
        now = time.time()
        if now < self._next_frame:
            return
        self._next_frame = now + FRAME_INTERVAL

        self.tx += self.tvx
        self.ty += self.tvy
        bounced = False
        if self.tx <= 0:
            self.tx, self.tvx, bounced = 0.0, abs(self.tvx), True
        elif self.tx + TILE_SIZE >= WIDTH:
            self.tx, self.tvx, bounced = WIDTH - TILE_SIZE, -abs(self.tvx), True
        if self.ty <= 0:
            self.ty, self.tvy, bounced = 0.0, abs(self.tvy), True
        elif self.ty + TILE_SIZE >= HEIGHT:
            self.ty, self.tvy, bounced = HEIGHT - TILE_SIZE, -abs(self.tvy), True
        if bounced:
            self.tci = (self.tci + 1) % len(COLORS)
            if self.tiles:
                self.tile_show = (self.tile_show + 1) % len(self.tiles)

        img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        tiles = self.tiles  # ローカル束縛 (背景スレッドの append と競合しても安全)
        if tiles:
            img.paste(tiles[self.tile_show % len(tiles)], (int(self.tx), int(self.ty)))
        else:
            # まだ 1 枚もできていない: 色つき正方形をプレースホルダ表示
            ph = Image.new("RGB", (TILE_SIZE, TILE_SIZE), COLORS[self.tci])
            img.paste(ph, (int(self.tx), int(self.ty)))
        self.display.buffer = img
        self.display.display()
