"""話のコンポジションへファイルを書く口（Docs/MOTION_UNIFIED_PLAN.md §5-1・§11-3・M4・I1）。

motion-agent だけが `shared/projects/{pid}/episodes/epNN/motion/hf/{comp_id}/` に書く（M4）。MCP の `motion_hf_put` の口。
- 書けるのは index.html・motion.json・compositions/custom/*.html（その場の部品）・assets/ の素材だけ。
  compositions/blocks/ は辞書から `prepare` が写す（M7）ので、ここでは書かせない。vendor/・snapshots/ も書かせない。
- 書く前に、上書きする index.html・motion.json を `history/{時刻}/` へ残す（最新20）。
- 新しい comp_id は mi_0001 から。識別子＝改名しない（M5: 作り直しは新しい ID）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from app.core import config, hf

PATH_RE = re.compile(
    r"^(?:index\.html|motion\.json|compositions/custom/[A-Za-z0-9_-]{1,48}\.html"
    r"|assets/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*\.(?:png|jpg|jpeg|webp|svg|json|txt))$")
BINARY_RE = re.compile(r"\.(?:png|jpg|jpeg|webp)$", re.I)
COMP_RE = re.compile(r"^mi_(\d{4})$")
MAX_FILES = 40
MAX_BYTES = 8 * 1024 * 1024
HISTORY_KEEP = 20
# motion.json の中で motion-agent が書く台帳の項目（PUT で上書きされても残す）
SERVER_KEYS = ("last_registered",)


class PutError(ValueError):
    pass


def _hf_base(project_id: str, episode: int) -> Path:
    base = hf.project_dir(project_id, episode, "mi_0000").parent
    return base


def next_comp_id(project_id: str, episode: int) -> str:
    base = _hf_base(project_id, episode)
    top = 0
    if base.is_dir():
        for c in base.iterdir():
            m = COMP_RE.match(c.name)
            if m:
                top = max(top, int(m.group(1)))
    return f"mi_{top + 1:04d}"


def _box_asset_bytes(box_dir: Path, path: str, asset_id: str) -> bytes:
    """Bロールの箱（episodes/epNN/broll/）の素材の中身を、コンポジションの assets/ へ写すために読む（読むだけ＝M4）。
    箱の素材は出典・ライセンスが台帳にある＝素材を base64 で会話に流さずに済む。写真だけ・拡張子は箱のファイルと同じ。"""
    try:
        doc = json.loads((box_dir / "broll.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PutError(f"{path}: この話に Bロールの箱が無い（broll_register で素材を入れてから）") from e
    asset = next((a for a in doc.get("assets") or [] if a.get("asset_id") == asset_id), None)
    if asset is None:
        raise PutError(f"{path}: 箱に無い素材: {asset_id}（broll_list で asset_id を確かめる）")
    if asset.get("media_type") != "photo":
        raise PutError(f"{path}: {asset_id} は写真ではない（assets/ へ写せるのは写真だけ）")
    src = (box_dir.parent / str(asset.get("file") or "")).resolve()
    if src.parent != box_dir.resolve() or not src.is_file():
        raise PutError(f"{path}: 箱の素材のファイルが無い: {asset_id}")
    ext = lambda n: ".jpg" if n.lower().endswith((".jpg", ".jpeg")) else Path(n).suffix.lower()   # noqa: E731
    if ext(path) != ext(src.name):
        raise PutError(f"{path}: 拡張子が箱の素材（{src.suffix}）と違う。{Path(path).stem}{src.suffix} にする")
    return src.read_bytes()


def _payload(path: str, value, box_dir: Path | None = None) -> bytes:
    if isinstance(value, dict) and "asset_id" in value and "base64" not in value:
        if box_dir is None:
            raise PutError(f"{path}: 箱の素材は話のコンポジションにだけ写せる")
        data = _box_asset_bytes(box_dir, path, str(value["asset_id"]))
    elif isinstance(value, dict) and "base64" in value:
        if not BINARY_RE.search(path):
            raise PutError(f"{path}: base64 で書けるのは画像（png・jpg・webp）だけ")
        try:
            data = base64.b64decode(value["base64"], validate=True)
        except ValueError as e:
            raise PutError(f"{path}: base64 が読めない") from e
    elif isinstance(value, (dict, list)):
        data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    elif isinstance(value, str):
        if BINARY_RE.search(path):
            raise PutError(f"{path}: 画像は {{\"base64\": …}} で渡す")
        data = value.encode("utf-8")
    else:
        raise PutError(f"{path}: 中身は文字列・JSON・{{base64}} のどれか")
    if len(data) > MAX_BYTES:
        raise PutError(f"{path}: {MAX_BYTES // (1024 * 1024)}MB を超える")
    if path == "motion.json":
        try:
            parsed = json.loads(data.decode("utf-8"))
        except ValueError as e:
            raise PutError("motion.json が JSON として読めない") from e
        if not isinstance(parsed, dict):
            raise PutError("motion.json は辞書")
    return data


def _keep_server_keys(old_path: Path, new_data: bytes) -> bytes:
    """motion.json を丸ごと書き換えられても、台帳の項目（SERVER_KEYS）は前の値を引き継ぐ。
    last_registered が消えると、作り直した時に古い箱の素材を差し替えられない（replaces が空になる）。"""
    try:
        old = json.loads(old_path.read_text(encoding="utf-8"))
        new = json.loads(new_data.decode("utf-8"))
    except (OSError, ValueError):
        return new_data
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new_data
    carry = {k: old[k] for k in SERVER_KEYS if k in old and k not in new}
    if not carry:
        return new_data
    new.update(carry)
    return (json.dumps(new, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def put(project_id: str, episode: int, comp_id: str | None, files: dict) -> dict:
    """ファイルを書く。comp_id が空なら新しい mi_NNNN を取る。返り値: {comp_id, target, written, created, history}。"""
    if not isinstance(files, dict) or not files:
        raise PutError("files は {パス: 中身} の辞書（1つ以上）")
    if len(files) > MAX_FILES:
        raise PutError(f"files は {MAX_FILES} 個まで")
    project_root = config.SHARED_DIR / "projects" / hf._name(project_id, "project_id")
    box_dir = project_root / "episodes" / f"ep{int(episode):02d}" / "broll"
    if not project_root.is_dir():
        raise FileNotFoundError(f"プロジェクトが無い: {project_id}")
    payloads: dict[str, bytes] = {}
    for path, value in files.items():
        if not isinstance(path, str) or ".." in path.split("/") or not PATH_RE.match(path):
            raise PutError(f"書けないパス: {path!r}（index.html・motion.json・compositions/custom/*.html・assets/ の画像／json／txt だけ）")
        payloads[path] = _payload(path, value, box_dir)
    created = not comp_id
    cid = comp_id or next_comp_id(project_id, episode)
    d = hf.project_dir(project_id, episode, cid)
    if not created and not d.is_dir():
        raise FileNotFoundError(f"コンポジションが無い: {cid}（新しく作るなら comp_id を空に）")
    d.mkdir(parents=True, exist_ok=True)
    # 直前の版を残す（上書きする index.html・motion.json だけ）
    hist_name = None
    olds = [p for p in ("index.html", "motion.json") if p in payloads and (d / p).is_file()]
    if olds:
        hist_name = datetime.now().strftime("%Y%m%d-%H%M%S")
        h = d / "history" / hist_name
        h.mkdir(parents=True, exist_ok=True)
        for p in olds:
            shutil.copyfile(d / p, h / p)
        hist = sorted(x for x in (d / "history").iterdir() if x.is_dir())
        for old in hist[:-HISTORY_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
    if "motion.json" in payloads and (d / "motion.json").is_file():
        payloads["motion.json"] = _keep_server_keys(d / "motion.json", payloads["motion.json"])
    written = []
    for path, data in payloads.items():
        out = d / path
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, out)
        written.append(path)
    return {"comp_id": cid, "target": f"projects/{project_id}/{episode}/{cid}", "written": written, "created": created,
            "history": hist_name, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
