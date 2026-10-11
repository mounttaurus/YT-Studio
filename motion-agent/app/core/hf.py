"""HyperFrames（新しい組み立て方）の口。設計: Docs/MOTION_UNIFIED_PLAN.md §3・§5・§11。

- CLI（`hyperframes lint|snapshot|render`）はコンテナの中だけで動かす（U-D3）。ホストからは API・MCP・scripts/hf.sh。
- 対象（target）は2種類:
    samples/{name}                         見本（motion-agent/hyperframes/samples/{name}/）
    projects/{pid}/{ep}/{comp_id}          話のコンポジション（shared/projects/{pid}/episodes/epNN/motion/hf/{comp_id}/）
- 描く前に `prepare` が、プロジェクトの vendor/ へ GSAP を置き（描画中にネットワークを使わない＝M3）、
  motion.json の `generated` に書かれた見本の素材を作る（許可した生成器だけ）。
- テレメトリ・自動更新・Skill の確認は環境変数で切る（M9）。Dockerfile でも設定してあるが、ここでも必ず付ける。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from app.core import config, flash, hf_checks

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
VENDOR_FILES = {"gsap.min.js": Path("node_modules/gsap/dist/gsap.min.js")}
HF_ENV = {"HYPERFRAMES_NO_TELEMETRY": "1", "HYPERFRAMES_NO_UPDATE_CHECK": "1", "HYPERFRAMES_SKIP_SKILLS": "1", "CI": "1"}
# 描画の画質（hyperframes render --quality）。インサートは standard（§7）
DEFAULT_QUALITY = "standard"
QUALITIES = {"draft", "looks", "standard", "delivery", "high"}


class TargetError(ValueError):
    pass


def _name(v: str, what: str) -> str:
    if not isinstance(v, str) or not NAME_RE.match(v):
        raise TargetError(f"{what} は英数字・-・_ だけ（64字まで）: {v!r}")
    return v


def project_dir(project_id: str, episode: int, comp_id: str) -> Path:
    if not isinstance(episode, int) or episode < 1:
        raise TargetError(f"episode は1以上の整数: {episode!r}")
    return (config.SHARED_DIR / "projects" / _name(project_id, "project_id") / "episodes" / f"ep{episode:02d}"
            / "motion" / "hf" / _name(comp_id, "comp_id"))


def resolve(target: str) -> Path:
    """target 文字列 → コンポジションのフォルダ（存在は確かめない）。"""
    parts = (target or "").strip("/").split("/")
    if len(parts) == 2 and parts[0] == "samples":
        return config.HF_SAMPLES_DIR / _name(parts[1], "sample")
    if len(parts) == 4 and parts[0] == "projects":
        try:
            ep = int(parts[2])
        except ValueError as e:
            raise TargetError(f"episode は整数: {parts[2]!r}") from e
        return project_dir(parts[1], ep, parts[3])
    raise TargetError("target は samples/{name} か projects/{pid}/{ep}/{comp_id}")


def existing(target: str) -> Path:
    d = resolve(target)
    if not (d / "index.html").is_file():
        raise FileNotFoundError(f"コンポジションが無い: {target}")
    return d


def read_motion(d: Path) -> dict | None:
    p = d / "motion.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def version() -> str | None:
    p = config.HF_DIR / "node_modules" / "hyperframes" / "package.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("version")


# ── 準備（vendor・見本の素材） ──

def _gen_portrait(out: Path, spec: dict) -> None:
    from app.tools import make_portrait
    make_portrait.draw(int(spec.get("seed", 1963))).save(out, optimize=True)


# motion.json の generated[].tool に書ける生成器（任意のコードは走らせない）
GENERATORS = {"portrait": _gen_portrait}


def prepare(d: Path) -> list[str]:
    """描く前の準備。置いた・作ったファイルの相対パスを返す。"""
    made: list[str] = []
    vendor = d / "vendor"
    for name, src_rel in VENDOR_FILES.items():
        src = config.HF_DIR / src_rel
        dst = vendor / name
        if src.is_file() and (not dst.is_file() or dst.stat().st_size != src.stat().st_size):
            vendor.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            made.append(f"vendor/{name}")
    motion = read_motion(d) or {}
    for spec in motion.get("generated") or []:
        tool = GENERATORS.get(spec.get("tool"))
        out_rel = spec.get("out") or ""
        out = (d / out_rel).resolve()
        if tool is None or not out_rel or d.resolve() not in out.parents:
            continue
        if not out.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            tool(out, spec)
            made.append(out_rel)
    return made


# ── CLI ──

def _env() -> dict:
    env = dict(os.environ)
    env.update(HF_ENV)
    return env


def run_cli(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([str(config.HF_BIN), *args], cwd=str(cwd), env=_env(), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _json_tail(text: str):
    """CLI の出力から JSON を取り出す（前に案内の行が付いていても読む）。"""
    text = text.strip()
    for start in (text.find("{"), text.find("[")):
        if start < 0:
            continue
        try:
            return json.loads(text[start:])
        except ValueError:
            continue
    return None


def lint(d: Path) -> dict:
    """`hyperframes lint --json`。返り値: {ok, errors, warnings, raw?}。"""
    try:
        cp = run_cli(["lint", "--json", str(d)], cwd=d, timeout=config.HF_CHECK_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return {"ok": False, "errors": [{"code": "lint_timeout", "message": "lint が時間内に終わらない"}], "warnings": []}
    data = _json_tail(cp.stdout)
    if not isinstance(data, dict):
        return {"ok": cp.returncode == 0, "errors": [] if cp.returncode == 0 else
                [{"code": "lint_failed", "message": (cp.stderr or cp.stdout)[-1500:]}], "warnings": []}
    findings = data.get("findings") or data.get("issues") or []
    errors = [f for f in findings if str(f.get("severity", f.get("level", ""))).lower() == "error"]
    warnings = [f for f in findings if str(f.get("severity", f.get("level", ""))).lower() in ("warning", "warn")]
    ok = bool(data.get("ok", not errors)) and not errors
    return {"ok": ok, "errors": errors, "warnings": warnings, "summary": {k: v for k, v in data.items() if k != "findings"}}


def check(target: str) -> dict:
    """描く前の検査（lint＋ネットワーク＋長さ）。誤りがあれば ok: false（描画は止める＝M6）。"""
    d = existing(target)
    prepared = prepare(d)
    motion = read_motion(d)
    ours = hf_checks.check_network(d)
    length_issues, duration = hf_checks.check_length(d, motion)
    ours += length_issues
    lr = lint(d)
    errors = [i for i in ours if i["level"] == "error"] + [{"code": f.get("code", "lint"), "level": "error",
                                                              "message": f.get("message", ""), "source": "lint",
                                                              **({"file": f["file"]} if f.get("file") else {})}
                                                             for f in lr["errors"]]
    warnings = [i for i in ours if i["level"] == "warning"] + [{"code": f.get("code", "lint"), "level": "warning",
                                                                  "message": f.get("message", ""), "source": "lint"}
                                                                 for f in lr["warnings"]]
    return {"target": target, "ok": not errors, "errors": errors, "warnings": warnings, "duration": duration,
            "profile": (motion or {}).get("profile"), "prepared": prepared, "hyperframes": version()}


def list_samples() -> list[dict]:
    out = []
    if config.HF_SAMPLES_DIR.is_dir():
        for d in sorted(config.HF_SAMPLES_DIR.iterdir()):
            if (d / "index.html").is_file():
                m = read_motion(d) or {}
                out.append({"target": f"samples/{d.name}", "profile": m.get("profile"), "notes": m.get("notes")})
    return out


def list_project(project_id: str, episode: int) -> list[dict]:
    base = project_dir(project_id, episode, "x").parent
    out = []
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if (d / "index.html").is_file():
                m = read_motion(d) or {}
                out.append({"target": f"projects/{project_id}/{episode}/{d.name}", "comp_id": d.name,
                            "profile": m.get("profile"), "fit": m.get("fit"), "notes": m.get("notes")})
    return out


# ── ジョブ（jobs.py の作業者から呼ばれる） ──

def job_spec(kind: str, target: str, job_dir: Path, frames: int = 6, at: list[float] | None = None,
             quality: str = DEFAULT_QUALITY) -> dict:
    d = existing(target)
    if quality not in QUALITIES:
        raise TargetError(f"quality は {sorted(QUALITIES)} のどれか: {quality!r}")
    spec = {"engine": "hyperframes", "mode": "video" if kind == "render" else "stills", "target": target,
            "dir": str(d), "fps": hf_checks.FPS, "quality": quality,
            "out": str(job_dir / "out.mp4") if kind == "render" else str(job_dir / "stills")}
    if kind != "render":
        spec["frames"] = max(1, min(12, int(frames)))
        if at:
            spec["at"] = [round(float(t), 3) for t in at][:12]
    return spec


def _stills_cmd(spec: dict) -> list[str]:
    args = ["snapshot", spec["dir"]]
    if spec.get("at"):
        args += ["--at", ",".join(f"{t:g}" for t in spec["at"])]
    else:
        args += ["--frames", str(spec["frames"])]
    return args


def run_job(job_id: str, job_dir: Path, spec: dict, update, rel, make_sheet) -> None:
    """HyperFrames の静止画・描画のジョブを1本回す。update(job_id, **fields) で状態を書く。"""
    d = Path(spec["dir"])
    prepare(d)
    started = time.monotonic()
    update(job_id, status="running", progress=0.05)
    if spec["mode"] == "video":
        out = Path(spec["out"])
        args = ["render", str(d), "--output", str(out), "--fps", str(spec["fps"]), "--quality", spec["quality"],
                "--workers", str(config.RENDER_CONCURRENCY), "--no-browser-gpu"]
    else:
        snap_dir = d / "snapshots"
        if snap_dir.is_dir():
            shutil.rmtree(snap_dir)
        args = _stills_cmd(spec)
    try:
        cp = run_cli(args, cwd=d, timeout=config.RENDER_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        update(job_id, status="error", error="時間切れ", elapsed_sec=round(time.monotonic() - started, 1))
        return
    elapsed = round(time.monotonic() - started, 1)
    log = (cp.stdout + "\n" + cp.stderr).strip()
    (job_dir / "cli.log").write_text(log, encoding="utf-8")
    if cp.returncode != 0:
        update(job_id, status="error", error=log[-4000:] or f"exit {cp.returncode}", elapsed_sec=elapsed)
        return
    if spec["mode"] == "video":
        out = Path(spec["out"])
        if not out.is_file():
            update(job_id, status="error", error="mp4 ができていない\n" + log[-2000:], elapsed_sec=elapsed)
            return
        result = {"file": rel(out), "url": f"/files/{rel(out)}", "summary": _render_summary(log)}
        # 描いた映像の明滅を実測する（画面の25%以上・毎秒3回まで）。落ちたら箱に入れない（M6）
        result["flash"] = flash.analyze_video(str(out), float(spec["fps"]))
    else:
        result = _collect_stills(d, Path(spec["out"]), rel)
        if not result["stills"]:
            update(job_id, status="error", error="静止画ができていない\n" + log[-2000:], elapsed_sec=elapsed)
            return
        sheet = make_sheet(Path(spec["out"]), result["stills"], 3, (640, 360))
        if sheet:
            result["sheet"] = {"file": rel(sheet), "url": f"/files/{rel(sheet)}",
                               "order": [s["name"] for s in result["stills"]], "columns": 3}
    update(job_id, status="done", progress=1.0, result=result, elapsed_sec=elapsed)


def _render_summary(log: str) -> str | None:
    """render の要約の行（capture の方式・GPU・段ごとの時間）。公式が「2行目を読め」と言う行。"""
    for line in log.splitlines():
        if "capture" in line and ("gpu" in line or "compile" in line):
            return line.strip()
    return None


_SEC_RE = re.compile(r"([0-9]+(?:[._][0-9]+)?)s?$")


def _collect_stills(d: Path, out_dir: Path, rel) -> dict:
    """snapshots/ の PNG を作業用の置き場へ写す。名前は時刻順の s01, s02…（見本シートの順）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pngs = sorted((d / "snapshots").rglob("*.png"), key=lambda p: (_png_time(p), p.name))
    stills = []
    for i, p in enumerate(pngs, 1):
        name = f"s{i:02d}"
        dst = out_dir / f"{name}.png"
        shutil.copyfile(p, dst)
        stills.append({"name": name, "source": p.name, "sec": _png_time(p), "file": rel(dst), "url": f"/files/{rel(dst)}"})
    return {"stills": stills}


def _png_time(p: Path) -> float:
    m = _SEC_RE.search(p.stem.replace("-", "_").split("_")[-1])
    try:
        return float(m.group(1).replace("_", ".")) if m else 0.0
    except ValueError:
        return 0.0
