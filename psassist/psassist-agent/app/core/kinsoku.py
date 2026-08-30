"""日本語の行分割（禁則処理）.

Photoshop の禁則は弱く、縦中横も絡むと崩れる。そこで **行分割はここで確定
させ、明示改行入りのテキストとして流し込む**（spec.USE_PHOTOSHOP_KINSOKU=False）。

方式は「追い出し」のみ。行頭・行末の禁則に触れたら、その文字を次行へ送る
（ぶら下がりは使わない＝実測PSDでも Burasagari=False だった）。
"""

from __future__ import annotations

import math
import re
import unicodedata

# 行頭にきてはいけない文字（句読点・閉じ括弧・小書き・音引き・繰り返し記号）
NOT_LINE_START = frozenset(
    "、。，．・：；？！?!‼⁇⁈⁉"
    "）〕］｝〉》」』】〙〗〟’”｠»"
    ")]}"
    "ぁぃぅぇぉっゃゅょゎゕゖ"
    "ァィゥェォッャュョヮヵヶ"
    "ーゝゞヽヾ々〻"
    "‐゠–〜～"
    "…‥"
)

# 行末にきてはいけない文字（開き括弧）
NOT_LINE_END = frozenset("（〔［｛〈《「『【〘〖〝‘“｟«([{")

# 分割してはいけない連続（同一文字が続くもの）
NO_SPLIT_RUNS = ("……", "‥‥", "――", "──")

# 半角英数トークン（USA / 9.11 / .30-06 / 4K など）は途中で折らない
ALNUM_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.\-']*|[.][0-9]+[0-9A-Za-z.\-]*")


def char_width(ch: str) -> float:
    """全角=1.0 / 半角=0.5 の実効幅。"""
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.5


def text_width(s: str) -> float:
    return sum(char_width(c) for c in s)


def _atoms(text: str) -> list[str]:
    """折ってよい最小単位に分解する。英数トークンは1原子にまとめる。"""
    atoms: list[str] = []
    i = 0
    while i < len(text):
        m = ALNUM_TOKEN.match(text, i)
        if m and len(m.group()) > 1:
            atoms.append(m.group())
            i = m.end()
            continue
        # 分離禁止の連続
        hit = next((r for r in NO_SPLIT_RUNS if text.startswith(r, i)), None)
        if hit:
            atoms.append(hit)
            i += len(hit)
            continue
        atoms.append(text[i])
        i += 1
    return atoms


def _needs_push(lines: list[list[str]], i: int, max_width: float) -> bool:
    """行 i の末尾を次行へ送る必要があるか。"""
    cur = lines[i]
    if len(cur) <= 1:
        return False  # 1原子しか無い行を空にはしない
    # 幅超過
    if text_width("".join(cur)) > max_width:
        return True
    # 行末禁則（開き括弧で終わっている）
    if cur[-1][-1] in NOT_LINE_END:
        return True
    # 次行の先頭が行頭禁則
    if i + 1 < len(lines) and lines[i + 1] and lines[i + 1][0][0] in NOT_LINE_START:
        return True
    return False


def _fix_kinsoku(lines: list[list[str]], max_width: float) -> list[list[str]]:
    """幅超過・行頭禁則・行末禁則を「追い出し」だけで解消する。

    いずれの違反も操作は同じ（行末の1原子を次行の先頭へ送る）。連鎖する
    ので収束するまで繰り返す。
    """
    guard = 0
    while guard < 200:
        guard += 1
        for i in range(len(lines)):
            if not _needs_push(lines, i, max_width):
                continue
            if i + 1 >= len(lines):
                lines.append([])
            lines[i + 1].insert(0, lines[i].pop())
            break  # 1件動かすたびに最初から見直す（連鎖に追随するため）
        else:
            break  # 違反ゼロ
    return [l for l in lines if l]


# --- 分割位置の良し悪し（小さいほど良い） --------------------------------
# ユーザーの手作業を見ると、行を目一杯詰めるより**意味の切れ目で改行する**
# 方が読みやすい（右端が揃わなくてよい）。貪欲法だと句点を無視して詰めてしまう
# ので、分割位置に罰点を付けて全体最適を解く。
SENTENCE_END = frozenset("。！？!?")
COMMA = frozenset("、，")
CLOSERS = frozenset("）〕］｝〉》」』】〙〗〟’”)]}")
MIDDOT = frozenset("・：；:;")
# 助詞・接続で切ると比較的自然（形態素解析なしで拾える範囲）
PARTICLES = ("は", "が", "を", "に", "へ", "と", "で", "も", "や", "の",
             "から", "まで", "より", "ので", "けど", "が、", "ね", "よ")
KATAKANA = frozenset("ァィゥェォャュョッーヴアイウエオカキクケコサシスセソタチツテトナニヌネノ"
                     "ハヒフヘホマミムメモヤユヨラリルレロワヲン゛゜")


def _break_penalty(prev_atom: str, next_atom: str) -> float:
    """prev と next の**あいだ**で改行する時の罰点。"""
    p, n = prev_atom[-1], next_atom[0]
    if p in SENTENCE_END:
        return 0.0  # 句点の直後＝最良
    if p in COMMA:
        return 1.0  # 読点の直後
    if p in CLOSERS:
        return 2.0
    if p in MIDDOT:
        return 6.0
    if prev_atom in PARTICLES or (len(prev_atom) == 1 and prev_atom in ("は", "が", "を", "に", "へ", "と", "で", "も", "の")):
        return 4.0
    if p in KATAKANA and n in KATAKANA:
        return 14.0  # カタカナ語の途中で割るのは避けたい
    if p.isdigit() and n.isdigit():
        return 20.0
    return 8.0


