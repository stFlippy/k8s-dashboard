@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

title Quaber Kubernetes authentication test
set "SCRIPT_DIR=%~dp0"
set "KUBECTL=%SCRIPT_DIR%kubectl.exe"
if not exist "%KUBECTL%" (
  where kubectl.exe >nul 2>&1
  if errorlevel 1 (
    echo [ОШИБКА] kubectl.exe не найден.
    echo Ожидаемый путь: "%SCRIPT_DIR%kubectl.exe"
    echo Проверьте, что расширение файла не стало kubectl.exe.exe.
    pause
    exit /b 2
  )
  set "KUBECTL=kubectl.exe"
)
where curl.exe >nul 2>&1
if errorlevel 1 (
  echo [ОШИБКА] curl.exe не найден. Нужен встроенный curl из Windows 10/11.
  pause
  exit /b 2
)

set "STAMP=%DATE:/=-%_%TIME::=-%"
set "STAMP=%STAMP: =0%"
set "STAMP=%STAMP:.=-%"
set "STAMP=%STAMP:,=-%"
set "RESULT_DIR=%SCRIPT_DIR%quaber-auth-test_%STAMP%_%RANDOM%"
mkdir "%RESULT_DIR%" >nul 2>&1
if errorlevel 1 (
  echo [ОШИБКА] Не удалось создать каталог результатов.
  pause
  exit /b 2
)
set "LOG=%RESULT_DIR%\quaber-auth-test.log"
set "TOKEN_FILE=%RESULT_DIR%\token.tmp"
set "KUBECONFIG_FILE=%USERPROFILE%\.kube\config"

call :log "Quaber authentication diagnostic started"
call :log "Каталог результатов: %RESULT_DIR%"
call :log "Пароль и полный токен в журнал не записываются."
echo.
echo Этот тест ничего не изменяет в Kubernetes: выполняются только GET-запросы.
echo.

:ask_api
set "API_SERVER="
set /p "API_SERVER=API server (например https://cluster.example:6443): "
if not defined API_SERVER goto ask_api
for %%C in (^& ^| ^< ^> ^^) do if not "!API_SERVER:%%C=!"=="!API_SERVER!" (
  echo Недопустимый символ в API URL.
  goto ask_api
)
if /i not "%API_SERVER:~0,8%"=="https://" if /i not "%API_SERVER:~0,7%"=="http://" (
  echo Адрес должен начинаться с https:// или http://
  goto ask_api
)
if "%API_SERVER:~-1%"=="/" set "API_SERVER=%API_SERVER:~0,-1%"

:ask_username
set "LOGIN_NAME="
set /p "LOGIN_NAME=Логин Kubernetes / Dex: "
if not defined LOGIN_NAME goto ask_username
for %%C in (^& ^| ^< ^> ^^) do if not "!LOGIN_NAME:%%C=!"=="!LOGIN_NAME!" (
  echo Недопустимый символ в логине.
  goto ask_username
)

echo.
echo Проверка TLS Kubernetes API:
echo   1. Проверять сертификат ^(рекомендуется^)
echo   2. Не проверять сертификат ^(только для последующих curl-проверок^)
choice /c 12 /n /m "Выберите 1 или 2: "
set "API_TLS=%ERRORLEVEL%"
set "API_CA="
if "%API_TLS%"=="1" (
  set /p "API_CA=Путь к CA API сервера, Enter = системное хранилище сертификатов: "
  if defined API_CA if not exist "!API_CA!" (
    echo [ОШИБКА] Файл CA не найден: !API_CA!
    goto failed
  )
)

echo.
echo Режим результата kubectl login:
echo   1. --onlytoken
echo   2. Полный kubeconfig ^(как в текущем маршруте Quaber^)
choice /c 12 /n /m "Выберите 1 или 2: "
set "LOGIN_MODE=%ERRORLEVEL%"

call :log "API server: %API_SERVER%"
call :log "Логин: %LOGIN_NAME%"
if "%API_TLS%"=="1" (call :log "TLS API: verification enabled") else call :log "TLS API: verification DISABLED"
if "%LOGIN_MODE%"=="1" (call :log "Login result: onlytoken") else call :log "Login result: standard user kubeconfig %KUBECONFIG_FILE%"

echo.
echo Сейчас kubectl запросит пароль. Ввод пароля выполняется самим kubectl и должен быть скрыт.
call :log "Starting kubectl login"

set "LOGIN_COMMON=login --server "%API_SERVER%" --username "%LOGIN_NAME%""
call :log "kubectl login receives only documented flags; authentication discovery is handled by kubectl"

