@echo off
rem ユーザー向け友好フォルダ (_ユーザーファイル\) を最新化する。
rem キャラ/プロジェクトを追加・改名したらダブルクリックするだけ。
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh-user-folders.ps1" %*
echo.
pause
