# pi-z2-display-hat-mini 仕様書

Raspberry Pi Zero 2 W + Pimoroni Display HAT Mini を、USB/Bluetooth キーボードの有無に応じて「地図ビューア」と「ターミナル」を自動で出し分け、相互に切り替えられるようにするサービスの仕様。

## 1. 目的

1台の Pi Zero 2 W + Display HAT Mini を、電源を入れるだけで使える地図デバイスにする。キーボードがあるときはターミナルとしても使え、地図とターミナルをユーザー操作で行き来できる。常駐サービス (systemd) として起動時から自動で動く。

## 2. ハードウェア前提

- Raspberry Pi Zero 2 W (SoC: BCM2710A1 / VideoCore IV, GLES2 まで)
- Pimoroni Display HAT Mini (ST7789 320x240, SPI 接続。ボタン A/B/X/Y = GPIO 5/6/16/24、RGB LED = GPIO 17/27/22、バックライト = GPIO 13、DC = GPIO 9、CS = SPI0 CE1)
- 任意: USB または Bluetooth キーボード (HID)
- 任意: PiSugar 3 (バッテリー。`pi-power` で給電状態を取得)

ソフトウェア前提 (デバイス側に既設):

- `~/.venvs/displayhatmini` (Python venv。`displayhatmini`, `pyte`, `evdev`, `xkbcommon`, `Pillow` を導入済み)
- `~/mbgl-render` (maplibre-native を EGL+llvmpipe でビルドした静的描画バイナリ) と `~/mbgl-libs` (cross-distro 実行用に bundle した共有ライブラリ)
- VideoCore IV は GLES2 までのため maplibre 描画は llvmpipe (ソフトウェア GLES3) で行う。HW アクセラは不可 (別途調査済み)

## 3. 要件 (諸元)

ユーザー提示の諸元を原文の意図のまま整理する。

1. 起動直後: ディスプレイの存在を確認する。
2. ディスプレイが無い場合: 何もしない。
3. ディスプレイだけがある場合: まず地図を表示する。
4. ディスプレイもキーボードもある場合: まずターミナルを表示する。
5. ターミナルで `pi-map` または `pi-maps` を実行したら地図に切り替わる。
6. 地図を表示中でキーボードがある場合: Display HAT Mini の A+X 同時押しでターミナルに切り替わる。
7. 地図を表示中でキーボードがある場合: キーボードの Ctrl+C 2回でもターミナルに切り替わる。
8. 最初にディスプレイだけがあり途中でキーボードが繋がった場合も、上記 (6)(7) の切り替え挙動が有効になる。

## 4. 状態遷移

### 4.1 モード

- `STANDBY`: ディスプレイ無し。何もしない (画面・入力に触れない)。
- `MAP`: 地図ビューア (`map_pager`) が前面。HAT の SPI/GPIO/LED/ボタンを占有。
- `TERMINAL`: ターミナル (`display_console`) が前面。HAT に加えてキーボードを grab。

### 4.2 起動時のモード決定 (ディスプレイ有りのとき)

- キーボード有り → `TERMINAL`
- キーボード無し → `MAP`

### 4.3 遷移

| 現モード | トリガ | 次モード | 条件 |
|---|---|---|---|
| TERMINAL | `pi-map` / `pi-maps` 実行 | MAP | 常時 |
| TERMINAL | キーボード切断 | MAP | 入力手段を失うため自動移行 |
| MAP | A+X 同時押し | TERMINAL | キーボード有りのときのみ |
| MAP | 短時間に Ctrl+C を連続2回 | TERMINAL | キーボード有りのときのみ |
| STANDBY | ディスプレイ検出 | MAP または TERMINAL | 4.2 に従う |
| 任意 | ディスプレイ喪失 | STANDBY | 通常は発生しない |

### 4.4 状態遷移図 (概念)

```
            ディスプレイ無し
        ┌────────── STANDBY ──────────┐
        │  (何もしない。検出を継続)      │
        │                              │
   ディスプレイ有り               ディスプレイ有り
   かつ KB 有り                   かつ KB 無し
        │                              │
        ▼                              ▼
   ┌─────────┐   pi-map / pi-maps   ┌─────────┐
   │TERMINAL │ ───────────────────▶ │   MAP   │
   │         │ ◀─────────────────── │         │
   └─────────┘  A+X / Ctrl+C x2     └─────────┘
                 (KB 有りのときのみ)
```

