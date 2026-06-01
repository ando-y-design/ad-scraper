@echo off
cd /d "%~dp0"

:: 起動直後はネットワークが不安定なため少し待機
timeout /t 30 /nobreak >nul

:: 最新コードを取得
git pull

:: スクレイパーを起動（venv優先、なければシステムPython）
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe main.py
) else (
    python main.py
)
