"""
ComfyUI (imagegen-agent) クライアント — セクション単位のピンポイントAI画像生成。

素材サイトに無いもの（爆破シーン・歴史的場面の再現イメージ等）を狙って生成する用途。
検索ソース（pexels/pixabay）とは独立した /generate エンドポイントから呼ばれる。
candidate の download_url はコンテナ間URL、thumbnail_url はブラウザから見えるURLを使う。
"""
import asyncio
import json
import os
import random
import time
import urllib.parse

import httpx

from app.core import style_manager

COMFY_URL = os.getenv("COMFY_URL", "http://imagegen-agent:8188")
# ブラウザがサムネイルを直接取得するための外部URL（ホスト公開ポート）
COMFY_PUBLIC_URL = os.getenv("COMFY_PUBLIC_URL", "http://localhost:8188")

VAE_NAME = "vae-ft-mse-840000-ema-pruned.safetensors"

# 横長（YouTube向け）: SD1.5の限界内で16:9に近いサイズ
WIDTH, HEIGHT = 768, 432


def _build_workflow(
    prompt: str, style: dict, seed: int,
    init_image: str | None = None, denoise: float = 1.0,
    controlnet: dict | None = None,
) -> dict:
    """ComfyUI API形式のワークフロー。styleにlorasがあればLoraLoaderをチェーン挿入する。

    init_image指定時はimg2img: LoadImage→ImageScaleToTotalPixels→VAEEncodeでlatentを作り、
    denoise（0.4〜0.75目安）で参照画像のポーズ・構図・服装をどの程度保つか制御する。

    controlnet指定時は ControlNet で構図を固定したまま t2i（空latent・denoise=1.0）で
    イラスト化する。img2imgの濁りを避けて忠実に絵柄変換できる。前処理(preprocess)で2系統:
      - "canny": 参照画像のエッジ(Canny)だけを制御に渡す。輪郭は合うが中身(顔/陰影/配色)は
                 SDが描き起こす＝画風の自由度は高いが元画像への忠実度は低い。
      - "tile" : 参照画像“全体”(構図＋明暗＋ざっくり色)を制御に渡す(前処理なしの素通し)。
                 元画像の見た目を保ったまま画風だけ載せ替える＝原画像の忠実なイラスト化に最適。
    controlnet = {"name": <controlnetモデル名>, "image": <ComfyUI input名>, "strength": 0.8,
                  "preprocess": "tile"|"canny", "low": 0.4, "high": 0.8,
                  "start": 0.0, "end": 1.0}。Cannyは組み込みノードのみ使用（追加ノード不要）。
    start/end は適用区間(0〜1)。序盤/終盤をSDに開放すると画風が乗り、中盤固定で構造を保つ。
    """
    # LoRAチェーン: ckpt → lora0 → lora1 → ... の順にmodel/clipを繋ぎ替える
    model_ref, clip_ref = ["ckpt", 0], ["ckpt", 1]
    lora_nodes = {}
    for i, lora in enumerate(style.get("loras") or []):
        node_id = f"lora{i}"
        strength = float(lora.get("strength", 0.8))
        lora_nodes[node_id] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": model_ref, "clip": clip_ref,
                "lora_name": lora["name"],
                "strength_model": strength, "strength_clip": strength,
            },
        }
        model_ref, clip_ref = [node_id, 0], [node_id, 1]

    # ControlNet(Canny): 参照エッジで構図を固定し、t2i（空latent・denoise=1.0）で生成する。
    # img2imgとは併用せず、ControlNet優先（init_imageは無視）。
    control_nodes: dict = {}
    pos_ref, neg_ref = ["pos", 0], ["neg", 0]
    if controlnet:
        init_image = None  # ControlNetは空latentのt2iで動く
        denoise = 1.0
        control_nodes = {
            "cn_load": {
                "class_type": "LoadImage",
                "inputs": {"image": controlnet["image"]},
            },
            "cn_model": {
                "class_type": "ControlNetLoader",
                "inputs": {"control_net_name": controlnet["name"]},
            },
        }
        # 前処理: canny=エッジ抽出ノードを挟む / tile(既定)=素通し（画像全体を制御に渡す）
        if controlnet.get("preprocess") == "canny":
            control_nodes["cn_canny"] = {
                "class_type": "Canny",
                "inputs": {
                    "image": ["cn_load", 0],
                    "low_threshold": float(controlnet.get("low", 0.4)),
                    "high_threshold": float(controlnet.get("high", 0.8)),
                },
            }
            ctrl_image = ["cn_canny", 0]
        else:
            ctrl_image = ["cn_load", 0]
        control_nodes["cn_apply"] = {
            "class_type": "ControlNetApplyAdvanced",
            "inputs": {
                "positive": ["pos", 0], "negative": ["neg", 0],
                "control_net": ["cn_model", 0], "image": ctrl_image,
                "strength": float(controlnet.get("strength", 0.8)),
                "start_percent": max(0.0, min(float(controlnet.get("start", 0.0)), 1.0)),
                "end_percent": max(0.0, min(float(controlnet.get("end", 1.0)), 1.0)),
            },
        }
        pos_ref, neg_ref = ["cn_apply", 0], ["cn_apply", 1]

    if init_image:
        # 参照画像のアスペクト比を保ったまま総画素数を抑える（4GB VRAM対策、768x432相当）
        latent_nodes = {
            "load": {
                "class_type": "LoadImage",
                "inputs": {"image": init_image},
            },
            "scale": {
                "class_type": "ImageScaleToTotalPixels",
                "inputs": {
                    "image": ["load", 0], "upscale_method": "lanczos",
                    "megapixels": 0.33, "resolution_steps": 8,
                },
            },
            "latent": {
                "class_type": "VAEEncode",
                "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]},
            },
        }
    else:
        latent_nodes = {
            "latent": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1},
            },
        }
        denoise = 1.0

    return {
        "ckpt": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": style["checkpoint"]},
        },
        **lora_nodes,
        "vae": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": VAE_NAME},
        },
        **latent_nodes,
        **control_nodes,
        "pos": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": style["prefix"] + prompt, "clip": clip_ref},
        },
        "neg": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": style["negative"], "clip": clip_ref},
        },
        "sample": {
            "class_type": "KSampler",
            "inputs": {
                "model": model_ref,
                "positive": pos_ref,
                "negative": neg_ref,
                "latent_image": ["latent", 0],
                "seed": seed,
                # サンプラー設定はstyles.jsonで上書き可能（LCM-LoRA: sampler=lcm/cfg=1.5/steps=6 等）
                "steps": int(style.get("steps", 22)),
                "cfg": float(style.get("cfg", 7.0)),
                "sampler_name": style.get("sampler", "dpmpp_2m"),
                "scheduler": style.get("scheduler", "karras"),
                "denoise": denoise,
            },
        },
        "decode": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]},
        },
        "save": {
            "class_type": "SaveImage",
            # prefixを毎回ユニークにする: 完全に同一のワークフロー（固定seed再生成等）だと
            # ComfyUIが全ノードをキャッシュしSaveImageが実行されず、historyにoutputsが
            # 載らないままポーリングがタイムアウトするため
            "inputs": {
                "images": ["decode", 0],
                "filename_prefix": f"scrap_ai_{int(time.time() * 1000)}",
            },
        },
    }


