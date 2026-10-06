"""描画ジョブ。CPU を食うので1本ずつ順に回す（ワーカー1本）。

ジョブの置き場は `shared/motion/_work/{job_id}/`（input.json・props.json・spec.json・job.json・成果物）。
状態は job.json にも書くので、コンテナを再起動しても終わったジョブの結果は読める。
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core import config, flash

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_queue: "queue.Queue[str]" = queue.Queue()
_worker: threading.Thread | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dir(job_id: str) -> Path:
    return config.WORK_DIR / job_id


def _save(job: dict) -> None:
    d = _job_dir(job["job_id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs[job_id]
        job.update(fields)
        _save(job)


def rel(p: Path) -> str:
    """shared/ からの相対パス（API で返す形）。"""
    return p.relative_to(config.SHARED_DIR).as_posix()


def submit(kind: str, composition: str, inp: dict, props: dict, frames: list[dict] | None = None,
           sheet: dict | None = None) -> dict:
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "input.json").write_text(json.dumps(inp, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "props.json").write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
    spec = {
        "mode": "video" if kind == "render" else "stills",
        "composition": composition,
        "props": str(d / "props.json"),
        "out": str(d / "out.mp4") if kind == "render" else str(d / "stills"),
        "frames": frames or [],
        "sheet": sheet,
        "scale": config.STILL_SCALE,
        "concurrency": config.RENDER_CONCURRENCY,
    }
    (d / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    job = {"job_id": job_id, "kind": kind, "composition": composition, "status": "queued",
           "progress": 0.0, "result": None, "error": None, "created_at": _now(), "finished_at": None}
    with _lock:
        _jobs[job_id] = job
        _save(job)
    _ensure_worker()
    _queue.put(job_id)
    return dict(job)


def get(job_id: str) -> dict | None:
    with _lock:
        if job_id in _jobs:
            return dict(_jobs[job_id])
    p = _job_dir(job_id) / "job.json"
    if job_id and "/" not in job_id and p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def wait(job_id: str, timeout_sec: float) -> dict | None:
    end = time.monotonic() + timeout_sec
    while time.monotonic() < end:
        job = get(job_id)
        if job is None or job["status"] in ("done", "error"):
            return job
        time.sleep(0.5)
    return get(job_id)


SHEET_COLUMNS = 3
SHEET_CELL = (640, 360)


def _make_sheet(stills_dir: Path, stills: list[dict], columns: int = SHEET_COLUMNS,
                cell: tuple[int, int] = SHEET_CELL) -> Path | None:
    """確認用の静止画を1枚の見本シート（3列）にまとめる。Claude Code が1回で全体を見られるように
    （画像1枚＝会話のトークンを絞る）。失敗しても静止画そのものは返すので、シートだけ諦める。"""
    if not stills:
        return None
    out = stills_dir / "sheet.jpg"  # JPEG（1.6MB→約0.3MB。会話に載せる画像なので軽く）
    w, h = cell
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for s in stills:
        cmd += ["-i", str(stills_dir / f"{s['name']}.png")]
    chains = "".join(f"[{i}]scale={w}:{h}[v{i}];" for i in range(len(stills)))
    layout = "|".join(f"{(i % columns) * w}_{(i // columns) * h}" for i in range(len(stills)))
    if len(stills) == 1:
        graph = f"{chains}[v0]copy[o]"
    else:
        graph = f"{chains}{''.join(f'[v{i}]' for i in range(len(stills)))}xstack=inputs={len(stills)}:layout={layout}:fill=black[o]"
    cmd += ["-filter_complex", graph, "-map", "[o]", "-frames:v", "1", "-q:v", "4", str(out)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    return out if out.is_file() else None


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, name="motion-render", daemon=True)
            _worker.start()


def _loop() -> None:
    while True:
        job_id = _queue.get()
        try:
            _run(job_id)
        except Exception as e:  # noqa: BLE001 — ワーカーを止めない
            _update(job_id, status="error", error=str(e), finished_at=_now())


def _run(job_id: str) -> None:
    d = _job_dir(job_id)
    spec = json.loads((d / "spec.json").read_text(encoding="utf-8"))
    _update(job_id, status="running", started_at=_now())
    stills: list[dict] = []
    tail: list[str] = []
    started = time.monotonic()
    proc = subprocess.Popen(
        [config.NODE_BIN, str(config.REMOTION_DIR / "render.mjs"), str(d / "spec.json")],
        cwd=str(config.REMOTION_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        try:
            msg = json.loads(line)
        except ValueError:
            tail = (tail + [line])[-30:]
            continue
        t = msg.get("type")
        if t == "bundling":
            _update(job_id, status="bundling")
        elif t == "progress":
            _update(job_id, status="running", progress=msg["progress"])
        elif t == "still":
            out = Path(msg["out"])
            stills.append({"name": msg["name"], "frame": msg["frame"], "file": rel(out), "url": f"/files/{rel(out)}"})
            _update(job_id, progress=len(stills) / max(1, len(spec["frames"])))
        elif t == "error":
            tail = (tail + [msg.get("message", "")])[-30:]
        if time.monotonic() - started > config.RENDER_TIMEOUT_SEC:
            proc.kill()
            break
    code = proc.wait()
    elapsed = round(time.monotonic() - started, 1)
    if code != 0:
        _update(job_id, status="error", error="\n".join(tail)[-4000:] or f"exit {code}",
                elapsed_sec=elapsed, finished_at=_now())
        return
    if spec["mode"] == "video":
        out = Path(spec["out"])
        result = {"file": rel(out), "url": f"/files/{rel(out)}"}
        # 描いた映像の明滅を実測する（演出プランの概算とは別。画面の25%以上・毎秒3回まで）
        props = json.loads((d / "props.json").read_text(encoding="utf-8"))
        fps = props.get("fps") or (props.get("timing") or {}).get("fps") or 30
        result["flash"] = flash.analyze_video(str(out), float(fps))
    else:
        result = {"stills": stills}
        lay = spec.get("sheet") or {}
        columns = int(lay.get("columns", SHEET_COLUMNS))
        sheet = _make_sheet(Path(spec["out"]), stills, columns, tuple(lay.get("cell", SHEET_CELL)))
        if sheet:
            result["sheet"] = {"file": rel(sheet), "url": f"/files/{rel(sheet)}",
                               "order": [s["name"] for s in stills], "columns": columns}
    _update(job_id, status="done", progress=1.0, result=result, elapsed_sec=elapsed, finished_at=_now())
