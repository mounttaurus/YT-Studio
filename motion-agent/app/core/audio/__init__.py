"""音の層（エンジンに依存しない）。設計: Docs/MOTION_UNIFIED_PLAN.md §9。

- sfx_synth: 効果音の手続き合成（numpy だけ・同じ引数なら WAV がバイト一致＝M2）
- mix:       打点の並び → 1本の音の道・無音の mp4 との結合（ffmpeg）
"""
