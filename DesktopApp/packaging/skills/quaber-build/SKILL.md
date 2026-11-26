---
name: quaber-build
description: Собирает Windows x64 portable Quaber из DesktopApp в Builds, используя профили baseline и conservative-candidate (также называемый conventional). Применять для сборки EXE/ZIP текущего приложения и проверки полученных артефактов.
---

# Сборка Quaber в Builds

Выполни сборку текущих исходников существующим pipeline проекта. По умолчанию нужны **два профиля одной версии исходников**: `baseline` и `conservative-candidate`. Если пользователь явно выбрал один профиль, собирай только его.

## Проект и профили

Найди `DesktopApp` в текущем репозитории или по пути пользователя. На этой машине: `C:\Projects\k8s-dashboard\DesktopApp`. Проверь наличие `main.py`, `quaber/`, `build_windows.ps1`, `requirements.lock` и `packaging/Quaber.spec`.

| Название в запросе | Значение `-Profile` |
|---|---|
| baseline, обычная, полная | `baseline` |
| conventional, conservative, облегчённая | `conservative-candidate` |

Профиля `conventional` в pipeline нет: это пользовательское название `conservative-candidate`. `minimal-candidate` используй только по явному запросу, как отдельный экспериментальный профиль.

Перед запуском прочитай актуальные `build_windows.ps1`, `packaging/Quaber.spec`, `packaging/README.md`, `requirements.lock`; при ошибке policy — также `packaging/policy.py` и `packaging/policy-files.json`. Используй текущую конфигурацию, а не команды из старых логов. Если пользователь отдельно запросил устранение ошибки, следуй и этому поручению; обычная сборка сама по себе не предполагает правок приложения.

## Окружение

Нужен **полный CPython 3.11 x64** и отдельный venv с точными версиями из `requirements.lock`. Текущий Qt-контракт: `PyQt5==5.15.9`, `PyQt5-Qt5==5.15.2`, `PyQt5-sip==12.18.0`. Не обновляй lock, Qt, PyInstaller или список исключений ради успешной сборки.

1. Используй указанный пользователем интерпретатор. Иначе проверь `.venv-release-size\Scripts\python.exe`, затем `.venv-qt5\Scripts\python.exe`: наличие файла ещё не означает работоспособность venv.
2. Проверь запуск, `sys.executable`, версию и разрядность. Затем из DesktopApp выполни `python release_manifest.py` и `python -m pip check` именно выбранным Python. Эти проверки уже есть внутри build script; предварительный запуск нужен, чтобы не тратить время на два заведомо неисправных профиля.
3. При `No Python at ...` / exit 103 сначала установи, **кому недоступен** базовый Python. Прочитай `pyvenv.cfg` как UTF-8, проверь точный `executable` и текущую Windows-учётную запись. `Test-Path=False` с ошибкой `Access denied` не доказывает отсутствие файла. Под `CodexSandboxOffline` пользовательский Python может быть закрыт для песочницы, а `py -0p` читает реестр другой учётной записи и не видит пользовательскую установку. Через штатное разрешённое выполнение вне песочницы повтори запуск существующего Python, `release_manifest.py` и `pip check`; при успехе выполняй сборку в том же разрешённом контексте. Не меняй ACL, не копируй установленный Python и не переустанавливай его ради обхода ограничений. Если такой запуск недоступен, укажи блокер доступа и команду для PowerShell пользователя, а не утверждай «Python отсутствует».
4. Если после проверки доступа подходящего окружения действительно нет или оно не соответствует lock, создай **новый** venv из доступного полного CPython 3.11 x64. Установи `requirements.lock`: сначала из `offline-deps\wheelhouse-qt5` с `--no-index --find-links`, если там есть полный набор; иначе используй доступный разрешённый источник пакетов. Повтори проверки. Не переустанавливай зависимости в рабочее пользовательское окружение автоматически. Embedded runtime из `reports/.../runtime` пригоден для отдельных диагностик, но не заменяет release venv.

Не маскируй несовпадение lock/policy: остановись с конкретной ошибкой. Не разрешай сборку с PyQt6/PySide или импортом Qt из другого окружения. Наследованные `PYTHONHOME`, `PYTHONPATH`, `QT_PLUGIN_PATH`, `QT_QPA_PLATFORM_PLUGIN_PATH`, `QT_QPA_PLATFORM` не должны подменять release runtime; при необходимости изолируй их только для дочерних процессов, сохранив исходные значения.

## Выполнение

**Всегда передавай `-OutputRoot` явно:** значение по умолчанию у build script находится вне `Builds`.

Создай новый каталог `DesktopApp/Builds/build-YYYYMMDD-HHMMSS-xxxxxxxx/`. Последняя часть — короткий случайный идентификатор. Старые каталоги и их содержимое сохраняй. Родитель запуска может существовать, но каталог каждого профиля перед вызовом `build_windows.ps1` **не должен существовать**.

Пример PowerShell после определения абсолютных `$appRoot` и `$python`:

