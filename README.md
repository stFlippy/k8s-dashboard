# Quaber — Kubernetes Control Panel

Quaber — веб-панель для работы с несколькими Kubernetes-кластерами. Интерфейс показывает Pods в матрице Deployment × Namespace и даёт доступ к логам, консоли, YAML, ConfigMaps, квотам и состоянию нод.

Браузер обращается только к backend Quaber. Backend подключается к Kubernetes API с учётными данными текущего пользователя; kubeconfig, токены и пароли во frontend не передаются.

## Возможности

- персональный список кластеров для каждого пользователя;
- подключение через kubeconfig, bearer token или логин/пароль с тестом до сохранения;
- фильтры Pods по статусу, Deployment, Namespace и кластеру;
- подсветка недавних рестартов и ручной rollout restart;
- пользовательские вкладки с собственными фильтрами, порядком и цветом;
- импорт/экспорт вкладок с проверкой кластеров и уникальности имён;
- текущие и previous-логи, поиск и экспорт;
- защищённая консоль Pod, YAML, метрики, события и контейнеры;
- редактирование ConfigMaps с подтверждением;
- ResourceQuota и Cluster Health;
- светлая/тёмная темы;
- локальные пользователи, временные пароли и 12-часовые сессии.

## Стек и схема

- frontend: React, TypeScript, Vite, TanStack Query;
- backend: Node.js, TypeScript, `@kubernetes/client-node`;
- пользователи и настройки: SQLite;
- credentials кластеров: AES-256-GCM с ключом `DASHBOARD_MASTER_KEY`.

```text
Browser -> frontend -> backend -> Kubernetes API
                        |
                        +-> SQLite и зашифрованные credentials
```

## Требования и запуск

- Node.js 22.12+ и npm 10+;
- сетевой доступ backend к Kubernetes API;
- Kubernetes identity с нужным RBAC;
- Metrics Server для CPU/RAM и полной страницы Cluster Health.

```bash
npm run install:all
cp .env.example .env
openssl rand -hex 32
```

Запишите результат последней команды в `.env`:

```dotenv
DASHBOARD_MASTER_KEY=<случайный-ключ-не-короче-32-символов>
```

Не меняйте ключ после сохранения кластеров: без него credentials расшифровать нельзя. Создайте первичного пользователя по следующему разделу и запустите:

```bash
npm run dev
```

- UI: `http://localhost:5173`;
- backend: `http://localhost:3001`.

Проверки и сборка:

```bash
npm run typecheck
npm run build
```

`npm run check` дополнительно ищет секреты и намеренно падает при наличии корневого `.env`; запускайте его до создания `.env` или в чистой копии.

## Конфигурация backend

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `PORT` | порт backend | `3001` |
| `DASHBOARD_MASTER_KEY` | ключ шифрования, минимум 32 символа | нет |
| `USER_DATABASE_PATH` | SQLite пользователей и настроек | `server/data/users.sqlite` |
| `BOOTSTRAP_USER_PATH` | одноразовый файл первого пользователя | `server/data/bootstrap-user.json` |
| `CLUSTER_STORE_PATH` | legacy-хранилище для миграции | `server/data/clusters.json` |
| `KUBECONFIG`, `KUBE_CONTEXT` | legacy/default cluster backend-машины | стандартный kubeconfig/context |

## Первичный пользователь

До первого запуска:

```bash
cp server/bootstrap-user.example.json server/data/bootstrap-user.json
```

```json
{
  "username": "admin",
  "password": "длинный-случайный-временный-пароль"
}
```

При старте пользователь импортируется в SQLite, пароль превращается в salted scrypt-хэш, а JSON удаляется. Первый вход потребует сменить пароль. Если файл остался, проверьте права backend на каталог и удалите его после успешного импорта.

В меню пользователя есть `Личное`, `Кластеры`, `Пользователи`. Новый пользователь создаётся по уникальному имени; временный пароль генерируется и показывается один раз. После первого входа или сброса пароль требуется заменить. Смена собственного пароля требует старый пароль и двойной ввод нового. Сессия действует 12 часов.

Сейчас ролей внутри Quaber нет: любой вошедший пользователь после смены временного пароля может управлять пользователями. Учитывайте это при публикации панели в недоверенной сети.

# Добавление кластеров

Откройте меню пользователя → `Кластеры` → `Добавить кластер`. Введите уникальное имя, необязательный алиас до пяти символов, выберите способ подключения, заполните поля и нажмите `Тест`. После успешной проверки сохраните запись.

Backend должен разрешать DNS-имя API, доверять сертификату и иметь маршрут до адреса. `localhost` означает машину/контейнер backend, не компьютер с браузером.

## Kubeconfig из файла — рекомендуемый способ

На машине с рабочим `kubectl` проверьте context:

```bash
kubectl config get-contexts
kubectl config current-context
kubectl get namespaces
```

Создайте автономный kubeconfig для текущего context:

```bash
kubectl config view --raw --minify --flatten > quaber-kubeconfig.yaml
```

