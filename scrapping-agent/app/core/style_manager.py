"""
画像生成スタイル定義の管理。

STYLES は従来 comfy_client.py にハードコードされていたが、
`shared/imagegen/styles.json` に外出しし、ユーザーが編集・追加できるようにした。
ファイルが無ければデフォルトを書き出す（初回起動時の自動セットアップ）。

スタイル構造:
  checkpoint: ComfyUIのcheckpointファイル名
  prefix:     プロンプト接頭辞
  negative:   ネガティブプロンプト
  loras:      [{"name": "xxx.safetensors", "strength": 0.8}, ...]（任意）
  usage:      "footage" | "character" | "both"（UIでの表示振り分け用）
  steps/cfg/sampler/scheduler: サンプラー設定（任意。省略時 22 / 7.0 / dpmpp_2m / karras）
    例) LCM-LoRA高速化スタイル: lorasにlcm-lora-sdv1-5を追加し
        "sampler": "lcm", "scheduler": "sgm_uniform", "cfg": 1.5, "steps": 6
"""
import json
import os
from pathlib import Path

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
STYLES_FILE = SHARED_DIR / "imagegen" / "styles.json"

_NEG_COMMON = "text, watermark, lowres, bad anatomy, worst quality, low quality"

DEFAULT_STYLES = {
    "realistic": {
        "checkpoint": "realistic_vision_v6_fp16.safetensors",
        "prefix": "RAW photo, realistic, cinematic lighting, high detail, ",
        "negative": f"cartoon, anime, illustration, painting, drawing, {_NEG_COMMON}",
        "loras": [],
        "usage": "both",
    },
    "anime": {
        "checkpoint": "counterfeit_v2.5_fp16.safetensors",
        "prefix": "masterpiece, best quality, anime style illustration, ",
        "negative": f"photo, realistic, {_NEG_COMMON}, nsfw",
        "loras": [],
        "usage": "footage",
    },
    "lineart": {
        "checkpoint": "counterfeit_v2.5_fp16.safetensors",
        "prefix": "monochrome, lineart, line art, clean lines, white background, "
                  "ink sketch, masterpiece, best quality, ",
        "negative": f"color, photo, realistic, {_NEG_COMMON}, nsfw",
        "loras": [],
        "usage": "footage",
    },
    # ===== キャラクター生成用スタイル（MVP 3種: comic / realistic / deformed）=====
    "comic": {
        "checkpoint": "counterfeit_v2.5_fp16.safetensors",
        "prefix": "masterpiece, best quality, comic style illustration, bold clean outlines, "
                  "flat colors, cel shading, expressive face, ",
        "negative": f"photo, realistic, 3d render, {_NEG_COMMON}, nsfw",
        "loras": [],
        "usage": "character",
    },
    "deformed": {
        "checkpoint": "counterfeit_v2.5_fp16.safetensors",
        "prefix": "masterpiece, best quality, chibi, super deformed, cute, simple background, "
                  "full body, big head, small body, ",
        "negative": f"photo, realistic, tall body, realistic proportions, {_NEG_COMMON}, nsfw",
        "loras": [],
        "usage": "character",
    },
    "kamishibai": {
        "checkpoint": "counterfeit_v2.5_fp16.safetensors",
        "prefix": "2D anime style illustration, soft cel shading, clean bold outlines, "
                  "flat pastel colors, gentle lighting, simple webtoon panel, expressive face, ",
        "negative": f"photo, realistic, 3d render, harsh shadows, {_NEG_COMMON}, nsfw",
        "loras": [],
        "usage": "character",
    },
}


def load_styles() -> dict:
    """styles.jsonを読む。無ければデフォルトを書き出して返す。壊れていればデフォルトを返す。

    既存ファイルに存在しないキーは DEFAULT_STYLES から非破壊的に補完して再保存する
    （ユーザーが削除した既存スタイルの復活はしない＝新規キーのみ追加）。
    """
    if not STYLES_FILE.exists():
        save_styles(DEFAULT_STYLES)
        return dict(DEFAULT_STYLES)
    try:
        data = json.loads(STYLES_FILE.read_text(encoding="utf-8"))
        changed = False
        for key, style in DEFAULT_STYLES.items():
            if key not in data:
                data[key] = style
                changed = True
        if changed:
            save_styles(data)
        return data
    except Exception:
        return dict(DEFAULT_STYLES)


def save_styles(styles: dict) -> None:
    STYLES_FILE.parent.mkdir(parents=True, exist_ok=True)
    STYLES_FILE.write_text(
        json.dumps(styles, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_style(name: str) -> dict | None:
    return load_styles().get(name)
