@echo off
echo =============================================
echo  広告収集ツール セットアップ (管理者権限用)
echo =============================================
echo.

:: このbatファイルがあるディレクトリ（末尾の\を除去）
set "AD_DIR=%~dp0"
if "%AD_DIR:~-1%"=="\" set "AD_DIR=%AD_DIR:~0,-1%"
echo 作業ディレクトリ: %AD_DIR%
echo.

echo [1/2] 省電力設定を適用中...
powercfg /h off
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 0
echo     完了: スリープ・ハイバネーション無効化

echo.
echo [2/2] Task Scheduler登録中...
schtasks /delete /tn "AdScraper" /f >nul 2>&1
schtasks /delete /tn "AdScraper_Daily" /f >nul 2>&1

schtasks /create ^
  /tn "AdScraper" ^
  /tr "cmd /c \"%AD_DIR%\auto_run.bat\"" ^
  /sc ONLOGON ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

schtasks /create ^
  /tn "AdScraper_Daily" ^
  /tr "cmd /c \"%AD_DIR%\auto_run.bat\"" ^
  /sc DAILY ^
  /st 05:00 ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

echo     完了: ログオン時・毎日AM5:00に自動起動登録

echo.
echo =============================================
echo  セットアップ完了！
echo =============================================
echo.
echo PC起動・ログオン後と毎朝5:00に自動でgit pull + スクレイパーが動きます。
echo.
pause