async def _generate_one(
    client: httpx.AsyncClient, prompt: str, style: dict, seed: int | None = None,
    init_image: str | None = None, denoise: float = 1.0, controlnet: dict | None = None,
) -> dict | None:
    """1枚生成し、保存された画像情報（filename等）を返す。seed指定でキャラ一貫性を担保。"""
    if seed is None:
        seed = random.randint(0, 2**32 - 1)
    workflow = _build_workflow(prompt, style, seed, init_image=init_image, denoise=denoise, controlnet=controlnet)
    res = await client.post(f"{COMFY_URL}/prompt", json={"prompt": workflow})
    res.raise_for_status()
    prompt_id = res.json()["prompt_id"]

    # 完了をポーリング（lowvram環境ではモデルロード込みで時間がかかる）
    for _ in range(180):
        h = await client.get(f"{COMFY_URL}/history/{prompt_id}")
        h.raise_for_status()
        data = h.json()
        if prompt_id in data:
            entry = data[prompt_id]
            if entry.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"ComfyUI generation error: {json.dumps(entry['status'])[:300]}")
            outputs = entry.get("outputs", {})
            for node in outputs.values():
                for img in node.get("images", []):
                    return {"filename": img["filename"], "subfolder": img.get("subfolder", ""), "seed": seed}
        await asyncio.sleep(2.0)
    raise RuntimeError("ComfyUI generation timed out")


