# Quaber Kubernetes Control Panel

React + Vite + TypeScript frontend и отдельный Node.js + TypeScript backend на `@kubernetes/client-node`.

Новых runtime-библиотек в этой итерации не добавлено. Используется ранее согласованный стек: React, Vite, TypeScript, TanStack Query и `@kubernetes/client-node`.

## Требования

- Node.js >= 22.12
- npm >= 10
- рабочий kubeconfig для первого/local кластера

## Первичная учётная запись

До первого запуска backend создайте одноразовый файл `server/data/bootstrap-user.json` (образец: `server/bootstrap-user.example.json`):

```json
{
  "username": "admin",
  "password": "длинный-случайный-пароль-не-короче-12-символов"
}
```

При первом запуске пользователь импортируется в SQLite `server/data/users.sqlite`, пароль сохраняется только как salted scrypt-хэш, а bootstrap-файл удаляется. После первого входа потребуется заменить временный пароль. Путь можно изменить через `BOOTSTRAP_USER_PATH`, путь базы — через `USER_DATABASE_PATH`.

Проверка Kubernetes:

```bash
kubectl config current-context
kubectl get nodes
```

## Установка с нуля

После распаковки архива:

```bash
cd k8s-dashboard
```

Создайте `.env`:

```bash
cp .env.example .env
openssl rand -hex 32
```

Полученный ключ вставьте в `.env`:

```text
DASHBOARD_MASTER_KEY=<сгенерированный-ключ>
```

Если проект обновляется поверх уже настроенной версии, **оставьте прежний `DASHBOARD_MASTER_KEY`**, иначе сохранённые credentials дополнительных кластеров нельзя будет расшифровать.

Установка и проверки:

```bash
npm run install:all
npm run typecheck
npm run check
```

Запуск:

```bash
npm run dev
```

Адреса:

- UI: `http://localhost:5173`
- backend: `http://localhost:3001`
- health: `http://localhost:3001/api/health`

Vite проксирует `/api/*` на Node backend. Браузер напрямую к Kubernetes API не подключается.

## Изменения текущей итерации

### Верхняя панель

- промежуточная надпись «Панель управления» удалена;
- увеличен размер текста интерфейса;
- меню пользователя находится поверх sticky-панелей страницы и больше не перекрывается страницей списка pod;
- основные разделы: `Pod list`, `Квоты ресурсов`, `ConfigMaps`.

### Список pod

- фиксированная колонка Deployment — 110 px;
- namespace — фиксированные колонки 24 px;
- вертикальные namespace-заголовки разделены более контрастной сеткой;
- заголовки namespace используют цвет текста, контрастный относительно фона;
- hover по имени Deployment подчёркивает, что строка кликабельна;
- верхняя панель фильтров остаётся закреплённой при прокрутке;
- отдельная легенда удалена, статусы собраны в карточках статистики;
- статистика показывает `Running`, `Pending`, `Error`, `Unknown` и `Total`;
- мигание `Pending` отключено;
- статусные подписи оставлены на английском;

### Фильтры

Доступны:

- `Running`;
- `Pending`;
- `Error`;
- `Unknown`;
- экспериментальный режим `Только проблемные`;
- скрытие namespace, в которых нет pod, соответствующих текущему фильтру;
- ручная кнопка `Обновить`;
- автообновление: выкл / 5 сек / 10 сек / 30 сек / 1 мин.

Настройки сохраняются в `localStorage`.

### Недавний рестарт pod

Pod подсвечивается кислотно-голубым цветом, если Kubernetes сообщает реальный рестарт контейнера за последний час через `lastState`/`restartCount`. В подсказке отображается время последнего рестарта. Счётчик и история рестартов не ведутся.

### Deployment

- из верхней статистики удалена дублирующая карточка `Доступно`;
- карточка `Реплики` переименована в `Pods` и показывает `available/desired`;
- у поля количества pod отключены встроенные browser spinner-кнопки — остаются только отдельные `−` и `+`;
- `Reload pods`, удаление, изменение количества pod, ресурсов и YAML требуют математического подтверждения;
- присутствуют события Deployment и принадлежащих ему ReplicaSet;
- даты событий выводятся в формате `ДД.ММ.ГГГГ`.

### ConfigMaps

- фильтры кластеров и namespace сделаны как отдельные dropdown-поля со стрелкой;
- строки вариантов подсвечиваются при наведении;
- dropdown закрывается при клике вне него;
- выбранные кластеры и namespace сохраняются в `localStorage`;
- колонка Keys отсутствует;
- detail открывается модальным окном ниже главного header;
- дата создания выводится в формате `ДД.ММ.ГГГГ`.

### Панель пользователя / кластеры

- исправлен z-index панели;
- чувствительные token/password отображаются только масками;
- tooltip для полей формы срабатывает при наведении на всю область соответствующего поля, в том числе на input/select/textarea;
- подсказка появляется примерно через 2 секунды и написана на русском;
- поддерживаются kubeconfig/context, bearer token и login/password подключения.

### Pod panel

- текущие логи;
- `Логи до падения` через Kubernetes `previous=true`;
- консоль pod с временной разблокировкой команд;
- описание;
- YAML/resource editor;
- Metrics;
- Events;
- Containers;
- restart/delete;
- даты событий — `ДД.ММ.ГГГГ`.

## Multi-cluster

`local` кластер берётся из kubeconfig backend-машины. Дополнительные кластеры задаются через меню пользователя.

Поддерживаются:

1. kubeconfig path + context;
2. API server + bearer token;
3. API server + username/password.

Dex/OIDC остаётся отдельным будущим этапом и пока не включён.

## Защита credentials

Token/password шифруются AES-256-GCM перед записью в локальный registry кластеров.

Дополнительно:

- `.env` исключён из Git;
- `server/data/clusters.json` исключён из Git;
- runtime cluster store записывается с mode `0600`;
- API использует `Cache-Control: no-store`;
- содержимое CA не возвращается в списке кластеров;
- kubeconfig credentials не копируются в dashboard store.

## TypeScript

Для собственных `.ts/.tsx` файлов действует политика без `any`:

- `strict: true`;
- `noImplicitAny: true`;
- `useUnknownInCatchVariables: true`;
- `npm run check` дополнительно сканирует исходники на токен `any`;
- `@ts-ignore`, `@ts-nocheck`, `@ts-expect-error` не используются.

## API

```text
GET    /api/clusters
GET    /api/namespaces
GET    /api/deployments
GET    /api/deployments/:name
GET    /api/deployments/:name/events
PUT    /api/deployments/:name/replicas
POST   /api/deployments/:name/restart
PUT    /api/deployments/:name/resources
PUT    /api/deployments/:name
DELETE /api/deployments/:name

GET    /api/pods
GET    /api/pods/:name
GET    /api/pods/:name/logs?previous=0|1
POST   /api/pods/:name/exec
GET    /api/pods/:name/metrics
GET    /api/pods/:name/events
PUT    /api/pods/:name
DELETE /api/pods/:name

GET    /api/resource-quotas
GET    /api/configmaps
```

## Если Vite показывает 502

Сначала проверьте backend:

```bash
curl http://localhost:3001/api/health
```

Если запрос не проходит, сначала смотрите terminal backend, а уже потом меняйте Minikube/kubeconfig.

## Сброс сохранённых UI-настроек

В DevTools можно удалить ключи, начинающиеся с:

```text
quaber.matrix.
quaber.configmaps.
quaber.restartHistory.
```

Credentials кластеров хранятся backend отдельно и этим не затрагиваются.
