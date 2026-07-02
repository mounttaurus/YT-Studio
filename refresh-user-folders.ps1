# ===================================================================
# YT-Studio - ユーザー向け「友好フォルダ」生成 (_ユーザーファイル\)
#
# shared\ 配下に散らばった "ユーザーが直接触るファイル" を、人間に読める
# 名前のショートカット(NTFSジャンクション)で 1か所に集約する。
#   - キャラ / プロジェクトは JSON の name / title をフォルダ名にする
#     (= 識別ID  20260615_002_02_cat  ではなく  02-CAT  で並ぶ)
#   - 中身を編集すると shared\ の実体に直接反映される(同じ inode を指すため)
#   - Docker のバインドマウントは shared\ の実パスを使うので一切影響しない
#
# 使い方:
#   .\refresh-user-folders.ps1          # 生成 / 最新化 (冪等。改名・削除も追従)
#   .\refresh-user-folders.ps1 -Remove  # 完全撤去 (リンクのみ削除。shared\ は無傷)
#   .\refresh-user-folders.ps1 -Quiet   # ログ抑制 (install.ps1 から呼ぶ用)
#
# 安全性: ジャンクション削除は「再パースポイントである事を確認してから
#         リンクのみ(非再帰)で外す」。実データを巻き込まない設計。
# ===================================================================
[CmdletBinding()]
param(
    [switch]$Remove,   # 友好フォルダを撤去 (リンクだけ。shared\ の実体は消さない)
    [switch]$Quiet     # 進捗ログを抑制
)

$ErrorActionPreference = 'Stop'
$Root     = $PSScriptRoot
$Shared   = Join-Path $Root 'shared'
$Friendly = Join-Path $Root '_ユーザーファイル'

function Info($m) { if (-not $Quiet) { Write-Host "[*] $m"  -ForegroundColor Cyan } }
function Ok($m)   { if (-not $Quiet) { Write-Host "[OK] $m" -ForegroundColor Green } }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

# --- ジャンクション判定/安全削除 -----------------------------------
function Test-Junction($path) {
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    $item = Get-Item -LiteralPath $path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}
function Remove-Junction($path) {
    # 再パースポイント(=リンク)以外は絶対に消さない。$false=非再帰でリンクのみ外す。
    if (-not (Test-Junction $path)) { throw "ジャンクションではないので削除を拒否: $path" }
    [System.IO.Directory]::Delete($path, $false)
}

# --- 撤去 (リンクのみ。-Recurse をジャンクション越しに走らせない) --
function Remove-FriendlyTree {
    if (-not (Test-Path -LiteralPath $Friendly)) { Info "撤去対象なし ($Friendly は存在しない)"; return }
    foreach ($child in Get-ChildItem -LiteralPath $Friendly -Force) {
        if (Test-Junction $child.FullName) {
            Remove-Junction $child.FullName            # 直下のジャンクション(音声素材 等)
        }
        elseif ($child.PSIsContainer) {
            # 実フォルダ(キャラクター / プロジェクト): 中のジャンクションを外してから空フォルダ削除
            foreach ($g in Get-ChildItem -LiteralPath $child.FullName -Force) {
                if (Test-Junction $g.FullName) { Remove-Junction $g.FullName }
                else { Warn "想定外の非リンクを残置: $($g.FullName)" }
            }
            if (-not (Get-ChildItem -LiteralPath $child.FullName -Force)) {
                Remove-Item -LiteralPath $child.FullName -Force
            } else { Warn "空でないため残置: $($child.FullName)" }
        }
        else {
            Remove-Item -LiteralPath $child.FullName -Force   # _お読みください.txt など
        }
    }
    if (-not (Get-ChildItem -LiteralPath $Friendly -Force)) {
        Remove-Item -LiteralPath $Friendly -Force
        Ok "撤去完了: $Friendly (shared\ の実体は無傷)"
    } else { Warn "空でないため $Friendly は残置" }
}

