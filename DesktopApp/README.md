# Quaber Desktop

Нативная версия Quaber на Python и Qt. Исходный код и рабочие данные размещены в `DesktopApp`; папка `UserData` создаётся рядом с исполняемым файлом и должна сохраняться при обновлении.

## Запуск из исходников

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

На Windows используйте `.venv\Scripts\python.exe`. Корпоративный `kubectl` положите в `addons/kubectl.exe` рядом с `main.py` или добавьте в `PATH`.

## Сборка Windows portable

Собирать следует на Windows x86-64 с доступом к Python и зависимостям:

```powershell
python -m pip install -r requirements.txt
.\build_windows.ps1
```

Распространяется вся папка `build-artifacts\Quaber`. Для обновления скопируйте новые программные файлы в папку пользователя, сохранив её `UserData` и `addons`. Не запускайте рабочую копию из `build-artifacts`: повторная сборка заменяет эту папку. `kubectl` в поставку приложения не включается.

## Статус реализации

Сейчас реализованы локальное хранилище, подключение через корпоративный `kubectl login` и `proxy`, базовая матрица, чтение Pod/Deployment/ConfigMap/ResourceQuota, логи, события, метрики из Metrics API с fallback на kubelet и cAdvisor, консоль Pod, редактирование YAML и операции масштабирования, restart и удаления Pod. Работа над точным переносом всех элементов интерфейса и Windows-приёмкой продолжается.

Файл `UserData/portable.key` переносится вместе с зашифрованным kubeconfig. Полный доступ к `UserData` даёт доступ к действующим подключениям.
