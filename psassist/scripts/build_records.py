"""行ごとの合成記録と「✋ 手直し済み」の検知（Docs/LINE_WORKBENCH_PLAN.md §6-2・D15）。

再合成の出力先 `psd_final/` は、ユーザーが Photoshop で手直しして保存する PSD と**同じファイル**。
これまで手直しを守る仕組みが無く、再合成すると手直しが黙って消えた。ここで次を持つ:

  `psassist/build_records.json`  {"version":1,"records":{line_id:{built_at,psd_sha256,size,mtime_ns,lang,source}}}
      自動で作った（再合成した）時点の PSD の指紋。**上書きで消さない**（行ごとに追記・更新する）。

「手直し済み」＝今の PSD の内容（sha256）≠ 記録の指紋。**更新時刻では判定しない**（コピーや同期ソフトで
時刻だけ変わる誤検知を避ける）。ただし全 PSD を毎回ハッシュすると重いので、記録した (size, mtime_ns) と
一致すれば「変わっていない」と見なす近道を使う（一致しない時だけ sha256 で確かめる）。

記録が無い既存の PSD は、導入時に「今の状態＝自動で作ったもの」として初期化する（`init_missing`）。

この判定は director（読み取り専用）にも同じものがある（`director-agent/app/core/psd_records.py`）。
記録のファイル形式を変える時は両方を直す。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone

RECORDS_NAME = "build_records.json"
BACKUP_DIR = "_backup"
DEFAULT_LANG = "ja"          # 翻訳への備え（§7）。今は日本語だけ


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def psd_path(psa_dir: str, line_id: str) -> str:
    return os.path.join(psa_dir, "psd_final", "panel_%s.psd" % line_id)


def _records_file(psa_dir: str) -> str:
    return os.path.join(psa_dir, RECORDS_NAME)


def load(psa_dir: str) -> dict:
    try:
        with open(_records_file(psa_dir), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc.get("records"), dict):
        doc = {"version": 1, "records": {}}
    return doc


def save(psa_dir: str, doc: dict) -> None:
    """tmp → replace（読み手が書きかけを掴まない）。"""
    path = _records_file(psa_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _fingerprint(path: str) -> dict:
    st = os.stat(path)
    return {"psd_sha256": sha256_file(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def record_built(psa_dir: str, line_ids: list[str], *, source: str = "build", lang: str = DEFAULT_LANG) -> list[str]:
    """再合成した行の記録を更新する（PSD がある行だけ）。記録した行のIDを返す。"""
    doc = load(psa_dir)
    done = []
    for lid in line_ids:
        p = psd_path(psa_dir, lid)
        if not os.path.exists(p):
            continue
        doc["records"][lid] = {"built_at": now_iso(), "lang": lang, "source": source, **_fingerprint(p)}
        done.append(lid)
    if done:
        save(psa_dir, doc)
    return done


def init_missing(psa_dir: str) -> list[str]:
    """記録の無い既存 PSD を「今の状態＝自動で作ったもの」として記録する。初期化した行のIDを返す。"""
    d = os.path.join(psa_dir, "psd_final")
    if not os.path.isdir(d):
        return []
    doc = load(psa_dir)
    fresh = []
    for fn in sorted(os.listdir(d)):
        if not (fn.startswith("panel_") and fn.endswith(".psd")):
            continue
        lid = fn[len("panel_"):-len(".psd")]
        if lid in doc["records"]:
            continue
        p = os.path.join(d, fn)
        # 導入時の状態を基準にする。built_at は分からないので PSD の更新時刻を使う
        built = datetime.fromtimestamp(os.stat(p).st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        doc["records"][lid] = {"built_at": built, "lang": DEFAULT_LANG, "source": "initialized", **_fingerprint(p)}
        fresh.append(lid)
    if fresh:
        save(psa_dir, doc)
    return fresh


def edited_lines(psa_dir: str, line_ids: list[str] | None = None) -> set[str]:
    """手直し済みの行（PSD があり、記録があり、内容が記録と違う）。記録が無い行は判定不能＝含めない。"""
    doc = load(psa_dir)
    out = set()
    for lid in (line_ids if line_ids is not None else list(doc["records"])):
        rec = doc["records"].get(lid)
        p = psd_path(psa_dir, lid)
        if not rec or not os.path.exists(p):
            continue
        st = os.stat(p)
        if st.st_size == rec.get("size") and st.st_mtime_ns == rec.get("mtime_ns"):
            continue                                            # 近道: 変わっていない
        if sha256_file(p) != rec.get("psd_sha256"):
            out.add(lid)
    return out


def backup(psa_dir: str, line_id: str) -> str | None:
    """`psd_final/_backup/{line_id}_{日時}.psd` へ退避する。PSD が無ければ None。"""
    src = psd_path(psa_dir, line_id)
    if not os.path.exists(src):
        return None
    d = os.path.join(psa_dir, "psd_final", BACKUP_DIR)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(d, "%s_%s.psd" % (line_id, stamp))
    n = 1
    while os.path.exists(dst):                                  # 同じ秒に2回押されても上書きしない
        dst = os.path.join(d, "%s_%s_%d.psd" % (line_id, stamp, n))
        n += 1
    shutil.copy2(src, dst)
    return dst