キーボードのホットプラグ (要件8): MAP 中に KB が接続/切断されると、MAP→TERMINAL の切替トリガ (A+X, Ctrl+C 連続2回) が動的に有効/無効になる。MAP 中に KB を接続しても自動的に TERMINAL へは移行しない (切替トリガが有効になるだけ)。一方 TERMINAL 中に KB が切断された場合は、入力手段を失うため自動的に MAP へ移行する。

## 5. アーキテクチャ

### 5.1 方針

「スーパバイザ + 子アプリ2種 + IPC」構成。既存の動作実績がある `display_console.py` (ターミナル) と `map_pager.py` (地図) を子アプリとして再利用し、スーパバイザが状態に応じて起動/停止/切替する。HAT/GPIO/LED/ボタン/キーボードは同時に1プロセスのみが保有する (排他)。

### 5.2 コンポーネント

- `supervisor` (常駐 / systemd): モード決定、子アプリの起動と停止、ディスプレイとキーボードの存在監視 (ホットプラグ)、IPC 要求の監視。実際の描画・入力処理は行わず、オーケストレーションに徹する。
- `apps/terminal` (= 既存 `display_console.py` 相当): PTY + bash + pyte + xkbcommon + evdev。キーボードを grab し HAT に端末画面を描画。bash は `apps/term.bashrc` を rcfile として起動し、`~/.bashrc` を読み込んだ上で小さな画面向けにプロンプト表示名を `yui@pi-z2-2` に差し替える (実ユーザ/ホスト名は不変)。起動時の pwd はホーム (`~`)。色指定の無い文字はソフトな白で描画する (純緑だと全体が緑すぎるため)。
- `apps/map` (= 既存 `map_pager.py` 相当): `mbgl-render` (llvmpipe) で地図タイルを描画しキャッシュ/プリレンダ。HAT ボタンで操作。MAP モードでは追加で「A+X 同時押し」と「Ctrl+C 2回 (KB 有り時)」を検出して切替要求を出す。
- `bin/pi-map`, `bin/pi-maps`: PATH に置く小さな実行スクリプト。制御チャネルに「MAP へ切替」要求を書き込むだけ。
- `systemd/pi-display.service`: スーパバイザを起動時から常駐させる unit。

### 5.3 IPC (制御チャネル)

`/tmp/pi-display/request` を要求ファイルとする (tmpfs)。書き込み主体と内容:

- `pi-map` / `pi-maps` → `map`
- MAP アプリが A+X または Ctrl+C x2 を検出 → `terminal`

スクリーンセーバーの手動制御は別ファイル `/tmp/pi-display/saver` を使う (11.5 参照):

- `pi-screensaver` / `pi-saver` → `saver` / `off` / `wake`

スーパバイザはこのファイルを監視 (ポーリング、約 0.3 秒間隔、または inotify) し、内容が変化したら現在の子アプリを停止して要求モードの子アプリを起動する。ファイルは処理後にクリアする。UNIX ドメインソケット/FIFO でも代替可能だが、要求ファイル方式が最も単純で堅牢。

## 6. 検出ロジック

### 6.1 ディスプレイ検出

Display HAT Mini は HAT EEPROM (ID) を持たないため、接続を厳密に検出できない。以下のヒューリスティックで代替する。

- `/dev/spidev0.1` が存在する (SPI が有効) ことを前提とする。
- `DisplayHATMini` の初期化が例外なく成功することを「ディスプレイ有り」とみなす。
- SPI が無効、または初期化に失敗した場合は `STANDBY` (何もしない)。

注意: SPI は HAT 不在でも書き込みエラーにならないため、パネルの物理有無までは判定できない。実運用では「SPI が有効なら HAT は装着されている」という前提で良い。

### 6.2 キーボード検出

`evdev` で全 `/dev/input/event*` を走査し、`KEY_A` と `KEY_SPACE` を持つデバイスを「キーボード」とみなす (USB/BT どちらでも)。