```powershell
$ErrorActionPreference = 'Stop'
$runId = '{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), ([guid]::NewGuid().ToString('N').Substring(0, 8))
$runRoot = Join-Path (Join-Path $appRoot 'Builds') ('build-' + $runId)
if (Test-Path -LiteralPath $runRoot) { throw "Output already exists: $runRoot" }
New-Item -ItemType Directory -Path $runRoot | Out-Null
$buildProfiles = @('baseline', 'conservative-candidate') # оставь один при явном выборе пользователя

foreach ($buildProfile in $buildProfiles) {
    $output = Join-Path $runRoot $buildProfile
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $appRoot 'build_windows.ps1') -Interpreter $python -OutputRoot $output -Profile $buildProfile
    if ($LASTEXITCODE -ne 0) { throw "Build failed: $buildProfile; inspect $output/reports" }
}
```

Собирай последовательно. При ошибке сохрани лог, назови этап и статус оставшихся профилей. Повторный запуск делай в новом каталоге. Не используй `--noconfirm` как разрешение перезаписать старую сборку: изоляцию обеспечивает новый `-OutputRoot`.

Ожидаемый результат для каждого профиля:

```text
<run>/<profile>/
  Quaber/Quaber.exe
  Quaber/release-manifest.json
  Quaber/sbom.json
  Quaber-<profile>.zip
  reports/pyinstaller.log
  reports/exclusions.json
  reports/pe-closure.json
  reports/files.csv
  reports/size.json
  work/
```

`build_windows.ps1` вызывает PyInstaller с версионным spec, затем `release_manifest.py` и `packaging/release_check.py`. Сохраняй этот pipeline: он проверяет Qt/plugins, ресурсы, PE import/delay-import зависимости, исключённые данные и roundtrip ZIP с кириллицей/пробелами. При отказе проверки не обходи её ручным удалением DLL, изменением policy или упаковкой непроверенного результата.

## Проверка и сопоставимость

- Убедись, что каждый запрошенный профиль завершился с exit 0; EXE, ZIP и перечисленные отчёты существуют; профиль в manifest/exclusions/size совпадает с каталогом; `pe-closure.json` не содержит unresolved imports.
- Сравни `source_sha256` двух manifest и входные `assets/`, `public/`, lock до/после сборок. Учитывай незакоммиченные файлы: обычный dirty working tree допустим. Если кто-то изменил входные данные между профилями, не объявляй пару сопоставимой и не откатывай чужие правки; пересобери оба с согласованного состояния или явно зафиксируй расхождение.
- В portable и ZIP отсутствуют Qt6/PySide, реальные `UserData`, kubeconfig, пароли, журналы, отчёты и исходное содержимое Addons. Pipeline оставляет только пустой каталог `addons`; внешний kubectl поставляется отдельно по правилам проекта. Не анализируй и не упаковывай эмулятор из Addons.
- `baseline` сохраняет полный состав по текущей policy; `conservative-candidate` применяет только утверждённые исключения. Не добавляй UPX, strip, onefile, raster или новые удаления. Облегчение не должно менять функциональность.
- Размер приложения бери из `reports/size.json` (`bytes`, `zip_bytes`) и сверяй с полезной нагрузкой `Quaber/`. Не включай `work/`, логи и соседние сборки в размер приложения. Сравни MiB и выигрыш candidate относительно baseline: `(baseline - candidate) / baseline * 100`.

## Нативный запуск

Успешная упаковка не доказывает успешный запуск GUI. Для каждого профиля распакуй ZIP в отдельный новый `<run>/acceptance/<profile>/` и проверяй эту копию с пустыми локальными данными. **Не запускай EXE в распространяемом `Quaber/`**: приложение создаёт UserData и загрязнит payload после составления manifest.

При доступном Windows desktop проверь настоящий `qwindows`, появление окна, несколько секунд обработки событий, штатное закрытие и повторный запуск. Проверь exit code и журналы только тестовой копии. Offscreen и факт существования процесса не заменяют эту проверку. Управляй только своим PID; если понадобилось принудительное завершение зависшего процесса, не называй закрытие штатным.

Не подключай реальные кластеры, не импортируй рабочий UserData и не выполняй админские действия ради smoke. `prepare_acceptance.py` использует реальные подключения и не входит в обычную сборку. При недоступном GUI/DPAPI/RDP отметь соответствующую проверку как **not verified**, сохрани результаты сборки и объясни ограничение. Падение native startup — **failed**, а не not verified. Проверку полного функционала/RDP/GPU не объявляй пройденной по одному старту.

## Результат пользователю

Сохрани в корне запуска краткий `BUILD_RESULT.md`: исходная версия/хеши, Python и Qt, профили, статусы packaging/native smoke, пути EXE/ZIP/логов, SHA-256 ZIP, размер папки/архива и разница. Для непроверенного или упавшего шага укажи точную причину. Сохраняй существующие отчёты и сборки.

В ответе дай ссылки на оба ZIP и `BUILD_RESULT.md`, размеры и фактический статус запуска. Если сборка заблокирована, назови ошибку и конкретный способ продолжения; наличие старых ZIP не выдавай за результат текущего запуска.