В форме выберите `Kubeconfig` → `Открыть файл`, укажите YAML и нажмите `Тест`. `--flatten` встраивает сертификаты вместо локальных путей, `--raw` включает секреты. Поэтому файл нельзя отправлять в Git, мессенджеры или тикеты; временную копию после импорта лучше удалить.

Допускается полный kubeconfig с несколькими contexts, но автономный минимальный файл предсказуемее. Ограничения:

- относительные пути backend недоступны — используйте `--flatten`;
- `exec`/auth-provider плагин должен быть установлен и настроен на backend-машине;
- короткоживущий токен внутри файла потребуется обновлять;
- максимальный размер файла — 1 МиБ.

## Kubeconfig из буфера

Переключите режим Kubeconfig с файла на редактор и вставьте вывод:

```bash
kubectl config view --raw --minify --flatten
```

Проверьте наличие `clusters`, `contexts`, `users` и `current-context`, затем нажмите `Тест`. Backend валидирует YAML и хранит содержимое зашифрованным. Режим удобен при работе через SSH.

## API server + bearer token

Этот способ позволяет выдать Quaber отдельную Kubernetes identity с ограниченным RBAC. Не используйте `cluster-admin` без необходимости.

Пример `quaber-access.yaml`:

```yaml
apiVersion: v1
kind: Namespace
metadata: {name: quaber-system}
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: quaber, namespace: quaber-system}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: quaber}
rules:
  - apiGroups: [""]
    resources: ["namespaces", "nodes"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
  - apiGroups: [""]
    resources: ["events", "resourcequotas"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch", "update", "patch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "update", "patch"]
  - apiGroups: ["apps"]
    resources: ["replicasets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["pods", "nodes"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: quaber}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: quaber}
subjects:
  - {kind: ServiceAccount, name: quaber, namespace: quaber-system}
```

Примените и выпустите token:

```bash
kubectl apply -f quaber-access.yaml
kubectl -n quaber-system create token quaber --duration=24h
```

Срок зависит от настроек Kubernetes. Для эксплуатации организуйте принятую у вас ротацию tokens.

Получите URL API и CA:

```bash
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'; echo
kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}'; echo
```

В форме укажите полный HTTPS URL, token без слова `Bearer` и CA в base64. `Skip TLS verification` используйте только для краткой диагностики.

Проверка RBAC:

```bash
kubectl auth can-i list pods -A --as=system:serviceaccount:quaber-system:quaber
kubectl auth can-i create pods/exec -A --as=system:serviceaccount:quaber-system:quaber
```

Пример даёт возможности интерфейса, включая exec и удаление Pod для rollout. В production сократите правила под реально используемые функции.

## API server + логин/пароль

Укажите HTTPS URL Kubernetes API, username, password и CA. Backend использует Basic Authentication от этой identity. Большинство современных кластеров basic auth отключают; при `401` используйте kubeconfig/OIDC-конфигурацию или token. Логин от сайта облачного провайдера обычно не является Kubernetes credential.

## Тест, редактирование и ошибки

`Тест` выполняет пробный запрос до сохранения; результат закрывается крестиком. Сохранённый кластер можно редактировать и повторно тестировать. Удаление защищено математическим подтверждением и удаляет только запись Quaber, не Kubernetes-кластер.

- `404` с HTML: указан URL сайта/Ingress вместо Kubernetes API;
- `401`: credential неверен или истёк;
- `403`: соединение работает, но RBAC запрещает действие;
- timeout/DNS/`ECONNREFUSED`: backend не видит API;
- TLS error: неверен CA или hostname сертификата;
- нет метрик: Metrics API отсутствует или запрещён.

Алиас содержит до пяти символов; по умолчанию используются первые пять символов имени. Он выводится отдельной фиксированной частью Namespace-заголовка, чтобы одинаковые Namespace разных кластеров не смешивались.

# Интерфейс

## Pod list и фильтры

Строки таблицы — Deployments, колонки — Namespaces. Статусные переключатели:

- `Running` — работающие Pods;
- `Restart < 1h` — новый Pod или restart контейнера за последний час;
- `Pending`, `Error`, `Unknown`;
- `Issues` — только проблемные;
- `Hide empty` — скрыть пустые Namespaces.

Restart определяется по данным Kubernetes, а не по памяти браузера, поэтому виден другим пользователям. Когда `Running` включён, отдельный restart-фильтр неактивен. Deployment с нулём Pods остаётся в таблице с серым `0`.

Автообновление: выключено, 5/10/30 секунд, 1/5 минут или целое пользовательское число минут. Интервал сохраняется в профиле; при переключении вкладки данные обновляются. Фильтры Deployment, Namespace и кластер открываются по одному и закрываются кликом снаружи.

Ширину Deployment и ширину/высоту Namespace-заголовков можно менять. Namespace и Deployments переставляются drag-and-drop с индикатором места; отпускание вне допустимой области отменяет перенос.