setlocal DisableDelayedExpansion
if "%LOGIN_MODE%"=="1" (
  "%KUBECTL%" %LOGIN_COMMON% --onlytoken 1>"%TOKEN_FILE%"
) else (
  "%KUBECTL%" %LOGIN_COMMON%
)
set "LOGIN_EXIT=%ERRORLEVEL%"
endlocal & set "LOGIN_EXIT=%LOGIN_EXIT%"
if not "%LOGIN_EXIT%"=="0" (
  call :log "FAIL: kubectl login returned exit code %LOGIN_EXIT%"
  goto failed
)
call :log "OK: kubectl login completed"

if "%LOGIN_MODE%"=="2" (
  if not exist "%KUBECONFIG_FILE%" (
    call :log "FAIL: kubectl login did not create %KUBECONFIG_FILE%"
    goto failed
  )
  set "KUBECONFIG=%KUBECONFIG_FILE%"
  "%KUBECTL%" config view --raw --minify -o "jsonpath={.users[0].user.token}" >"%TOKEN_FILE%" 2>>"%LOG%"
)

set "TOKEN="
setlocal DisableDelayedExpansion
for /f "usebackq delims=" %%T in ("%TOKEN_FILE%") do if not defined TOKEN set "TOKEN=%%T"
endlocal & set "TOKEN=%TOKEN%"
if not defined TOKEN (
  call :log "FAIL: token is empty. The kubeconfig may use an exec plugin instead of a static token."
  goto failed
)
for %%L in ("%TOKEN_FILE%") do set "TOKEN_SIZE=%%~zL"
call :log "OK: bearer token received ^(%TOKEN_SIZE% bytes; value hidden^)"

call :log "GET /version (Kubernetes API endpoint check via curl)"
call :curl_get "%API_SERVER%/version" "%RESULT_DIR%\api-version.json"
if not "%HTTP_CODE%"=="200" (
  call :log "FAIL: Kubernetes /version returned HTTP %HTTP_CODE%"
  call :report_http_error "Kubernetes API endpoint check" "%RESULT_DIR%\api-version.json" "%HTTP_CODE%"
  echo.
  echo Указанный адрес не выглядит как рабочий Kubernetes API endpoint.
  echo Проверьте поле server в созданном %%USERPROFILE%%\.kube\config.
  goto failed
)
call :log "OK: Kubernetes /version returned HTTP 200"

call :log "GET /api/v1/namespaces?pretty=true (curl + bearer token)"
call :curl_get "%API_SERVER%/api/v1/namespaces?pretty=true" "%RESULT_DIR%\namespaces.json"
if not "%HTTP_CODE%"=="200" (
  call :log "FAIL: namespaces request returned HTTP %HTTP_CODE%"
  call :report_http_error "Namespace list" "%RESULT_DIR%\namespaces.json" "%HTTP_CODE%"
  goto failed
)
call :log "OK: namespaces request returned HTTP 200"

call :extract_item_names "%RESULT_DIR%\namespaces.json" "%RESULT_DIR%\namespace-names.txt"

echo.
echo Доступные Namespace:
set /a NS_COUNT=0
for /f "usebackq delims=" %%N in ("%RESULT_DIR%\namespace-names.txt") do (
  set /a NS_COUNT+=1
  set "ITEM=%%N"
  set "ITEM=!ITEM:namespace/=!"
  set "NS_!NS_COUNT!=!ITEM!"
  echo   !NS_COUNT!. !ITEM!
)
if !NS_COUNT! EQU 0 (
  call :log "WARN: CMD could not extract Namespace names from pretty JSON; raw response is preserved"
  set /p "NAMESPACE=Не удалось построить список. Введите точное имя Namespace: "
  if not defined NAMESPACE goto failed
  goto namespace_selected
)
:ask_namespace
set "NS_CHOICE="
set /p "NS_CHOICE=Номер Namespace: "
for /f "delims=0123456789" %%X in ("!NS_CHOICE!") do goto bad_namespace
if not defined NS_CHOICE goto bad_namespace
if !NS_CHOICE! LSS 1 goto bad_namespace
if !NS_CHOICE! GTR !NS_COUNT! goto bad_namespace
for %%I in (!NS_CHOICE!) do set "NAMESPACE=!NS_%%I!"
goto namespace_selected
:bad_namespace
echo Введите номер от 1 до !NS_COUNT!.
goto ask_namespace
:namespace_selected
call :log "Selected namespace: %NAMESPACE%"