# --- 名前の整形 / 重複回避 -----------------------------------------
function New-FsSafeName($raw, $fallback) {
    if ([string]::IsNullOrWhiteSpace($raw)) { $raw = $fallback }
    $invalid = [Regex]::Escape(-join [IO.Path]::GetInvalidFileNameChars())
    $name = ([Regex]::Replace($raw, "[$invalid]", '_')).Trim().TrimEnd('.')
    if ([string]::IsNullOrWhiteSpace($name)) { $name = $fallback }
    return $name
}
function Get-UniqueName($desired, $used, $idSuffix) {
    # タイトル衝突時は識別IDを括弧で付けてユニーク化 (取り違え防止)
    $name = $desired
    if ($used.Contains($name.ToLower())) {
        $name = "$desired ($idSuffix)"
        $n = 2
        while ($used.Contains($name.ToLower())) { $name = "$desired ($idSuffix-$n)"; $n++ }
    }
    [void]$used.Add($name.ToLower())
    return $name
}

# --- 1本のジャンクション作成 ---------------------------------------
function Link($linkPath, $target) {
    if (-not (Test-Path -LiteralPath $target)) { Warn "対象が無いのでスキップ: $target"; return }
    New-Item -ItemType Junction -Path $linkPath -Target $target | Out-Null
    Info "  $([IO.Path]::GetFileName($linkPath))  ->  $target"
}

# --- カテゴリ単位の動的リンク (id フォルダ -> JSON の表示名) --------
function Link-Category($subDir, $label, $jsonFile, $nameProp) {
    $srcRoot = Join-Path $Shared $subDir
    if (-not (Test-Path -LiteralPath $srcRoot)) { return }
    $base = Join-Path $Friendly $label
    New-Item -ItemType Directory -Path $base -Force | Out-Null
    $used = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($d in Get-ChildItem -LiteralPath $srcRoot -Directory) {
        $display = $d.Name
        $jf = Join-Path $d.FullName $jsonFile
        if (Test-Path -LiteralPath $jf) {
            try {
                $j = Get-Content -LiteralPath $jf -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($j.$nameProp) { $display = [string]$j.$nameProp }
            } catch { Warn "JSON 読み取り失敗 (フォルダ名で代替): $jf" }
        }
        $safe = New-FsSafeName $display $d.Name
        $uniq = Get-UniqueName $safe $used $d.Name
        Link (Join-Path $base $uniq) $d.FullName
    }
}

# --- 生成本体 -------------------------------------------------------
function Build-FriendlyTree {
    if (-not (Test-Path -LiteralPath $Shared)) { throw "shared\ が見つからない: $Shared" }
    Remove-FriendlyTree                                   # 毎回まっさらから (改名/削除に追従)
    New-Item -ItemType Directory -Path $Friendly -Force | Out-Null

    $readme = @"
このフォルダは自動生成のショートカット集(ジャンクション)です。
  * 中のファイルを追加/削除/編集すると shared\ の実体に直接反映されます。
  * キャラやプロジェクトは「自分が付けた名前」で並びます(識別IDではなく)。

[最新化] キャラ/プロジェクトを追加・改名したら、リポジトリ直下の
         フォルダ整理.cmd  をダブルクリック (または refresh-user-folders.ps1 を実行)。
[撤去]   refresh-user-folders.ps1 -Remove
         (消えるのはこのショートカットだけ。shared\ の実データは残ります)
"@
    Set-Content -LiteralPath (Join-Path $Friendly '_お読みください.txt') -Value $readme -Encoding UTF8

    # 静的カテゴリ (フォルダごと丸ごと)
    Link (Join-Path $Friendly '音声素材')     (Join-Path $Shared 'voices')
    Link (Join-Path $Friendly '自由生成画像') (Join-Path $Shared 'direct_output')

    # 動的カテゴリ (ID フォルダ -> 表示名)  ← 今回の UX 修正の肝
    Link-Category 'characters' 'キャラクター' 'character.json' 'name'
    Link-Category 'projects'   'プロジェクト' 'project.json'   'title'

    Ok "生成完了: $Friendly"
    if (-not $Quiet) { Write-Host "  Explorer で開く: $Friendly" -ForegroundColor Gray }
}

# --- entry ----------------------------------------------------------
if ($Remove) { Remove-FriendlyTree; return }
Build-FriendlyTree