## Вкладки

Вкладка сохраняет фильтры, порядок, автообновление и цвет. Цвет применяется к вкладке, панелям управления и подложке таблицы. Вкладки создаются, переименовываются, удаляются и переставляются; контекстное меню открывается ПКМ. Имена уникальны.

Экспорт создаёт JSON, включая цвет. Импорт проверяет формат, имя и наличие кластеров. При конфликте диалог остаётся открытым до исправления или отмены; отсутствие данных не должно ломать текущую страницу.

## Deployment

Нажатие открывает боковую панель, не отдельную страницу. При одинаковом имени сначала выбираются кластер и Namespace. Доступны реплики, Pods, события, YAML, масштабирование и restart. CPU/RAM: зелёный до 75%, жёлтый 75–90%, красный выше 90%. Прочерк снабжён причиной — например, нет Metrics API или requests/limits. Подтверждение изменений явно показывает кластер и Namespace.

## Pod

Панель содержит текущие/previous-логи, консоль, статус, YAML, метрики, события и контейнеры. Логи автоматически прокручиваются, поддерживают поиск и экспорт с именем `<pod>-<container>-ДД.ММ.ГГГГ-ЧЧ.ММ.log`. Previous-лог отмечен как «Лог предыдущего запуска».

Консоль изначально заблокирована. Разблокировка действует до закрытия вкладки или пяти минут простоя; замок в заголовке позволяет заблокировать команды. Enter принимает ответ проверки и выполняет команды, клик по области фокусирует ввод.

## YAML и ConfigMaps

Редакторы имеют фиксированную область, номера строк и поиск. Enter переходит к следующему совпадению, стрелки — вперёд/назад; при нуле совпадений позиция не меняется. Счётчик показывает текущий и общий результат. Сохранение валидирует документ и требует подтверждения; модалки закрываются крестиком.

## ResourceQuota и Cluster Health

ResourceQuota показывает только реально созданные Kubernetes-объекты `ResourceQuota`: hard и used. Пусто означает отсутствие квот либо запрет RBAC, а не нулевую фактическую нагрузку.

Cluster Health выбирает один кластер и показывает суммарные CPU/RAM, ноды, capacity/allocatable/usage, conditions и Details. Usage требует Metrics Server и доступа к `metrics.k8s.io`; без него базовые данные нод остаются доступны.

## Темы и пасхалка

Тема переключается ползунком в `Личное` и сохраняется за пользователем. Если в Deployment-фильтр ввести `йцукенqwerty`, интерфейс сменяется анимированным ASCII-котом; очистка возвращает страницу.

# Безопасность

- пароли: salted scrypt-хэши;
- cluster credentials: AES-256-GCM;
- session cookie: `HttpOnly`, `SameSite=Strict`, а под HTTPS — `Secure`;
- SQL-запросы параметризованы;
- ввод валидируется по типу, длине и формату на frontend/backend;
- YAML, логи и ошибки выводятся как текст, не HTML;
- секретные API-ответы используют `Cache-Control: no-store`;
- неизвестные маршруты показывают страницу 404;
- необратимые действия защищены математическим подтверждением.

Quaber умеет exec и изменять workloads. Защищайте его TLS, сетью и Kubernetes RBAC, используйте отдельные identities и не выдавайте `cluster-admin` без необходимости.

# Production и резервные копии

```bash
npm run build
```

`npm start` запускает backend. Backend не раздаёт `client/dist`: публикуйте frontend static server/reverse proxy и проксируйте `/api` на backend. Передавайте `X-Forwarded-Proto: https` для cookie `Secure`.

Храните на постоянных volumes SQLite и runtime data. Резервируйте SQLite, legacy store при наличии и `DASHBOARD_MASTER_KEY` вместе, но в разных защищённых системах. Без master key credentials не восстановить. Bootstrap JSON после импорта не нужен.

# Диагностика

Метрики:

```bash
kubectl get apiservice v1beta1.metrics.k8s.io
kubectl top nodes
kubectl top pods -A
```

RBAC:

```bash
kubectl auth can-i list pods -A --as=system:serviceaccount:quaber-system:quaber
kubectl auth can-i get pods/log -A --as=system:serviceaccount:quaber-system:quaber
kubectl auth can-i create pods/exec -A --as=system:serviceaccount:quaber-system:quaber
```

Previous-лог существует лишь для предыдущего экземпляра контейнера в том же Pod. После удаления Pod или нового rollout его отсутствие нормально.

При `401` обновите credential и повторите `Тест`. При TLS error проверьте CA и SAN сертификата; отключение проверки TLS — только временная диагностика. Ошибка одного кластера должна отображаться локально и не блокировать остальные.

## Команды разработчика

```bash
npm run dev
npm run typecheck
npm run build
npm run check
```

Не публикуйте `.env`, kubeconfig, tokens, bootstrap JSON, SQLite и `server/data`.

## Лицензия

Лицензия пока не указана. Добавьте `LICENSE` перед публичным распространением.
