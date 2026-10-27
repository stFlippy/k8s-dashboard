@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Quaber OpenLens curl test

rem Flow: optional kubectl login -> kubeconfig -> kubectl proxy -> curl.
rem kubectl proxy authenticates upstream requests with the active kubeconfig.

set "SCRIPT_DIR=%~dp0"
set "WORKDIR_PUSHED=0"
set "KUBECTL=kubectl.exe"
if not exist "%SCRIPT_DIR%kubectl.exe" (
  where kubectl.exe >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] kubectl.exe was not found next to this script or in PATH.
    goto fatal
  )
)
pushd "%SCRIPT_DIR%" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot enter the script directory: "%SCRIPT_DIR%"
  goto fatal
)
set "WORKDIR_PUSHED=1"

where curl.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows curl.exe was not found in PATH.
  goto fatal
)
"%KUBECTL%" proxy --help >nul 2>&1
if errorlevel 1 (
  echo [ERROR] This kubectl.exe does not support: kubectl proxy
  goto fatal
)

set "RESULT_DIR=%TEMP%\quaber-openlens-%RANDOM%-%RANDOM%"
mkdir "%RESULT_DIR%" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot create result directory: "%RESULT_DIR%"
  goto fatal
)

set "LOG=%RESULT_DIR%\test.log"
set "PROXY_LOG=%RESULT_DIR%\kubectl-proxy.log"
set "HTTP_CODE_FILE=%RESULT_DIR%\http-code.tmp"
set "NAMESPACE_JSON=%RESULT_DIR%\namespaces.json"
set "NAMESPACE_NAMES=%RESULT_DIR%\namespaces.txt"
set "PODS_JSON=%RESULT_DIR%\pods.json"
set "POD_NAMES=%RESULT_DIR%\pods.txt"
set "PROXY_PID="
set "PROXY_STARTED=0"
set "DID_LOGIN=0"
set "HAVE_NAMESPACE_LIST=0"
set "EXIT_CODE=1"
set /a PORT=18080+(%RANDOM% %% 1000)

call :log "START"
call :log "Results: %RESULT_DIR%"
if defined KUBECONFIG (call :log "KUBECONFIG path: %KUBECONFIG%") else call :log "KUBECONFIG path: Windows user default"

echo.
echo The test uses the existing kubeconfig unless you request kubectl login.
echo Kubernetes requests are read-only GET requests through localhost proxy.
echo.
choice /C YN /N /M "Run kubectl login now? [Y/N]: "
if errorlevel 2 goto login_complete
call :perform_login
if errorlevel 1 goto finish
set "DID_LOGIN=1"

:login_complete
call :load_context
if not errorlevel 1 goto context_ready

:offer_login
if "%DID_LOGIN%"=="1" goto finish
echo Current kubeconfig has no usable active context.
choice /C YN /N /M "Refresh it with kubectl login? [Y/N]: "
if errorlevel 2 goto finish
call :perform_login
if errorlevel 1 goto finish
set "DID_LOGIN=1"
call :load_context
if errorlevel 1 goto finish

:context_ready
call :start_proxy
if errorlevel 1 goto finish

:fetch_namespaces
call :http_get "http://127.0.0.1:%PORT%/api/v1/namespaces?pretty=true" "%NAMESPACE_JSON%" "Namespace list"
if "%HTTP_CODE%"=="200" goto namespaces_received
if "%HTTP_CODE%"=="401" goto refresh_after_unauthorized
if "%HTTP_CODE%"=="403" goto namespace_list_forbidden
echo Namespace request failed with HTTP %HTTP_CODE%.
if exist "%NAMESPACE_JSON%" type "%NAMESPACE_JSON%"
echo.
echo Namespace enumeration failed, but a request to a known Namespace may still work.
goto manual_namespace

:refresh_after_unauthorized
call :log "Bearer token is unauthorized"
if "%DID_LOGIN%"=="1" (
  echo kubectl login completed, but Kubernetes still returned HTTP 401.
  goto finish
)
echo Existing kubeconfig token has expired or is unauthorized.
choice /C YN /N /M "Refresh authentication with kubectl login? [Y/N]: "
if errorlevel 2 goto finish
call :stop_proxy
call :perform_login
if errorlevel 1 goto finish
set "DID_LOGIN=1"
call :load_context
if errorlevel 1 goto finish
call :start_proxy
if errorlevel 1 goto finish
goto fetch_namespaces

