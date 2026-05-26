@echo off
echo =============================================
echo  広告収集ツール セットアップ (管理者権限用)
echo =============================================
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

schtasks /create ^
  /tn "AdScraper" ^
  /tr "\"C:\Users\amdwt\ad_scraper\venv\Scripts\python.exe\" \"C:\Users\amdwt\ad_scraper\main.py\"" ^
  /sc ONSTART ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

schtasks /create ^
  /tn "AdScraper_Daily" ^
  /tr "\"C:\Users\amdwt\ad_scraper\venv\Scripts\python.exe\" \"C:\Users\amdwt\ad_scraper\main.py\"" ^
  /sc DAILY ^
  /st 05:00 ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

echo     完了: 起動時・毎日AM5:00に自動起動登録

echo.
echo =============================================
echo  セットアップ完了！
echo =============================================
echo.
pause
