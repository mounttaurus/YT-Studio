"""
irodori と OmniVoice は同一GPU(4GB)を共有する。Phase1実測では両方ロード済みで
空きVRAM 104MBまで落ち込むため、同時並行推論はOOMの可能性がある
（Docs/08_i18n.md §4b）。プロセス全体で1本の推論ロックを共有し、
両エンジンの実推論呼び出しをこのロックで直列化する。
"""
import asyncio

INFERENCE_LOCK = asyncio.Lock()
