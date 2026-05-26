@echo off
REM 自律改善エンジン — タスクスケジューラー用ラッパー
REM このファイルを 7:00 / 23:00 に実行するタスクを登録する
REM
REM タスクスケジューラーへの登録例:
REM   schtasks /create /tn "AdScraperImprove_Morning" /tr "C:\Users\amdwt\ad_scraper\scripts\auto_improve.bat" /sc DAILY /st 07:00 /ru SYSTEM /f
REM   schtasks /create /tn "AdScraperImprove_Evening" /tr "C:\Users\amdwt\ad_scraper\scripts\auto_improve.bat" /sc DAILY /st 23:00 /ru SYSTEM /f

setlocal

set AD_SCRAPER_DIR=C:\Users\amdwt\ad_scraper
set VENV_PYTHON=%AD_SCRAPER_DIR%\venv\Scripts\python.exe
set LOG_DIR=%AD_SCRAPER_DIR%\logs
set TIMESTAMP=%date:~0,4%-%date:~5,2%-%date:~8,2%_%time:~0,2%-%time:~3,2%

REM ログディレクトリ確認
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM 出力先ログ
set RUN_LOG=%LOG_DIR%\improve_run_%TIMESTAMP: =0%.log

echo ============================================================ >> "%RUN_LOG%"
echo 自律改善エンジン 起動: %date% %time% >> "%RUN_LOG%"
echo ============================================================ >> "%RUN_LOG%"

REM venv Pythonの存在確認
if not exist "%VENV_PYTHON%" (
    echo ERROR: venv Python が見つかりません: %VENV_PYTHON% >> "%RUN_LOG%"
    exit /b 1
)

REM ad_scraper ディレクトリに移動して実行
cd /d "%AD_SCRAPER_DIR%"
"%VENV_PYTHON%" -m utils.auto_improver >> "%RUN_LOG%" 2>&1

echo. >> "%RUN_LOG%"
echo 終了: %date% %time% (exit code: %errorlevel%) >> "%RUN_LOG%"

REM 古いログを削除（30日以上前のファイル）
forfiles /p "%LOG_DIR%" /m "improve_run_*.log" /d -30 /c "cmd /c del @path" 2>nul

endlocal