- USB/BT の区別は不要。`KEY_A`+`KEY_SPACE` を持つ実キーボードのみ対象 (HDMI CEC やマウスを除外)。
- ホットプラグ追従のため約 1 秒間隔でポーリングする (既存の `acquire_keyboard` と同方式)。
- BT キーボードは LE Random アドレスのため `bluetoothctl scan` の一覧に出にくいが、ペアリング/ボンディング済みであれば自動再接続し evdev に現れる (別ドキュメントの BLE ペアリング手順を参照)。

## 7. モード切替トリガ詳細

### 7.1 TERMINAL → MAP: `pi-map` / `pi-maps`

- ユーザーがターミナル内の bash で `pi-map` (または別名 `pi-maps`) を実行する。
- このスクリプトは `/tmp/pi-display/request` に `map` を書き込んで即終了する。
- スーパバイザが要求を検出し、ターミナル子プロセスを停止 (kill) してから MAP アプリを起動する。
- ターミナル側アプリ自身は要求を解釈しない (スーパバイザが停止を担う)。

### 7.2 MAP → TERMINAL: A+X 同時押し

- MAP アプリはナビ用に常時ボタンを読んでいる。`BUTTON_A` と `BUTTON_X` が同時に押下 (両方 LOW) されている状態を検出する。
- 単押し/ダブル/長押しのジェスチャ判定より A+X 同時押し判定を優先する (誤爆防止のため数十 ms の同時保持を確認)。
- 検出したら `/tmp/pi-display/request` に `terminal` を書き込み、自身は終了 (リソース解放) する。
- キーボードが無いときは TERMINAL へ移行しても無意味なため、A+X 切替はキーボード有りのときだけ有効にする。

### 7.3 MAP → TERMINAL: Ctrl+C 2回

- MAP モードにはシェルが存在しないため、これは本物の SIGINT ではない。MAP アプリがキーボード有りのとき evdev からキー入力を読み、`Ctrl` 押下中の `C` 押下を検出する擬似実装とする。
- 脱出は Claude Code と同じく「短時間に連続2回」のときだけ行う。1回目の Ctrl+C から一定時間 (例: 1.5 秒) 以内に2回目の Ctrl+C が来た場合のみ TERMINAL へ切替。単発の Ctrl+C では切り替えない (誤爆防止)。
- 検出したら A+X と同様に `terminal` を要求して終了する。
- MAP アプリはこのためにキーボードを読む (必要なら grab する)。キーボードが無いときはこの監視を行わない。

### 7.4 画面フィードバック (UI)

ユーザーが状態を把握できるよう、MAP 画面に以下を表示する (フォントは DejaVu Sans Mono のため英数字のみ)。

- 上部 HUD にキーボード認識中は `KYBD` を表示 (A+X / Ctrl+C の切替トリガが有効な合図)。キーボードの抜き差しで自動更新。
- Ctrl+C の1回目を受けたら、下部の操作凡例に被せて赤帯で `Press Ctrl+C again to exit` を表示 (Claude Code と同様)。一定時間内に2回目が来なければ消える。
- A+X または Ctrl+C 連続2回で実際に切り替わる際は、直前の地図に被せて `Exiting...` を短時間表示してから TERMINAL へ移行する。

## 8. リソース排他

HAT の SPI/GPIO/LED/ボタン、および (TERMINAL の) キーボード grab は、同時に1プロセスだけが保有する。スーパバイザは以下を守る。

- 切替時は必ず「現アプリを停止 (SIGTERM、必要なら SIGKILL) し、プロセス終了 (= カーネルが fd を解放) を待ってから」次アプリを起動する。
- 起動の取りこぼし防止に短い settle 待ち (例: 0.5〜1 秒) を挟む。
- LED/バックライトは各アプリが起動時に初期化し、終了時はカーネルの fd 解放に委ねる (rppal/RPi.GPIO の drop 時挙動に注意)。

本セッションで、kill → 再起動による HAT/キーボードのハンドオフが確実に動くことは実証済み。

## 9. systemd サービス