:namespace_list_forbidden
call :log "RBAC denied list namespaces"
echo.
echo Kubernetes returned HTTP 403 for list namespaces.
echo This user cannot enumerate Namespace objects, so a complete numbered list is unavailable.
if exist "%NAMESPACE_JSON%" type "%NAMESPACE_JSON%"
echo.
goto manual_namespace

:namespaces_received
call :extract_names "%NAMESPACE_JSON%" "%NAMESPACE_NAMES%"
if "%ITEM_COUNT%"=="0" (
  echo Kubernetes returned no Namespace names.
  goto finish
)
set "HAVE_NAMESPACE_LIST=1"
echo.
echo Available Namespaces:
call :print_numbered "%NAMESPACE_NAMES%"

:choose_namespace
set "NS_CHOICE="
set /p "NS_CHOICE=Select Namespace number: "
call :select_numbered "%NAMESPACE_NAMES%" "%NS_CHOICE%"
if not defined NAMESPACE (
  echo Select one of the displayed numbers.
  goto choose_namespace
)
goto fetch_pods

:manual_namespace
set "NAMESPACE="
set /p "NAMESPACE=Enter an accessible Namespace, or press Enter to stop: "
if not defined NAMESPACE goto finish

:fetch_pods
call :log "Selected Namespace: %NAMESPACE%"
call :http_get "http://127.0.0.1:%PORT%/api/v1/namespaces/%NAMESPACE%/pods?pretty=true" "%PODS_JSON%" "Pod list for %NAMESPACE%"
if "%HTTP_CODE%"=="200" goto pods_received
if "%HTTP_CODE%"=="403" (
  echo.
  echo This user cannot list Pods in Namespace "%NAMESPACE%".
  if exist "%PODS_JSON%" type "%PODS_JSON%"
  echo.
  set "NAMESPACE="
  if "%HAVE_NAMESPACE_LIST%"=="1" goto choose_namespace
  goto manual_namespace
)
if "%HTTP_CODE%"=="401" (
  echo Authentication became unauthorized. Run the script again and choose login.
  goto finish
)
echo Pod request failed with HTTP %HTTP_CODE%.
if exist "%PODS_JSON%" type "%PODS_JSON%"
goto finish

:pods_received
call :extract_names "%PODS_JSON%" "%POD_NAMES%"
echo.
echo Pods in Namespace "%NAMESPACE%":
if "%ITEM_COUNT%"=="0" (
  echo ^(no Pods^)
) else (
  type "%POD_NAMES%"
)
echo.
echo Namespace export: "%NAMESPACE_NAMES%"
echo Pod export: "%POD_NAMES%"
call :log "SUCCESS: Namespaces selected and Pods retrieved through proxy with curl"
set "EXIT_CODE=0"
goto finish

:perform_login
set "API_SERVER="
set /p "API_SERVER=Kubernetes API URL: "
set "LOGIN_NAME="
set /p "LOGIN_NAME=Kubernetes / Dex username: "
call :log "Running kubectl login for API: %API_SERVER%"
"%KUBECTL%" login "%API_SERVER%" --username="%LOGIN_NAME%"
if errorlevel 1 (
  call :log "FAIL: kubectl login failed"
  exit /b 1
)
call :log "OK: kubectl login completed"
exit /b 0

:load_context
set "CURRENT_CONTEXT="
for /f "usebackq delims=" %%C in (`"%KUBECTL%" config current-context 2^>nul`) do if not defined CURRENT_CONTEXT set "CURRENT_CONTEXT=%%C"
if not defined CURRENT_CONTEXT (
  call :log "FAIL: kubeconfig has no current context"
  exit /b 1
)
set "CURRENT_USER="
for /f "usebackq delims=" %%U in (`"%KUBECTL%" config view --minify -o "jsonpath={.contexts[0].context.user}" 2^>nul`) do if not defined CURRENT_USER set "CURRENT_USER=%%U"
call :log "Current context: %CURRENT_CONTEXT%"
if defined CURRENT_USER call :log "Kubeconfig user reference: %CURRENT_USER%"
echo Active context: %CURRENT_CONTEXT%
if defined CURRENT_USER echo Kubeconfig user: %CURRENT_USER%
exit /b 0

