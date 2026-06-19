#!/usr/bin/env python3
"""アイドル時のスクリーンセーバー (bouncing DVD logo) と消灯を司る共有ヘルパ。

map.py / terminal.py の両アプリが使う。HAT/画面の占有は呼び出し側 (子アプリ) に
従う設計で、supervisor は一切触れない (docs/spec.md 8 の排他方針)。

状態:
  ACTIVE : 通常表示。最後の操作からの経過時間で遷移する。
  SAVER  : 5 分無操作。画面いっぱいを跳ね回る "DVD" ロゴを描画 (壁で色変化)。
  OFF    : 15 分無操作。バックライト消灯・描画停止。

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
import time

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 320, 240

# pi-screensaver / pi-saver が書く制御ファイル (手動トリガ / 動作確認用)
#   saver|on -> 直ちに SAVER / off -> 直ちに OFF / wake -> 通常表示へ復帰
CONTROL = "/tmp/pi-display/saver"

SAVER_AFTER = int(os.environ.get("PI_SAVER_AFTER", 5 * 60))    # 5 分でスクリーンセーバー
OFF_AFTER = int(os.environ.get("PI_OFF_AFTER", 15 * 60))       # 15 分でディスプレイ消灯
FRAME_INTERVAL = 0.07                                          # ロゴ更新間隔 (約 14fps)

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
    ACTIVE, SAVER, OFF = "active", "saver", "off"

    def __init__(self, display, on_wake=None, brightness=1.0,
                 saver_after=SAVER_AFTER, off_after=OFF_AFTER):
        self.display = display
        self.on_wake = on_wake          # 復帰時にホストへ通常画面の再描画を促すコールバック
        self.brightness = brightness    # 通常時 / スクリーンセーバー時のバックライト
        self.saver_after = saver_after
        self.off_after = off_after
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

    @property
    def awake(self):
        return self.state == self.ACTIVE

    def note_activity(self):
        """入力検出時に呼ぶ。アイドルタイマを更新し、必要なら通常表示へ復帰する。"""
        self.last = time.time()
        if self.state != self.ACTIVE:
            self.state = self.ACTIVE
            self.display.set_backlight(self.brightness)
            print("[screensaver] -> ACTIVE", flush=True)
            if self.on_wake:
                try:
                    self.on_wake()
                except Exception:
                    pass

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
        elif cmd == "off":
            self.last = time.time() - self.off_after    # 直ちに OFF 相当へ

        idle = time.time() - self.last
        if idle >= self.off_after:
            if self.state != self.OFF:
                self.state = self.OFF
                self._blank()
                self.display.set_backlight(0.0)
                print("[screensaver] -> OFF", flush=True)
            return self.state
        if idle >= self.saver_after:
            if self.state != self.SAVER:
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