def wrap(text: str, max_width: float, *, ragged_weight: float = 0.02) -> list[str]:
    """max_width（全角換算）で折り返す。禁則を守り、意味の切れ目を優先する。

    動的計画法で「分割位置の罰点 ＋ 行末の余白^2×重み」の総和を最小化する。
    ragged_weight を小さくするほど右端の不揃いを許し、句読点での改行を優先する。

    text に含まれる既存の改行は段落境界として尊重する。
    """
    if max_width <= 0:
        raise ValueError("max_width must be positive")

    out: list[str] = []
    for para in re.split(r"[\r\n]+", text):
        para = para.strip()
        if not para:
            continue
        out.extend(_wrap_para(_atoms(para), max_width, ragged_weight))
    return out


def _wrap_para(atoms: list[str], max_width: float, ragged_weight: float) -> list[str]:
    n = len(atoms)
    widths = [text_width(a) for a in atoms]
    # cum[i] = atoms[:i] の幅
    cum = [0.0]
    for w in widths:
        cum.append(cum[-1] + w)

    INF = float("inf")
    cost = [INF] * (n + 1)
    prev = [0] * (n + 1)
    cost[0] = 0.0
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            line_w = cum[j] - cum[i]
            if line_w > max_width and i < j - 1:
                break  # これ以上左へ伸ばしても入らない
            if cost[i] == INF:
                continue
            slack = max_width - line_w
            # 最終行の余白は罰しない（そこで終わるのが自然）
            c = cost[i] + (0.0 if j == n else ragged_weight * slack * slack)
            if j < n:
                c += _break_penalty(atoms[j - 1], atoms[j])
            if c < cost[j]:
                cost[j] = c
                prev[j] = i
    if cost[n] == INF:  # 1原子が max_width を超える等。素直に貪欲へ落とす
        return _wrap_greedy(atoms, max_width)

    cuts = []
    j = n
    while j > 0:
        cuts.append((prev[j], j))
        j = prev[j]
    cuts.reverse()
    lines = [list(atoms[i:j]) for i, j in cuts]
    return ["".join(l) for l in _fix_kinsoku(lines, max_width)]


def _wrap_greedy(atoms: list[str], max_width: float) -> list[str]:
    lines: list[list[str]] = [[]]
    width = 0.0
    for atom in atoms:
        w = text_width(atom)
        if width + w > max_width and lines[-1]:
            lines.append([])
            width = 0.0
        lines[-1].append(atom)
        width += w
    return ["".join(l) for l in _fix_kinsoku(lines, max_width)]


def fits(lines: list[str], max_width: float, max_lines: int) -> bool:
    return len(lines) <= max_lines and all(text_width(l) <= max_width for l in lines)


def split_for_bubbles(text: str, max_chars: int) -> list[str] | None:
    """長すぎるセリフを、1つが max_chars を超えないように**N分割**する候補を返す。

    ⚠️ **2分割固定にしない。** 第1話で 403字（収まる上限151字の2.7倍）の行が出て、
       2分割案では1つ201字＝まだ収まらなかった。必要な数だけ割る。

    割れない（句読点が無い）場合は None。判断はユーザーに委ねる前提で、
    ここでは「句点優先・なるべく均等」な候補を1つ出すだけ。
    """
    # ⚠️ max_chars は「**何分割するか**」を決める値であって「割るかどうか」ではない。
    #    ここで「上限以下なら None」にすると、プラン側が収まらないと判断した行
    #    （プランの見積りは実測フィットより厳しい）で提案が消える。呼ばれたら必ず割る。
    text = text.strip()
    if not text:
        return None

    def units_at(pattern: str) -> list[str]:
        """区切り文字の直後で切った断片列（区切り文字は前の断片に残す）。"""
        cuts = [m.end() for m in re.finditer(pattern, text) if 0 < m.end() < len(text)]
        out, prev = [], 0
        for c in cuts + [len(text)]:
            seg = text[prev:c]
            if seg:
                out.append(seg)
            prev = c
        return out

    # 句点で足りなければ読点も使う。★「最大の断片が上限を超えないか」で判定する
    #   （平均で見ると、長い1文が混ざった時に取りこぼす）。
    units = units_at(r"[。！？!?]")
    if len(units) < 2 or max(len(u) for u in units) > max_chars:
        finer = units_at(r"[。！？!?、，]")
        if len(finer) > len(units):
            units = finer
    if len(units) < 2:
        return None

    def pack(limit: int) -> list[str]:
        """前から詰める。limit を超えるなら次へ送る（各断片 <= limit を保証）。"""
        out: list[str] = []
        for u in units:
            if out and len(out[-1]) + len(u) <= limit:
                out[-1] += u
            else:
                out.append(u)
        return out

    # ★分割数は少ないほど良く、その中では均等なほど良い。
    #   前から上限まで詰めるだけだと末尾が痩せる（実測: 403字が 111/136/120/36）。
    #   分割数 n を最小から試し、n個に収まる**最も小さい上限**を探す。
    total = len(text)
    longest = max(len(u) for u in units)
    best = pack(max_chars)
    for n in range(max(2, -(-total // max_chars)), len(units) + 1):
        limit = max(-(-total // n), longest)
        if limit > max_chars:
            continue
        while limit <= max_chars:
            cand = pack(limit)
            if len(cand) <= n:
                best = cand
                break
            limit += 1
        if len(best) <= n:
            break

    parts = [p.strip() for p in best if p.strip()]
    return parts if len(parts) >= 2 else None