call :log "GET /api/v1/namespaces/%NAMESPACE%/pods?pretty=true (curl + bearer token)"
call :curl_get "%API_SERVER%/api/v1/namespaces/%NAMESPACE%/pods?pretty=true" "%RESULT_DIR%\pods.json"
if not "%HTTP_CODE%"=="200" (
  call :log "FAIL: pod list request returned HTTP %HTTP_CODE%"
  call :report_http_error "Pod list" "%RESULT_DIR%\pods.json" "%HTTP_CODE%"
  goto failed
)
call :log "OK: pod list request returned HTTP 200"

call :extract_item_names "%RESULT_DIR%\pods.json" "%RESULT_DIR%\pod-names.txt"

echo.
echo Pods в %NAMESPACE%:
set /a POD_COUNT=0
for /f "usebackq delims=" %%P in ("%RESULT_DIR%\pod-names.txt") do (
  set /a POD_COUNT+=1
  set "ITEM=%%P"
  set "ITEM=!ITEM:pod/=!"
  set "POD_!POD_COUNT!=!ITEM!"
  echo   !POD_COUNT!. !ITEM!
)
if !POD_COUNT! EQU 0 (
  call :log "WARN: no Pod names were extracted; raw response is preserved"
  set /p "POD_NAME=Введите точное имя Pod или оставьте пустым для завершения: "
  if not defined POD_NAME goto failed
  goto pod_selected
)
:ask_pod
set "POD_CHOICE="
set /p "POD_CHOICE=Номер Pod для проверки метрик: "
for /f "delims=0123456789" %%X in ("!POD_CHOICE!") do goto bad_pod
if not defined POD_CHOICE goto bad_pod
if !POD_CHOICE! LSS 1 goto bad_pod
if !POD_CHOICE! GTR !POD_COUNT! goto bad_pod
for %%I in (!POD_CHOICE!) do set "POD_NAME=!POD_%%I!"
goto pod_selected
:bad_pod
echo Введите номер от 1 до !POD_COUNT!.
goto ask_pod
:pod_selected
call :log "Selected Pod: %NAMESPACE%/%POD_NAME%"

call :log "GET /api/v1/namespaces/%NAMESPACE%/pods/%POD_NAME% (curl + bearer token)"
call :curl_get "%API_SERVER%/api/v1/namespaces/%NAMESPACE%/pods/%POD_NAME%" "%RESULT_DIR%\selected-pod.json"
if not "%HTTP_CODE%"=="200" (
  call :log "FAIL: selected Pod request returned HTTP %HTTP_CODE%"
  call :report_http_error "Selected Pod" "%RESULT_DIR%\selected-pod.json" "%HTTP_CODE%"
  goto failed
)
call :log "OK: selected Pod request returned HTTP 200"

set "NODE_NAME="
findstr /C:"\"nodeName\":" "%RESULT_DIR%\selected-pod.json" >"%RESULT_DIR%\node-line.tmp" 2>nul
for /f "usebackq tokens=2 delims=:" %%N in ("%RESULT_DIR%\node-line.tmp") do if not defined NODE_NAME set "NODE_NAME=%%N"
set "NODE_NAME=!NODE_NAME:"=!"
set "NODE_NAME=!NODE_NAME:,=!"
set "NODE_NAME=!NODE_NAME: =!"
if defined NODE_NAME (call :log "Pod node: %NODE_NAME%") else call :log "Pod is not assigned to a node; kubelet fallback cannot be checked"

call :log "GET /apis/metrics.k8s.io/v1beta1/namespaces/%NAMESPACE%/pods/%POD_NAME% (curl + bearer token)"
call :curl_get "%API_SERVER%/apis/metrics.k8s.io/v1beta1/namespaces/%NAMESPACE%/pods/%POD_NAME%" "%RESULT_DIR%\pod-metrics-api.json"
set "METRICS_CODE=%HTTP_CODE%"
if "%METRICS_CODE%"=="200" (
  findstr /C:"\"containers\":" "%RESULT_DIR%\pod-metrics-api.json" >nul 2>&1
  if errorlevel 1 (
    set "METRICS_CODE=HTTP 200, but response has no containers"
    call :log "INFO: Metrics API returned HTTP 200 but its data was not usable; Quaber switches to kubelet stats/summary"
  ) else (
    call :log "OK: Metrics API returned usable data; Quaber uses Metrics API for CPU/RAM"
  )
)
if not "!METRICS_CODE!"=="200" (
  call :log "INFO: Metrics API returned HTTP %METRICS_CODE%; Quaber switches to kubelet stats/summary"
  if defined NODE_NAME (
    call :log "GET /api/v1/nodes/%NODE_NAME%/proxy/stats/summary (curl + bearer token)"
    call :curl_get "%API_SERVER%/api/v1/nodes/%NODE_NAME%/proxy/stats/summary" "%RESULT_DIR%\kubelet-stats-summary.json"
    if "!HTTP_CODE!"=="200" (call :log "OK: kubelet stats/summary returned HTTP 200") else call :log "FAIL: kubelet stats/summary returned HTTP !HTTP_CODE!"
  )
)