- system サービスとして `User=yuiseki` で起動 (グループ: `spi`, `gpio`, `i2c`, `input`, `video`, `render`)。
- `Restart=always`、`After=multi-user.target` 程度。
- 実行コマンドはスーパバイザ (venv の python で起動)。
- 依存: `~/.venvs/displayhatmini`, `~/mbgl-render`, `~/mbgl-libs`, タイルキャッシュ用 `~/map-cache`、PiSugar 用 `~/.local/bin/pi-power` (任意)。
- ネットワーク (PMTiles/グリフ取得) が必要なため、初回描画はオンライン前提。キャッシュ後はオフラインでも既訪範囲は表示可。

## 10. 実現可能性評価

### 10.1 実証済み (再利用可能)

- ターミナル: PTY + bash + pyte (VT100、htop/vim 可) + xkbcommon (JIS) + evdev、USB/BT キーボードのホットプラグ対応。
- 地図ビューア: `mbgl-render` (llvmpipe, PMTiles 対応) + 0.5タイル離散ナビ + 強キャッシュ + プリレンダ + 給電連動 + LED フィードバック。
- HAT への画像表示、ボタン読取 (A/B/X/Y)、RGB LED 制御。
- キーボードの検出と抜き差し追従。

### 10.2 新規実装 (いずれも容易)

- スーパバイザのメインループ (モード決定、子の起動/停止、要求とホットプラグの監視)。
- IPC (要求ファイル `/tmp/pi-display/request`)。
- `pi-map` / `pi-maps` スクリプト。
- MAP アプリへの A+X 同時押し検出と Ctrl+C x2 検出 (KB 有り時) の追加、および切替要求の書き込み。
- systemd unit と install スクリプト。

### 10.3 リスク・制約

- ディスプレイ検出はヒューリスティック (HAT に ID が無い)。SPI 有効を前提とする。
- 地図描画は llvmpipe (CPU) のため1描画に数秒。素早い反映は前提にしない (諸元と整合)。
- 512MB RAM。ターミナルと地図を同時起動しない設計なので、単独動作なら収まる (実証済み)。
- 切替時に一瞬画面が消える/再初期化が入るのは許容する。

## 11. 確定事項 (オープン事項の回答)

当初のオープン事項は以下のとおり確定した。

1. TERMINAL 表示中にキーボードが切断されたら、入力手段を失うため自動的に MAP へ移行する。
2. MAP 表示中にキーボードが接続されても自動では TERMINAL へ移行しない。切替トリガ (A+X / Ctrl+C 連続2回) が有効になるだけ。
3. ディスプレイ検出はヒューリスティック (SPI 有効 + `DisplayHATMini` init 成否) で良い。
4. MAP 中の Ctrl+C は evdev からの擬似検出で良い。Claude Code と同様、短時間 (例: 1.5 秒以内) に連続2回受け取ったときだけ TERMINAL へ脱出する。単発では切り替えない。
5. STANDBY (ディスプレイ無し) でもサービスは常駐し、ディスプレイとキーボードの再検出を継続する。

## 11.5 スクリーンセーバー / 消灯 (アイドル制御)

無操作が続いたときに段階的に画面を落とす。MAP / TERMINAL の両モードで有効。

- 5 分無操作: スクリーンセーバー発動。画面いっぱいを跳ね回る DVD ロゴ (実物の DVD-Video ロゴ画像 `assets/dvd-logo.png`) を描画し、壁にぶつかる度に色が変わる (bouncing DVD logo)。ロゴはアルファをシルエットマスクとして現在色で塗る。画像読込に失敗した場合は "DVD" テキストにフォールバック。バックライトは点灯のまま。
- 15 分無操作: ディスプレイ消灯 (バックライト 0、描画停止)。
- 復帰: HAT ボタン (MAP) / キー入力 (TERMINAL) のいずれかで通常表示へ即復帰。復帰のトリガとなった入力は「起こすだけ」で消費し、地図移動・モード切替・文字入力には使わない (誤操作防止)。

活動 (アイドルタイマのリセット) の定義:

- MAP: HAT ボタン押下、またはキー入力。背景プリレンダや電源状態の変化は活動に含めない (CPU が動いていても無操作なら発動する)。
- TERMINAL: キー入力のみ。PTY の画面出力 (`htop` / `tail -f` 等) は活動に含めない → 打鍵が無ければ 5 分で発動する。

