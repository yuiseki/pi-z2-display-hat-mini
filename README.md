# pi-z2-display-hat-mini

Raspberry Pi Zero 2 W + Pimoroni Display HAT Mini を、キーボードの有無に応じて「地図ビューア」と「ターミナル」を自動で出し分け、相互に切り替えられるようにする常駐サービス。

詳細仕様は [docs/spec.md](docs/spec.md) を参照。

## 挙動

- ディスプレイ無し: 何もしない (常駐し再検出を継続)
- ディスプレイのみ: 地図を表示
- ディスプレイ + キーボード: ターミナルを表示
- ターミナルで `pi-map` (または `pi-maps`) 実行: 地図へ切替
- 地図表示中 + キーボード有り: HAT の A+X 同時押し、または Ctrl+C を短時間に連続2回でターミナルへ切替
- ターミナル表示中にキーボード切断: 地図へ自動移行
- キーボードはあとから接続しても上記の切替が有効になる
- 5 分無操作: スクリーンセーバー (跳ね回る DVD ロゴ) を表示
- 10 分無操作: 跳ね回る地図タイル (広域・正方形。数枚をプリレンダしてキャッシュし、壁にぶつかる度に切替)
- 消灯: バッテリー駆動は 30 分、給電中 (battery_power_plugged) は 12 時間。ボタン/キー入力で即復帰 (その入力は復帰のみに消費)

## 構成

- `supervisor.py` : モード決定・子アプリの起動/停止/切替・ディスプレイ/キーボード監視 (systemd 常駐)
- `apps/map.py` : 地図ビューア (maplibre-native + llvmpipe + PMTiles、タイルキャッシュ/プリレンダ、給電連動)
- `apps/terminal.py` : ターミナル (PTY + bash + pyte + xkbcommon、USB/BT キーボード対応)
- `apps/screensaver.py` : アイドル時の DVD ロゴ → 跳ねる地図タイル → 消灯 (map/terminal 共有)
- `bin/pi-map`, `bin/pi-maps` : 地図モードへの切替要求コマンド
- `bin/pi-screensaver`, `bin/pi-saver` : スクリーンセーバーの手動トリガ (`saver`/`tile`/`off`/`wake`。動作確認用)
- `systemd/pi-display.service` : 起動時自動起動の unit
- IPC: `/tmp/pi-display/request` (切替要求ファイル)

## 前提 (デバイス側)

- venv `~/.venvs/displayhatmini` (依存は `requirements.txt`)
- `~/mbgl-render` (maplibre-native を EGL+llvmpipe でビルドした静的描画バイナリ)
- `~/mbgl-libs` (cross-distro 実行用に bundle した共有ライブラリ)
- 任意: PiSugar (`~/.local/bin/pi-power` で給電状態取得)

`mbgl-render` / `mbgl-libs` のビルド・配置手順は `docs/NOTES.md` (今後追記) を参照。

## 導入

リポジトリを `~/pi-z2-display-hat-mini` に配置して:

```bash
bash install.sh
```

ログ: `journalctl -u pi-display -f`
