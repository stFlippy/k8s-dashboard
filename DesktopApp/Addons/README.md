# kubectl: Linux и Windows

Эталон — Bash-скрипт `kubectl`, версия 0.4.0. По запросу пользователя
TLS-проверка отключена по умолчанию и CA-файлы в этом режиме не требуются.
Это изменение внесено одинаково в Linux- и Windows-версии.
`kubectl_windows.py` переносит его поведение на Windows. `kubectl.exe`
собирается командой из DesktopApp:

```powershell
.\Addons\build_kubectl_windows.ps1
```

Справка и версия берутся из исходного скрипта, включённого в сборку.
Bash для работы Windows-версии не требуется. Нужны `curl.exe` и настоящий
`kubectl.exe` в PATH. Клиент выбирается по порядку PATH, с проверкой
`version --client` и исключением самой обёртки. Отдельный `kubectl-real.exe`
больше не используется.
На Windows, если клиент отсутствует в PATH уже запущенного процесса,
дополнительно проверяется сохранённый системный и пользовательский PATH.
Это позволяет найти клиент после настройки PATH без перезапуска терминала.
Настройки окружения в реестре не меняются.

## Контракт исходного скрипта

- CLI: `login`/`-login`, URL, `--server`, `-u`/`--username`, `-p`/`--password`,
  `--onlytoken[=true|false]`, `-h`/`--help`, `--version`.
- Другие команды передаются системному kubectl с сохранением аргументов и кода выхода.
- Дополнительных CLI-флагов для TLS, CA и issuer нет. Используются только
  исходные переменные `QUABER_*`; прежний `kubectl-login.json` не читается.
  Локальный файл сохранён, но больше не влияет на запуск.
- Без URL читается сервер текущего context из kubeconfig.
- Kubeconfig определяется через KUBECONFIG, иначе HOME/.kube/config.
  В Windows при отсутствии HOME автоматически используется профиль пользователя
  (обычно USERPROFILE). Дополнительно настраивать окружение для login не нужно.
- Пароль читается скрыто из терминала либо первой строкой stdin.
- Discovery, password grant и проверка API выполняются через curl с исходными
  таймаутами, TLS-параметрами и HTTP-обработкой, включая 403 и диагностику 401.
- `--onlytoken` печатает ID token и не сохраняет kubeconfig.
- Новый kubeconfig всегда проходит `kubectl config view --raw --flatten`.
  Сохраняются посторонние contexts и доступный ранее выбранный namespace.
- Сохранены сообщения, stdout/stderr и коды завершения проверенных сценариев.
  Сохранена и особенность Bash: неверный boolean печатает ошибку, но вызов
  продолжается с пустым значением.

## Окружение

Поддерживаются исходные переменные:

```text
KUBECONFIG, HOME
QUABER_OIDC_ISSUER_URL, QUABER_OIDC_CLIENT_ID
QUABER_OIDC_CLIENT_SECRET, QUABER_OIDC_CLIENT_SECRET_FILE
QUABER_OIDC_SCOPES, QUABER_LOGIN_TIMEOUT
QUABER_API_CA_FILE, QUABER_OIDC_CA_FILE
QUABER_API_INSECURE_SKIP_TLS_VERIFY
QUABER_OIDC_INSECURE_SKIP_TLS_VERIFY
QUABER_CLUSTER_NAME, QUABER_CONTEXT_NAME, QUABER_USER_NAME
```

Относительные пути разрешаются относительно рабочего каталога, как в Bash.
Лабораторные адреса и Linux-пути CA сохранены. По умолчанию пути CA не
используются и сертификаты можно не устанавливать. Запуск из Addons:

```powershell
.\kubectl.exe login https://192.168.56.11:6443
```

Для включения проверки TLS явно задайте `false` и реальные пути CA:

```powershell
$env:QUABER_API_INSECURE_SKIP_TLS_VERIFY = 'false'
$env:QUABER_OIDC_INSECURE_SKIP_TLS_VERIFY = 'false'
$env:QUABER_API_CA_FILE = 'C:\path\to\k8s-lab-1-ca.crt'
$env:QUABER_OIDC_CA_FILE = 'C:\path\to\dex-ca.crt'
.\kubectl.exe login https://192.168.56.11:6443
```

Проверка сертификатов API и Dex управляется раздельно через переменные,
а не CLI-флаги. Значение по умолчанию для обеих переменных — `true`.
Чтобы отключить ранее включённую проверку в текущем терминале:

```powershell
$env:QUABER_API_INSECURE_SKIP_TLS_VERIFY = 'true'
$env:QUABER_OIDC_INSECURE_SKIP_TLS_VERIFY = 'true'
```

CA проверяется и используется только при включённой проверке TLS.
В insecure-режиме даже заданный, но отсутствующий CA-файл не мешает входу.
В kubeconfig сохраняется `insecure-skip-tls-verify: true` для последующих
proxy/exec; существующий kubeconfig изменится при следующем успешном login.
Приложение DesktopApp удаляет QUABER_* при login; это существующее поведение
приложения сохранено. Эта доработка касается только kubectl.

## Необходимые различия платформ

- Имена исполняемых файлов `.exe`, разделитель списка KUBECONFIG `;` вместо `:`.
- Отсутствующий HOME заменяется папкой профиля Windows; явно заданные
  KUBECONFIG и HOME имеют приоритет. В Linux HOME обычно задаёт сама ОС.
- Base64, JSON и разбор даты реализованы стандартной библиотекой Python;
  jq/base64/GNU date как отдельные программы не нужны.
- На Windows используются ACL каталогов, а не POSIX chmod/umask.
- Системные сообщения curl, доверенные CA ОС и консольный ввод зависят от ОС.
  Это не изменение логики login, но их побайтовое совпадение не гарантируется.

## Проверки

Сравнительные тесты запускают Linux-скрипт через Git Bash и Windows
порт с одинаковыми сценариями. Проверяются stdout, stderr, коды выхода,
локальный HTTPS discovery/token, форма запроса с Unicode, `--onlytoken`,
ошибки авторизации, 200/403 API, итоговый kubeconfig и повторный вход с
сохранением namespace. Для JSON-парсера и системного kubectl в тестовом Bash
используются адаптеры Windows-путей и LF; логика Linux-скрипта в тестах не меняется.
Для полного сравнительного набора на этой машине используется системный
`C:\Projects\kubectl.exe`. Тесты не обращаются к реальным кластерам.