実装:

- 共有ヘルパ `apps/screensaver.py` の `Screensaver` クラスが状態 (ACTIVE / SAVER / OFF)・バックライト・ロゴ描画を司る。HAT/画面の占有は呼び出し側 (子アプリ) に従い、supervisor は関与しない (8 の排他方針)。
- 子アプリは入力検出時に `note_activity()`、ループ毎に `tick()` を呼ぶ。SAVER 中は `tick()` がロゴを 1 フレーム進める (約 14fps、`mbgl-render` は使わず Pillow 直描画)。
- MAP は SAVER/OFF 中はプリレンダを止める (消費電力と LED 点滅の抑制)。
- タイマは環境変数 `PI_SAVER_AFTER` / `PI_OFF_AFTER` (秒) で上書き可能 (動作確認・調整用)。
- 手動トリガ: `pi-screensaver` (別名 `pi-saver`) で待たずに発動を確認できる。制御ファイル `/tmp/pi-display/saver` に `saver` / `off` / `wake` を書き、`Screensaver.tick()` が読み取って即座に状態遷移する。状態遷移は `[screensaver] -> SAVER/OFF/ACTIVE` として stdout (journal) に出る。
  - `pi-screensaver` または `pi-screensaver saver` → 直ちにスクリーンセーバー (10 分後に消灯)
  - `pi-screensaver off` → 直ちに消灯
  - `pi-screensaver wake` → 通常表示へ復帰

## 12. ディレクトリ構成 (案)

隣接プロジェクト (pi-co2-logger / pi5-e-paper-hat) の流儀に合わせる。

```
pi-z2-display-hat-mini/
├── README.md
├── requirements.txt          # displayhatmini, pyte, evdev, xkbcommon, Pillow
├── supervisor.py             # スーパバイザ (systemd で起動)
├── apps/
│   ├── terminal.py           # 既存 display_console.py を整理して移植
│   ├── map.py                # 既存 map_pager.py を整理して移植 (+A+X/Ctrl+C)
│   └── screensaver.py        # アイドル時の DVD ロゴ + 消灯 (両アプリ共有)
├── assets/
│   └── dvd-logo.png          # 跳ね回る DVD-Video ロゴ (色替え用マスクに使用)
├── bin/
│   ├── pi-map                # /tmp/pi-display/request に map を書く
│   └── pi-maps               # pi-map への別名
├── systemd/
│   └── pi-display.service
├── install.sh                # venv/依存/PATH/サービス導入
├── docs/
│   ├── spec.md               # 本書
│   ├── NOTES.md              # 設定の勘所 (EGL/llvmpipe, BLE ペアリング, cross-distro 等)
│   └── ONBOARDING.md
└── tests/
```

注: `mbgl-render` と `mbgl-libs` は大きなビルド成果物のためリポジトリには含めず、別途デバイスへ配置する前提とする (NOTES.md にビルド/配置手順を記載)。

## 13. 実装状況

初版を実装し、pi-z2 上で systemd サービスとして稼働・動作確認済み。

- [x] 既存 `display_console.py` / `map_pager.py` を `apps/terminal.py` / `apps/map.py` へ移植。
- [x] MAP アプリに A+X 同時押しと Ctrl+C 連続2回の検出 (KB 有り時) を追加し切替要求を書く。`KYBD` 表示・Ctrl+C ヒント・`Exiting...` の画面フィードバックも実装。
- [x] `supervisor.py` を実装 (モード決定 / 子の起動停止 / 要求・ホットプラグ監視 / STANDBY 常駐)。
- [x] `bin/pi-map` / `bin/pi-maps` と `systemd/pi-display.service`、`install.sh` を作成。
- [x] デバイスへ配置し、起動時モード決定 / pi-map / 相互切替 / KB 無し時のバウンス / KYBD 表示を確認。A+X・Ctrl+C 連続2回・物理 KB 抜き差しは実機操作で確認。

挙動は11章で確定済み。`mbgl-render` / `mbgl-libs` のビルドと配置手順は今後 `docs/NOTES.md` に追記予定。
