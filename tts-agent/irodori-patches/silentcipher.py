"""silentcipher を import 不能にするスタブ（YT-Studio）。

Irodori-TTS-Server（irodori_tts.watermark）は、生成音声に SilentCipher の透かしを常時入れる。
設定・環境変数・リクエスト引数で無効化する手段は無いが、`import silentcipher` が ImportError なら
ライブラリは警告を出して「透かしなしで続行」する（公式コードの経路）。
このファイルを PYTHONPATH の先頭（コンテナ内 /opt/irodori-patches）に置いて、その経路に入れる。

効果（実測 2026-10-12・RTX 3050 Ti 4GB・MeanFlow での比較）:
  - 所要時間: 温まった状態で約 1/3（RTF 0.49 → 0.14）。透かし工程が処理時間の約4割を占めていた
  - 音: 同一 seed の出力差は -48.5dB（設計値 47dB）・長さとサンプルレートは同一

戻す時は docker-compose.yml の irodori-tts-server から PYTHONPATH とこのフォルダのマウントを外す。
"""
raise ImportError("silentcipher is disabled on purpose (tts-agent/irodori-patches)")
