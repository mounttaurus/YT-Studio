# ===================================================================
# YT-Studio - psassist ホスト常駐ワーカー 起動 (host_worker.py)
#
# director（コンテナ）は Photoshop（ホスト専用）を直接起動できないため、
# この画面を開いたまま作業する（refresh-user-folders.ps1 と同じ「明示的に
# 始めて明示的に終える」運用。常駐サービス化はしない）。
#
# 使い方:
#   .\start-psassist-worker.ps1          # ルート .env の HOST_SHARED_DIR を見張る
#   .\start-psassist-worker.ps1 -Shared "<別リポ>\shared"
#                                         # 別リポの shared を見張る（例: 稼働中の本番）
#
# 終了: このウィンドウを閉じる、または Ctrl+C。
# ===================================================================
[CmdletBinding()]
param(
    [string]$Shared = ""
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot

Write-Host ""
Write-Host "  psassist host_worker" -ForegroundColor White
Write-Host "  repo   : $Root" -ForegroundColor DarkGray
if ($Shared) {
    Write-Host "  shared : $Shared (-Shared 指定)" -ForegroundColor DarkGray
} else {
    Write-Host "  shared : ルート .env の HOST_SHARED_DIR / PSA_SHARED_DIR に従う" -ForegroundColor DarkGray
}
Write-Host ""

$scriptPath = Join-Path $Root 'psassist\scripts\host_worker.py'
if (-not (Test-Path $scriptPath)) {
    Write-Host "[X] host_worker.py が見つかりません: $scriptPath" -ForegroundColor Red
    exit 1
}

$pyArgs = @($scriptPath)
if ($Shared) { $pyArgs += @('--shared', $Shared) }

python @pyArgs