:start_proxy
set "PROXY_PID="
set "PROXY_STARTED=0"
type nul >"%PROXY_LOG%"
start "" /b kubectl.exe proxy --address=127.0.0.1 --port=%PORT% 1>"%PROXY_LOG%" 2>&1
if errorlevel 1 (
  call :log "FAIL: Windows could not start kubectl proxy"
  exit /b 1
)
set "PROXY_STARTED=1"
set "ATTEMPT=0"
:wait_proxy
call :find_proxy_pid
if defined PROXY_PID (
  call :log "Proxy listening at 127.0.0.1:%PORT%; PID %PROXY_PID%"
  exit /b 0
)
set /a ATTEMPT+=1
if %ATTEMPT% GEQ 15 (
  call :log "FAIL: kubectl proxy did not start"
  if exist "%PROXY_LOG%" type "%PROXY_LOG%"
  exit /b 1
)
ping.exe -n 2 127.0.0.1 >nul
goto wait_proxy

:http_get
set "HTTP_CODE=000"
if exist "%HTTP_CODE_FILE%" del /q "%HTTP_CODE_FILE%" >nul 2>&1
curl.exe --noproxy "*" --silent --show-error --header "Accept: application/json" --connect-timeout 5 --max-time 60 --output "%~2" --write-out "%%{http_code}" "%~1" 1>"%HTTP_CODE_FILE%" 2>>"%LOG%"
if exist "%HTTP_CODE_FILE%" set /p "HTTP_CODE="<"%HTTP_CODE_FILE%"
if not defined HTTP_CODE set "HTTP_CODE=000"
call :log "%~3: HTTP %HTTP_CODE%"
if not "%HTTP_CODE%"=="200" if exist "%~2" (
  >>"%LOG%" echo ----- %~3 response body -----
  >>"%LOG%" type "%~2"
  >>"%LOG%" echo.
  >>"%LOG%" echo ----- end response body -----
)
exit /b 0

:extract_names
setlocal EnableDelayedExpansion
set "COUNT=0"
>"%~2" (
  for /f "usebackq tokens=2 delims=:" %%N in (`findstr.exe /R /C:"^        \"name\":" "%~1" 2^>nul`) do (
    set "ITEM=%%N"
    set "ITEM=!ITEM:"=!"
    set "ITEM=!ITEM:,=!"
    set "ITEM=!ITEM: =!"
    if defined ITEM (
      set /a COUNT+=1
      echo(!ITEM!
    )
  )
)
endlocal & set "ITEM_COUNT=%COUNT%"
exit /b 0

:print_numbered
setlocal EnableDelayedExpansion
set /a INDEX=0
for /f "usebackq delims=" %%N in ("%~1") do (
  set /a INDEX+=1
  echo   !INDEX!. %%N
)
endlocal
exit /b 0

:select_numbered
set "NAMESPACE="
setlocal EnableDelayedExpansion
set "TARGET=%~2"
set "FOUND="
set /a INDEX=0
for /f "usebackq delims=" %%N in ("%~1") do (
  set /a INDEX+=1
  if "!INDEX!"=="!TARGET!" set "FOUND=%%N"
)
endlocal & set "NAMESPACE=%FOUND%"
exit /b 0

:find_proxy_pid
set "PROXY_PID="
for /f "tokens=2,5" %%A in ('netstat.exe -ano -p tcp 2^>nul ^| findstr.exe /C:"127.0.0.1:%PORT%"') do if not defined PROXY_PID if "%%A"=="127.0.0.1:%PORT%" set "PROXY_PID=%%B"
exit /b 0

:stop_proxy
if not defined PROXY_PID call :find_proxy_pid
if defined PROXY_PID taskkill.exe /PID %PROXY_PID% /T /F >nul 2>&1
set "PROXY_STARTED=0"
set "PROXY_PID="
exit /b 0

:finish
if "%PROXY_STARTED%"=="1" call :stop_proxy
if exist "%HTTP_CODE_FILE%" del /q "%HTTP_CODE_FILE%" >nul 2>&1
if exist "%NAMESPACE_JSON%" del /q "%NAMESPACE_JSON%" >nul 2>&1
if exist "%PODS_JSON%" del /q "%PODS_JSON%" >nul 2>&1
echo.
if "%EXIT_CODE%"=="0" echo [OK] Namespace and Pod lists were retrieved through proxy with curl.
if not "%EXIT_CODE%"=="0" echo [FAIL] Test did not complete.
echo Log: "%LOG%"
echo Result directory: "%RESULT_DIR%"
echo No password or bearer token was read or written by this script.
if "%WORKDIR_PUSHED%"=="1" popd
pause
exit /b %EXIT_CODE%

:log
echo [%DATE% %TIME%] %~1
>>"%LOG%" echo [%DATE% %TIME%] %~1
exit /b 0

:fatal
if "%WORKDIR_PUSHED%"=="1" popd
echo.
pause
exit /b 2