def _view_url(base: str, img: dict) -> str:
    q = urllib.parse.urlencode({
        "filename": img["filename"],
        "subfolder": img.get("subfolder", ""),
        "type": "output",
    })
    return f"{base}/view?{q}"


async def generate(
    prompt: str, count: int = 2, style_name: str = "realistic", seed: int | None = None,
) -> list[dict]:
    """プロンプトから画像をcount枚生成し、候補形式で返す（最大4枚）。

    seed指定時は seed, seed+1, ... で生成（キャラ一貫性・再現用）。
    """
    styles = style_manager.load_styles()
    style = styles.get(style_name) or styles.get("realistic") or style_manager.DEFAULT_STYLES["realistic"]
    return await generate_with_style(prompt, style, count=count, style_name=style_name, seed=seed)


async def generate_with_style(
    prompt: str, style: dict, count: int = 2, style_name: str = "custom", seed: int | None = None,
    init_image: str | None = None, denoise: float = 1.0, controlnet: dict | None = None,
) -> list[dict]:
    """スタイルdictを直接指定して生成する（キャラ生成のLoRA/seed/img2img/ControlNet参照用）。"""
    count = max(1, min(count, 4))
    results = []
    async with httpx.AsyncClient(timeout=600.0) as client:
        for i in range(count):
            img = await _generate_one(
                client, prompt, style,
                seed=None if seed is None else seed + i,
                init_image=init_image, denoise=denoise, controlnet=controlnet,
            )
            if img is None:
                continue
            results.append({
                "candidate_id": f"ai_{style_name}_{img['seed']}",
                "seed": img["seed"],
                "media_type": "photo",
                "source": "ai",
                "query": prompt,
                "original_url": "",
                "download_url": _view_url(COMFY_URL, img),
                "thumbnail_url": _view_url(COMFY_PUBLIC_URL, img),
                "duration_sec": 0.0,
                "resolution": f"{WIDTH}x{HEIGHT}",
                "photographer": f"AI ({style_name})",
                "license": "ai-generated",
            })
    return results


async def upload_image(data: bytes, name: str) -> str:
    """参照画像をComfyUIのinputフォルダへアップロードし、LoadImageで使える名前を返す。"""
    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.post(
            f"{COMFY_URL}/upload/image",
            files={"image": (name, data, "application/octet-stream")},
            data={"overwrite": "true"},
        )
        res.raise_for_status()
        return res.json().get("name", name)


async def list_models() -> dict:
    """ComfyUIが認識しているcheckpoint/LoRA/VAEの一覧を返す（object_info経由、volume追加不要）。"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        async def options(node: str, field: str) -> list[str]:
            try:
                res = await client.get(f"{COMFY_URL}/object_info/{node}")
                res.raise_for_status()
                opts = res.json()[node]["input"]["required"][field][0]
                return opts if isinstance(opts, list) else []
            except Exception:
                return []
        return {
            "checkpoints": await options("CheckpointLoaderSimple", "ckpt_name"),
            "loras": await options("LoraLoader", "lora_name"),
            "vaes": await options("VAELoader", "vae_name"),
            "controlnets": await options("ControlNetLoader", "control_net_name"),
        }


async def is_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(f"{COMFY_URL}/system_stats")
            return res.status_code == 200
    except httpx.RequestError:
        return False