if defined NODE_NAME (
  call :log "GET /api/v1/nodes/%NODE_NAME%/proxy/metrics/cadvisor (curl + bearer token)"
  call :curl_get "%API_SERVER%/api/v1/nodes/%NODE_NAME%/proxy/metrics/cadvisor" "%RESULT_DIR%\cadvisor-metrics.txt"
  if "!HTTP_CODE!"=="200" (call :log "OK: cAdvisor returned HTTP 200; CPU throttling source is available") else call :log "INFO: cAdvisor returned HTTP !HTTP_CODE!; CPU throttling is unavailable"
)

del /q "%TOKEN_FILE%" >nul 2>&1
del /q "%RESULT_DIR%\node-line.tmp" >nul 2>&1
call :log "SUCCESS: authentication and read-only Quaber API paths were checked"
echo.
echo Проверка завершена успешно.
echo Журнал: "%LOG%"
echo Ответы API: "%RESULT_DIR%"
if "%LOGIN_MODE%"=="2" echo ВНИМАНИЕ: kubectl login обновил стандартный файл "%KUBECONFIG_FILE%".
pause
exit /b 0

:curl_get
set "HTTP_CODE=000"
if "%API_TLS%"=="2" (
  for /f "delims=" %%H in ('curl.exe -k -sS --connect-timeout 10 --max-time 30 -D "%~2.headers.txt" -H "Accept: application/json" -H "Authorization: Bearer %TOKEN%" -o "%~2" -w "%%{http_code}" "%~1" 2^>^>"%LOG%"') do set "HTTP_CODE=%%H"
) else if defined API_CA (
  for /f "delims=" %%H in ('curl.exe --cacert "%API_CA%" -sS --connect-timeout 10 --max-time 30 -D "%~2.headers.txt" -H "Accept: application/json" -H "Authorization: Bearer %TOKEN%" -o "%~2" -w "%%{http_code}" "%~1" 2^>^>"%LOG%"') do set "HTTP_CODE=%%H"
) else (
  for /f "delims=" %%H in ('curl.exe -sS --connect-timeout 10 --max-time 30 -D "%~2.headers.txt" -H "Accept: application/json" -H "Authorization: Bearer %TOKEN%" -o "%~2" -w "%%{http_code}" "%~1" 2^>^>"%LOG%"') do set "HTTP_CODE=%%H"
)
exit /b 0

:report_http_error
echo.
echo [HTTP ОШИБКА] %~1: HTTP %~3
>>"%LOG%" echo [HTTP ERROR] %~1: HTTP %~3
echo Headers: "%~2.headers.txt"
echo Body: "%~2"
echo Первые строки ответа:
set /a ERROR_LINES=0
if exist "%~2" for /f "usebackq delims=" %%E in ("%~2") do if !ERROR_LINES! LSS 8 (
  set "ERROR_TEXT=%%E"
  echo(!ERROR_TEXT:~0,500!
  >>"%LOG%" echo RESPONSE: !ERROR_TEXT:~0,500!
  set /a ERROR_LINES+=1
)
if !ERROR_LINES! EQU 0 echo ^(тело ответа пустое^)
exit /b 0

:extract_item_names
set "NAME_LINES=%RESULT_DIR%\name-lines.tmp"
findstr /R /C:"^        \"name\":" "%~1" >"!NAME_LINES!" 2>nul
>"%~2" (
  for /f "usebackq tokens=2 delims=:" %%N in ("!NAME_LINES!") do (
    set "ITEM=%%N"
    set "ITEM=!ITEM:"=!"
    set "ITEM=!ITEM:,=!"
    set "ITEM=!ITEM: =!"
    if defined ITEM echo !ITEM!
  )
)
del /q "!NAME_LINES!" >nul 2>&1
exit /b 0

:log
echo [%DATE% %TIME%] %~1
>>"%LOG%" echo [%DATE% %TIME%] %~1
exit /b 0

:failed
del /q "%TOKEN_FILE%" >nul 2>&1
del /q "%RESULT_DIR%\node-line.tmp" >nul 2>&1
echo.
echo Проверка завершилась с ошибкой. Смотрите журнал:
echo "%LOG%"
pause
exit /b 1
