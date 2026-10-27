@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "ORIGINAL_CP="
for /f "tokens=2 delims=:" %%C in ('chcp') do for /f "tokens=* delims= " %%D in ("%%C") do set "ORIGINAL_CP=%%D"
if not defined ORIGINAL_CP (
  echo Could not detect the console code page.
  goto :early_error
)
chcp 65001 >nul
if errorlevel 1 (
  echo Could not enable UTF-8 in the console.
  goto :early_error
)
set "UTF8_ACTIVE=1"
rem UTF-8 messages. The original code page is restored; window size is unchanged.
set "SCRIPT_DIR=%~dp0"
set "KUBECTL=%SCRIPT_DIR%kubectl.exe"
if not exist "%KUBECTL%" set "KUBECTL=kubectl.exe"
where "%KUBECTL%" >nul 2>&1
if errorlevel 1 (
  echo kubectl.exe не найден рядом со скриптом или в PATH.
  goto :early_error
)
where curl.exe >nul 2>&1
if errorlevel 1 (
  echo curl.exe не найден в PATH.
  goto :early_error
)
where netstat.exe >nul 2>&1
if errorlevel 1 (
  echo netstat.exe не найден в PATH.
  goto :early_error
)
where taskkill.exe >nul 2>&1
if errorlevel 1 (
  echo taskkill.exe не найден в PATH.
  goto :early_error
)
set "STAMP=%DATE:~-10% %TIME:~0,8%"
set "STAMP=!STAMP:/=-!"
set "STAMP=!STAMP:.=-!"
set "STAMP=!STAMP::=-!"
set "STAMP=!STAMP:,=-!"
set "STAMP=!STAMP: =_!"
set "DEFAULT_NAME=result !STAMP!.csv"
set "LOG_FILE=%SCRIPT_DIR%result !STAMP!.log"
set "WORK_DIR=%TEMP%\deployment-export-%RANDOM%-%RANDOM%"
if exist "%WORK_DIR%" goto :early_error
mkdir "%WORK_DIR%" || goto :early_error
set "HOST_FILE=%WORK_DIR%\hosts.txt"
type nul > "%HOST_FILE%"
set /a HOST_COUNT=0,ROW_COUNT=0,ERROR_COUNT=0,SUCCESS_COUNT=0
set "LAST_LOGIN="
call :log "Начата выгрузка Deployment и CronJob"

:choose_hosts
echo.
echo 1 - Ввести адреса API вручную
echo 2 - Прочитать адреса API из hosts.cfg
set "CHOICE="
set /p "CHOICE=Выберите 1 или 2: "
if "!CHOICE!"=="1" goto :manual_hosts
if "!CHOICE!"=="2" goto :file_hosts
goto :choose_hosts

:manual_hosts
echo Вводите по одному адресу API в строке. Пустая строка завершит ввод.
:manual_next
set "RAW_HOST="
set /p "RAW_HOST=Адрес API: "
if not defined RAW_HOST if !HOST_COUNT! GTR 0 goto :hosts_loaded
if not defined RAW_HOST (
  echo Нужно указать хотя бы один адрес.
  goto :manual_next
)
call :add_host
goto :manual_next

:file_hosts
if not exist "%SCRIPT_DIR%hosts.cfg" (
  call :log "Файл hosts.cfg не найден"
  goto :finish_error
)
rem FOR /F counts actual nonempty lines; a final newline is not a new host.
for /f "usebackq delims=" %%H in ("%SCRIPT_DIR%hosts.cfg") do (
  set "RAW_HOST=%%H"
  call :add_host
)

:hosts_loaded
if !HOST_COUNT! EQU 0 (
  call :log "Не загружено ни одного допустимого адреса API"
  goto :finish_error
)
call :log "Загружено адресов API: !HOST_COUNT!."
for /f "usebackq delims=" %%H in ("%HOST_FILE%") do call :log "  %%H"

