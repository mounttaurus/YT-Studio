"""書体の束 = 同梱の書体（remotion/src/gimmicks/fonts.json・Debian のパッケージ）＋ユーザーの書体。

ユーザーの書体（購入したものなど）は shared/motion/fonts/ に置くだけ。ここが自動で見つけて束に加える。
- イメージにもリポにも入らない（shared/ はバインド・git の対象外）＝公開リポに載らない
- コンテナの書体の検索先（fontconfig）にこのフォルダを加えてある（Dockerfile の fonts-user.conf）ので、
  描画の Chrome もそのまま使える
- 名前・太さ・日本語の有無は fc-query（fontconfig）で読む＝Chrome が書体を探す名前と同じ
- id は `user_<ファイル名>`。用途（role）や id を変えたい時は shared/motion/fonts/fonts.json に書く（任意）:
    {"fonts": {"user_myfont-bold": {"id": "title_gothic", "role": "タイトル・強調"}}}
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import subprocess
import time
from pathlib import Path

from app.core.config import REMOTION_DIR, USER_FONTS_DIR, WORK_DIR

BUNDLED_FILE = REMOTION_DIR / "src" / "gimmicks" / "fonts.json"
FONT_EXT = {".ttf", ".otf", ".ttc", ".otc"}
_cache: dict = {"key": None, "fonts": None}


def _css_weight(fc_weight: int) -> int:
    """fontconfig の太さ（0〜210）→ CSS の太さ（100〜900）。"""
    table = [(0, 100), (40, 200), (50, 300), (80, 400), (100, 500), (180, 600), (200, 700), (205, 800), (210, 900)]
    return min(table, key=lambda t: abs(t[0] - fc_weight))[1]


def _fc_weight(raw: str) -> int:
    """fc-query の太さ。可変フォントは範囲 "[50 205]" で来るので、標準（80）に一番近い値を採る。"""
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", raw or "")]
    if not nums:
        return 80
    return round(min(max(80, min(nums)), max(nums)))


def _slug(name: str) -> str:
    """id 用の名前。英数字が残らない名前（日本語のファイル名など）は、名前のハッシュで区別する
    （全部 "font" にすると id が重なり、後の書体が前の書体を黙って消すため）。"""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if re.search(r"[^\x00-\x7f]", name):
        s = f"{s}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]}".strip("-")
    return s or "font"


def _query_faces(path: Path) -> list[list[str]] | None:
    """fc-query で書体ファイルの顔（家族名・スタイル・太さ・index・対応言語）を読む。読めなければ None。"""
    tab = chr(9)
    try:
        # 項目はタブで区切る（%{lang} は対応言語を "|" で区切って返すため）
        fmt = tab.join(["%{family[0]}", "%{style[0]}", "%{weight}", "%{index}", "%{lang}"]) + chr(10)
        res = subprocess.run(["fc-query", "--format", fmt, str(path)],
                             capture_output=True, text=True, timeout=20, check=True)
    except (subprocess.SubprocessError, OSError):
        return None
    return [ln.split(tab) for ln in res.stdout.splitlines() if ln.count(tab) == 4]


def discover_user_fonts(folder: Path, cache_file: Path | None = None) -> dict[str, dict]:
    """フォルダの書体を fc-query で読み、束の形にする。読めないファイルは飛ばす。
    cache_file を渡すと、ファイルの大きさ・更新日時が変わっていない書体は fc-query を呼ばずに前回の結果を使う
    （書体が数百あると最初の読み込みだけで1分近くかかるため）。"""
    out: dict[str, dict] = {}
    if not folder.is_dir():
        return out
    cache: dict = {}
    if cache_file and cache_file.is_file():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cache = {}
    fresh: dict = {}
    for path in sorted(p for p in folder.rglob("*") if p.suffix.lower() in FONT_EXT):
        st = path.stat()
        sig = [st.st_size, st.st_mtime_ns]
        hit = cache.get(str(path))
        if hit and hit.get("sig") == sig:
            faces = hit["faces"]
        else:
            faces = _query_faces(path)
            if faces is None:
                continue
        fresh[str(path)] = {"sig": sig, "faces": faces}
        for family, style, weight, index, langs in faces:
            fid = f"user_{_slug(path.stem)}" + (f"-{index}" if len(faces) > 1 else "")
            if fid in out:  # 拡張子だけ違う同名のファイルなど
                fid = f"{fid}-{_slug(path.suffix)}"
            out[fid] = {
                "family": family,
                "weight": _css_weight(_fc_weight(weight)),
                "file": str(path),
                "index": int(index or 0),
                "role": "ユーザー書体（用途は未分類）",
                "style": style,
                "ja": "ja" in langs.split("|"),
                "user": True,
            }
    if cache_file and fresh != cache:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, cache_file)
        except OSError:
            pass
    return out


def _apply_overrides(found: dict[str, dict], folder: Path) -> dict[str, dict]:
    p = folder / "fonts.json"
    if not p.is_file():
        return found
    try:
        overrides = json.loads(p.read_text(encoding="utf-8")).get("fonts", {})
    except (ValueError, OSError):
        return found
    out = {}
    for fid, f in found.items():
        o = overrides.get(fid, {})
        new_id = o.get("id", fid)
        out[new_id] = {**f, **{k: v for k, v in o.items() if k != "id"}}
    return out


CHECK_TTL = 5.0  # 既定の置き場の書体は、この秒数のあいだ再確認しない（数百ファイルの stat が毎回は重い）


def load_fonts(bundled: Path | None = None, user_dir: Path | None = None) -> dict:
    """束の全体（同梱＋ユーザー）。ユーザーのフォルダの更新日時が変わった時だけ読み直す。"""
    default_dir = user_dir is None
    bundled = bundled or BUNDLED_FILE
    user_dir = user_dir if user_dir is not None else USER_FONTS_DIR
    now = time.monotonic()
    if default_dir and _cache["fonts"] is not None and _cache.get("dir") == str(user_dir) and now - _cache.get("at", -1e9) < CHECK_TTL:
        return _cache["fonts"]
    key = (str(bundled), bundled.stat().st_mtime,
           str(user_dir), max((p.stat().st_mtime for p in user_dir.rglob("*")), default=0) if user_dir.is_dir() else 0)
    if _cache["key"] == key:
        _cache["at"] = now
        return _cache["fonts"]
    base = json.loads(bundled.read_text(encoding="utf-8"))
    user = _apply_overrides(discover_user_fonts(user_dir, WORK_DIR / "fonts_cache.json" if default_dir else None), user_dir)
    merged = {**base, "fonts": {**base["fonts"], **{k: v for k, v in user.items() if k not in base["fonts"]}}}
    _cache.update(key=key, fonts=merged, dir=str(user_dir), at=now)
    return merged


def resolve_ref(fonts: dict, ref: str | None) -> str | None:
    """書体の指定を書体の id に直す。id そのもの、または `role:<用途タグ>`（タグを持つ書体の先頭。
    同梱を先に、id 順＝決定的）。解決できなければ None。"""
    if not isinstance(ref, str):
        return None
    table = fonts["fonts"]
    if ref in table:
        return ref
    if ref.startswith("role:"):
        tag = ref[5:]
        cands = [k for k, f in table.items() if tag in (f.get("tags") or []) and f.get("ja", True)]
        cands.sort(key=lambda k: (bool(table[k].get("user")), k))
        return cands[0] if cands else None
    return None