:choose_name
echo.
set "FILE_NAME="
set /p "FILE_NAME=Имя CSV-файла [!DEFAULT_NAME!]: "
if not defined FILE_NAME set "FILE_NAME=!DEFAULT_NAME!"
echo(!FILE_NAME!| findstr /R /C:"^[A-Za-z0-9 _.-][A-Za-z0-9 _.-]*$" >nul
if errorlevel 1 goto :invalid_name
if /i not "!FILE_NAME:~-4!"==".csv" set "FILE_NAME=!FILE_NAME!.csv"
if "!FILE_NAME:~0,1!"=="." goto :invalid_name
for %%F in ("!FILE_NAME!") do set "BASE_NAME=%%~nF"
for %%R in (CON PRN AUX NUL COM1 COM2 COM3 COM4 COM5 COM6 COM7 COM8 COM9 LPT1 LPT2 LPT3 LPT4 LPT5 LPT6 LPT7 LPT8 LPT9) do if /i "!BASE_NAME!"=="%%R" goto :invalid_name
set "CSV_FILE=%SCRIPT_DIR%!FILE_NAME!"
if exist "!CSV_FILE!" (
  echo Файл уже существует: !CSV_FILE!
  goto :choose_name
)
>"!CSV_FILE!" echo "HOST","NAMESPACE","KIND","NAME","CONTAINER_IMAGES","branch","deployed by","version"
if errorlevel 1 goto :finish_error
call :log "CSV: !CSV_FILE!"
goto :process_hosts

:invalid_name
echo Недопустимое имя файла. Разрешены латинские буквы, цифры, пробел, точка, дефис и подчёркивание; путь указывать нельзя.
goto :choose_name

:process_hosts
set /a HOST_INDEX=0
for /f "usebackq delims=" %%H in ("%HOST_FILE%") do (
  set "CURRENT_HOST=%%H"
  set /a HOST_INDEX+=1
  call :process_host
)
if !SUCCESS_COUNT! EQU 0 goto :finish_error
if !ERROR_COUNT! GTR 0 (
  call :log "Выгрузка завершена с ошибками: !ERROR_COUNT!. Строк CSV: !ROW_COUNT!."
  set "EXIT_CODE=2"
) else (
  call :log "Выгрузка завершена. Ресурсов: !ROW_COUNT!."
  set "EXIT_CODE=0"
)
goto :finish

:finish_error
call :log "Выгрузка остановлена. Успешных хостов: !SUCCESS_COUNT!, ошибок: !ERROR_COUNT!."
set "EXIT_CODE=1"
:finish
if exist "%WORK_DIR%" rmdir /s /q "%WORK_DIR%"
if defined CSV_FILE echo CSV: !CSV_FILE!
echo Журнал: !LOG_FILE!
echo.
if defined ORIGINAL_CP chcp !ORIGINAL_CP! >nul
pause
exit /b !EXIT_CODE!

:early_error
if "!UTF8_ACTIVE!"=="1" (echo Не удалось запустить выгрузку.) else echo Export could not start.
if defined ORIGINAL_CP chcp !ORIGINAL_CP! >nul
pause
exit /b 1

:add_host
for /f "tokens=* delims= " %%A in ("!RAW_HOST!") do set "RAW_HOST=%%A"
:trim_host
if "!RAW_HOST:~-1!"==" " (
  set "RAW_HOST=!RAW_HOST:~0,-1!"
  goto :trim_host
)
if not defined RAW_HOST exit /b
if "!RAW_HOST:~0,1!"=="#" exit /b
if "!RAW_HOST:~0,1!"==";" exit /b
if /i "!RAW_HOST:~0,8!"=="https://" (
  set "AUTHORITY=!RAW_HOST:~8!"
  set "SCHEME=https://"
) else if /i "!RAW_HOST:~0,7!"=="http://" (
  set "AUTHORITY=!RAW_HOST:~7!"
  set "SCHEME=http://"
) else (
  set "AUTHORITY=!RAW_HOST!"
  set "SCHEME=https://"
)
if "!AUTHORITY:~-1!"=="/" set "AUTHORITY=!AUTHORITY:~0,-1!"
echo(!AUTHORITY!| findstr /R /I /C:"^[A-Z0-9][A-Z0-9.:-]*$" >nul
if errorlevel 1 (
  call :log "Пропущен недопустимый адрес API: !RAW_HOST!"
  exit /b
)
set "NORMAL_HOST=!SCHEME!!AUTHORITY!"
findstr /L /I /X /C:"!NORMAL_HOST!" "%HOST_FILE%" >nul 2>&1
if not errorlevel 1 exit /b
>>"%HOST_FILE%" echo(!NORMAL_HOST!
set /a HOST_COUNT+=1
exit /b

:process_host
call :log "Обработка !CURRENT_HOST!"
if defined LAST_LOGIN (
  echo Предыдущий логин: !LAST_LOGIN!
  choice /C 12 /N /M "1 - оставить логин, 2 - ввести другой: "
  if errorlevel 2 (
    set "LOGIN_NAME="
  ) else (
    set "LOGIN_NAME=!LAST_LOGIN!"
    call :log "Повторно используется логин !LOGIN_NAME!"
  )
) else (
  set "LOGIN_NAME="
)
:read_login
if not defined LOGIN_NAME set /p "LOGIN_NAME=Логин Kubernetes / Dex: "
if not defined LOGIN_NAME goto :read_login
set "LAST_LOGIN=!LOGIN_NAME!"
set "LOGIN_CONFIG=%WORK_DIR%\login-!HOST_INDEX!.yaml"
rem A valid empty config keeps each host isolated and prevents CreateFile errors.
> "!LOGIN_CONFIG!" (
  echo apiVersion: v1
  echo kind: Config
  echo preferences: {}
  echo clusters: []
  echo contexts: []
  echo users: []
  echo current-context: ""
)
if not exist "!LOGIN_CONFIG!" (
  call :log "Не удалось создать отдельный kubeconfig для !CURRENT_HOST!"
  set /a ERROR_COUNT+=1
  exit /b
)
rem Some kubectl login wrappers read KUBECONFIG rather than the command flag.
set "KUBECONFIG=!LOGIN_CONFIG!"
echo Пароль запросит kubectl; ввод не будет отображаться.
"%KUBECTL%" login "!CURRENT_HOST!" --username "!LOGIN_NAME!" --kubeconfig "!LOGIN_CONFIG!"
if errorlevel 1 (
  call :log "Не удалось выполнить kubectl login для !CURRENT_HOST!"
  set /a ERROR_COUNT+=1
  exit /b
)
if not exist "!LOGIN_CONFIG!" (
  call :log "kubectl login не создал отдельный kubeconfig для !CURRENT_HOST!"
  set /a ERROR_COUNT+=1
  exit /b
)
call :start_proxy
if errorlevel 1 (
  call :log "Не удалось запустить kubectl proxy для !CURRENT_HOST!"
  set /a ERROR_COUNT+=1
  exit /b
)
call :export_host
set "HOST_RESULT=!ERRORLEVEL!"
call :stop_proxy
exit /b !HOST_RESULT!

:export_host
set "NS_JSON=%WORK_DIR%\namespaces-!HOST_INDEX!.json"
set "NS_FILE=%WORK_DIR%\namespaces-!HOST_INDEX!.txt"
set "NS_ERROR=%WORK_DIR%\namespaces-!HOST_INDEX!.err"
call :http_get "!PROXY_BASE!/api/v1/namespaces" "!NS_JSON!" "!NS_ERROR!"
if errorlevel 1 (
  call :log "Не удалось получить список namespace на !CURRENT_HOST!"
  call :show_kubectl_error "!NS_ERROR!"
  set /a ERROR_COUNT+=1
  exit /b 1
)
call :confirm_proxy
if errorlevel 1 (
  set /a ERROR_COUNT+=1
  exit /b 1
)
"%KUBECTL%" patch --local -f "!NS_JSON!" --type=merge -p "{}" -o=jsonpath="{.metadata.name}{'\n'}" > "!NS_FILE!" 2> "!NS_ERROR!"
if errorlevel 1 (
  call :log "Не удалось получить список namespace на !CURRENT_HOST!"
  call :show_kubectl_error "!NS_ERROR!"
  set /a ERROR_COUNT+=1
  exit /b 1
)
for %%F in ("!NS_FILE!") do if %%~zF EQU 0 (
  call :log "kubectl не вернул ни одного namespace на !CURRENT_HOST!"
  set /a ERROR_COUNT+=1
  exit /b 1
)
set /a SUCCESS_COUNT+=1
for /f "usebackq delims=" %%N in ("!NS_FILE!") do (
  set "CURRENT_NAMESPACE=%%N"
  call :process_namespace
)
exit /b 0

:start_proxy
set "PROXY_STARTED=0"
set "PROXY_PID="
set /a PROXY_PORT=20000 + (!RANDOM! %% 30000)
set "PROXY_BASE=http://127.0.0.1:!PROXY_PORT!"
set "PROXY_LOG=%WORK_DIR%\proxy-!HOST_INDEX!.log"
set "HTTP_STATUS=%WORK_DIR%\http-!HOST_INDEX!.status"
start "" /b "%KUBECTL%" proxy --address=127.0.0.1 --port=!PROXY_PORT! --kubeconfig="!LOGIN_CONFIG!" 1>"!PROXY_LOG!" 2>&1
if errorlevel 1 exit /b 1
set "PROXY_STARTED=1"
exit /b 0

:confirm_proxy
findstr /L /C:"Starting to serve on 127.0.0.1:!PROXY_PORT!" "!PROXY_LOG!" >nul 2>&1
if errorlevel 1 (
  call :log "Не удалось проверить kubectl proxy на !PROXY_BASE!"
  call :show_kubectl_error "!PROXY_LOG!"
  exit /b 1
)
call :find_proxy_pid
if not defined PROXY_PID (
  call :log "Не удалось определить PID kubectl proxy на !PROXY_BASE!"
  exit /b 1
)
exit /b 0

:find_proxy_pid
set "PROXY_PID="
for /f "tokens=2,5" %%A in ('netstat.exe -ano -p tcp 2^>nul ^| findstr.exe /C:"127.0.0.1:!PROXY_PORT!"') do if "%%A"=="127.0.0.1:!PROXY_PORT!" if not defined PROXY_PID set "PROXY_PID=%%B"
exit /b 0

:stop_proxy
if not "!PROXY_STARTED!"=="1" exit /b
findstr /L /C:"Starting to serve on 127.0.0.1:!PROXY_PORT!" "!PROXY_LOG!" >nul 2>&1
if not errorlevel 1 (
  if not defined PROXY_PID call :find_proxy_pid
  set "OWN_PROXY_PID=!PROXY_PID!"
  call :find_proxy_pid
  if defined PROXY_PID (
    if "!PROXY_PID!"=="!OWN_PROXY_PID!" (
      taskkill.exe /PID !PROXY_PID! /T /F >nul 2>&1
      if errorlevel 1 (
        call :log "Не удалось остановить kubectl proxy с PID !PROXY_PID!"
        set /a ERROR_COUNT+=1
      )
    ) else (
      call :log "PID прокси изменился на !PROXY_BASE!; другой процесс не остановлен"
      set /a ERROR_COUNT+=1
    )
  )
)
set "PROXY_PID="
set "PROXY_STARTED=0"
exit /b 0

:http_get
set "HTTP_CODE=000"
curl.exe --noproxy "*" --silent --show-error --retry 4 --retry-connrefused --retry-delay 1 --retry-max-time 15 --connect-timeout 5 --max-time 60 --header "Accept: application/json" --output "%~2" --write-out "%%{http_code}" "%~1" 1>"!HTTP_STATUS!" 2>"%~3"
if errorlevel 1 exit /b 1
if exist "!HTTP_STATUS!" set /p "HTTP_CODE="<"!HTTP_STATUS!"
if not "!HTTP_CODE!"=="200" (
  >>"%~3" echo HTTP !HTTP_CODE! при обращении к %~1
  if exist "%~2" type "%~2" >>"%~3"
  exit /b 1
)
exit /b 0

:show_kubectl_error
if not exist "%~1" exit /b
type "%~1"
type "%~1" >> "%LOG_FILE%"
exit /b

:process_namespace
set "RESOURCE_KIND=Deployment"
set "RESOURCE_PLURAL=deployments"
set "JSONPATH=N:{.metadata.name}{'|'}B:{.metadata.labels.GIT_BRANCH}{'|'}M:{.metadata.labels.vtb\.ru/branch}{'|'}S:{.metadata.labels.STARTED_BY}{'|'}T:{.metadata.labels.vtb\.ru/started_by}{'|'}V:{.metadata.labels.app\.kubernetes\.io/version}{'|'}b:{'|'}m:{'|'}s:{'|'}t:{'|'}v:{'|'}p:{.spec.template.metadata.labels.GIT_BRANCH}{'|'}q:{.spec.template.metadata.labels.vtb\.ru/branch}{'|'}r:{.spec.template.metadata.labels.STARTED_BY}{'|'}u:{.spec.template.metadata.labels.vtb\.ru/started_by}{'|'}w:{.spec.template.metadata.labels.app\.kubernetes\.io/version}{'|'}I:{range .spec.template.spec.containers[*]}{.image}{';'}{end}{range .spec.template.spec.initContainers[*]}{.image}{';'}{end}{'|'}END{'\n'}"
call :export_resource
set "RESOURCE_KIND=CronJob"
set "RESOURCE_PLURAL=cronjobs"
set "JSONPATH=N:{.metadata.name}{'|'}B:{.metadata.labels.GIT_BRANCH}{'|'}M:{.metadata.labels.vtb\.ru/branch}{'|'}S:{.metadata.labels.STARTED_BY}{'|'}T:{.metadata.labels.vtb\.ru/started_by}{'|'}V:{.metadata.labels.app\.kubernetes\.io/version}{'|'}b:{.spec.jobTemplate.metadata.labels.GIT_BRANCH}{'|'}m:{.spec.jobTemplate.metadata.labels.vtb\.ru/branch}{'|'}s:{.spec.jobTemplate.metadata.labels.STARTED_BY}{'|'}t:{.spec.jobTemplate.metadata.labels.vtb\.ru/started_by}{'|'}v:{.spec.jobTemplate.metadata.labels.app\.kubernetes\.io/version}{'|'}p:{.spec.jobTemplate.spec.template.metadata.labels.GIT_BRANCH}{'|'}q:{.spec.jobTemplate.spec.template.metadata.labels.vtb\.ru/branch}{'|'}r:{.spec.jobTemplate.spec.template.metadata.labels.STARTED_BY}{'|'}u:{.spec.jobTemplate.spec.template.metadata.labels.vtb\.ru/started_by}{'|'}w:{.spec.jobTemplate.spec.template.metadata.labels.app\.kubernetes\.io/version}{'|'}I:{range .spec.jobTemplate.spec.template.spec.containers[*]}{.image}{';'}{end}{range .spec.jobTemplate.spec.template.spec.initContainers[*]}{.image}{';'}{end}{'|'}END{'\n'}"
call :export_resource
exit /b

:export_resource
set "ROWS_FILE=%WORK_DIR%\!RESOURCE_PLURAL!-!HOST_INDEX!.txt"
set "RESOURCE_JSON=%WORK_DIR%\!RESOURCE_PLURAL!-!HOST_INDEX!.json"
set "RESOURCE_ERROR=%WORK_DIR%\!RESOURCE_PLURAL!-!HOST_INDEX!.err"
if "!RESOURCE_PLURAL!"=="deployments" (
  set "RESOURCE_PATH=/apis/apps/v1/namespaces/!CURRENT_NAMESPACE!/deployments"
) else (
  set "RESOURCE_PATH=/apis/batch/v1/namespaces/!CURRENT_NAMESPACE!/cronjobs"
)
call :http_get "!PROXY_BASE!!RESOURCE_PATH!" "!RESOURCE_JSON!" "!RESOURCE_ERROR!"
if errorlevel 1 (
  call :log "Не удалось получить !RESOURCE_PLURAL! в !CURRENT_NAMESPACE! на !CURRENT_HOST!"
  call :show_kubectl_error "!RESOURCE_ERROR!"
  set /a ERROR_COUNT+=1
  exit /b
)
rem Kubectl parses the downloaded JSON locally; it does not contact the API server.
"%KUBECTL%" patch --local -f "!RESOURCE_JSON!" --type=merge -p "{}" -o=jsonpath="!JSONPATH!" > "!ROWS_FILE!" 2> "!RESOURCE_ERROR!"
if errorlevel 1 (
  call :log "Не удалось получить !RESOURCE_PLURAL! в !CURRENT_NAMESPACE! на !CURRENT_HOST!"
  call :show_kubectl_error "!RESOURCE_ERROR!"
  set /a ERROR_COUNT+=1
  exit /b
)
rem Prefixes preserve empty label fields when FOR /F splits the pipe-delimited rows.
for /f "usebackq tokens=1-18 delims=|" %%A in ("!ROWS_FILE!") do (
  set "F_NAME=%%A"
  set "F_BRANCH=%%B"
  set "F_ALT_BRANCH=%%C"
  set "F_STARTED=%%D"
  set "F_ALT_STARTED=%%E"
  set "F_VERSION=%%F"
  set "F_JOB_BRANCH=%%G"
  set "F_JOB_ALT_BRANCH=%%H"
  set "F_JOB_STARTED=%%I"
  set "F_JOB_ALT_STARTED=%%J"
  set "F_JOB_VERSION=%%K"
  set "F_POD_BRANCH=%%L"
  set "F_POD_ALT_BRANCH=%%M"
  set "F_POD_STARTED=%%N"
  set "F_POD_ALT_STARTED=%%O"
  set "F_POD_VERSION=%%P"
  set "F_IMAGES=%%Q"
  set "F_END=%%R"
  call :append_row
)
exit /b

:append_row
if not "!F_END!"=="END" (
  call :log "Некорректные данные !RESOURCE_KIND! в !CURRENT_NAMESPACE!"
  set /a ERROR_COUNT+=1
  exit /b
)
set "RESOURCE_NAME=!F_NAME:~2!"
set "BRANCH=!F_BRANCH:~2!"
set "STARTED=!F_STARTED:~2!"
set "VERSION=!F_VERSION:~2!"
if not defined BRANCH set "BRANCH=!F_ALT_BRANCH:~2!"
if not defined STARTED set "STARTED=!F_ALT_STARTED:~2!"
if not defined BRANCH set "BRANCH=!F_JOB_BRANCH:~2!"
if not defined BRANCH set "BRANCH=!F_JOB_ALT_BRANCH:~2!"
if not defined STARTED set "STARTED=!F_JOB_STARTED:~2!"
if not defined STARTED set "STARTED=!F_JOB_ALT_STARTED:~2!"
if not defined VERSION set "VERSION=!F_JOB_VERSION:~2!"
if not defined BRANCH set "BRANCH=!F_POD_BRANCH:~2!"
if not defined BRANCH set "BRANCH=!F_POD_ALT_BRANCH:~2!"
if not defined STARTED set "STARTED=!F_POD_STARTED:~2!"
if not defined STARTED set "STARTED=!F_POD_ALT_STARTED:~2!"
if not defined VERSION set "VERSION=!F_POD_VERSION:~2!"
set "IMAGES=!F_IMAGES:~2!"
if "!IMAGES:~-1!"==";" set "IMAGES=!IMAGES:~0,-1!"
>>"!CSV_FILE!" echo("!CURRENT_HOST!","!CURRENT_NAMESPACE!","!RESOURCE_KIND!","!RESOURCE_NAME!","!IMAGES!","!BRANCH!","!STARTED!","!VERSION!"
if errorlevel 1 (
  call :log "Не удалось записать строку CSV"
  set /a ERROR_COUNT+=1
) else set /a ROW_COUNT+=1
exit /b

:log
set "LOG_LINE=[%DATE% %TIME:~0,8%] %~1"
echo(!LOG_LINE!
>>"%LOG_FILE%" echo(!LOG_LINE!
exit /b
