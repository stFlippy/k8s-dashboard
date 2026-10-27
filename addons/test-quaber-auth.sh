#!/usr/bin/env bash

set -u
set -o pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
KUBECTL="$SCRIPT_DIR/kubectl"
KUBECONFIG_FILE="${KUBECONFIG:-${HOME:?HOME is not set}/.kube/config}"

die() { printf '[ОШИБКА] %s\n' "$*" >&2; exit 1; }
need_command() { command -v "$1" >/dev/null 2>&1 || die "не найдена команда: $1"; }
[[ -x "$KUBECTL" ]] || die "не найден исполняемый helper: $KUBECTL"
need_command curl

if command -v jq >/dev/null 2>&1; then
  JSON_TOOL=jq
elif command -v python3 >/dev/null 2>&1; then
  JSON_TOOL=python3
else
  die 'для разбора JSON требуется jq или python3'
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
RESULT_DIR="$SCRIPT_DIR/quaber-auth-test-$STAMP-$$"
mkdir -m 700 -- "$RESULT_DIR" || die "не удалось создать $RESULT_DIR"
LOG="$RESULT_DIR/quaber-auth-test.log"
TOKEN_FILE="$RESULT_DIR/.token"
AUTH_CONFIG="$RESULT_DIR/.curl-auth"
TOKEN=''

cleanup() {
  TOKEN=''
  rm -f -- "$TOKEN_FILE" "$AUTH_CONFIG"
}
trap cleanup EXIT HUP INT TERM

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"
}

json_names() {
  local file="$1" kind="$2"
  if [[ "$JSON_TOOL" == jq ]]; then
    jq -r '.items[]?.metadata.name // empty' "$file" 2>/dev/null
  else
    python3 - "$file" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as source:
    data = json.load(source)
for item in data.get('items', []):
    name = item.get('metadata', {}).get('name')
    if isinstance(name, str):
        print(name)
PY
  fi
}

json_node_name() {
  local file="$1"
  if [[ "$JSON_TOOL" == jq ]]; then
    jq -r '.spec.nodeName // empty' "$file" 2>/dev/null
  else
    python3 - "$file" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as source:
    value = json.load(source).get('spec', {}).get('nodeName', '')
if isinstance(value, str):
    print(value)
PY
  fi
}

has_metric_containers() {
  local file="$1"
  if [[ "$JSON_TOOL" == jq ]]; then
    jq -e '.containers | type == "array"' "$file" >/dev/null 2>&1
  else
    python3 - "$file" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as source:
    value = json.load(source).get('containers')
raise SystemExit(0 if isinstance(value, list) else 1)
PY
  fi
}

print_error_excerpt() {
  local title="$1" body="$2" code="$3"
  printf '\n[HTTP ОШИБКА] %s: HTTP %s\n' "$title" "$code" | tee -a "$LOG" >&2
  printf 'Headers: %s.headers\nBody: %s\n' "$body" "$body" >&2
  if [[ -s "$body" ]]; then
    sed -n '1,8p' "$body" | cut -c1-500 | tee -a "$LOG" >&2
  else
    printf '(тело ответа пустое)\n' >&2
  fi
}

API_TLS=verify
API_CA=''
HTTP_CODE=000
curl_get() {
  local url="$1" output="$2"
  local -a args=(curl --silent --show-error --connect-timeout 10 --max-time 30
    --config "$AUTH_CONFIG"
    --dump-header "$output.headers" --header 'Accept: application/json'
    --output "$output" --write-out '%{http_code}')
  if [[ "$API_TLS" == insecure ]]; then
    args+=(--insecure)
  elif [[ -n "$API_CA" ]]; then
    args+=(--cacert "$API_CA")
  fi
  HTTP_CODE="$("${args[@]}" "$url" 2>>"$LOG")" || HTTP_CODE=000
}

choose_item() {
  local title="$1" file="$2" result_name="$3"
  local -a items=()
  mapfile -t items < <(json_names "$file" "$title")
  ((${#items[@]} > 0)) || return 1
  printf '\n%s:\n' "$title"
  local index
  for index in "${!items[@]}"; do printf '  %d. %s\n' "$((index + 1))" "${items[index]}"; done
  local choice
  while true; do
    read -r -p "Выберите номер (1-${#items[@]}): " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#items[@]})); then
      printf -v "$result_name" '%s' "${items[choice - 1]}"
      return 0
    fi
    printf 'Введите номер от 1 до %d.\n' "${#items[@]}" >&2
  done
}

log 'Запущена Linux-проверка авторизации Quaber'
log "Результаты: $RESULT_DIR"
log 'Пароль и полный токен в журнал не записываются'
printf '\nВ Kubernetes выполняются только read-only GET-запросы.\n\n'

read -r -p 'API server (например https://192.168.56.11:6443): ' API_SERVER
[[ -n "$API_SERVER" ]] || die 'API server не указан'
API_SERVER="${API_SERVER%/}"
read -r -p 'Логин Kubernetes / Dex: ' LOGIN_NAME
[[ -n "$LOGIN_NAME" ]] || die 'логин не указан'

printf '\nПроверка TLS Kubernetes API:\n  1. Проверять сертификат (рекомендуется)\n  2. Не проверять сертификат\n'
read -r -p 'Выберите 1 или 2: ' tls_choice
case "$tls_choice" in
  1)
    API_TLS=verify
    default_ca=''
    [[ "$API_SERVER" == 'https://192.168.56.11:6443' ]] && default_ca='/home/flippy/k8s-lab/kubeconfigs/k8s-lab-1-ca.crt'
    [[ "$API_SERVER" == 'https://192.168.56.12:6443' ]] && default_ca='/home/flippy/k8s-lab/kubeconfigs/k8s-lab-2-ca.crt'
    if [[ -n "$default_ca" ]]; then
      read -r -p "CA Kubernetes API [$default_ca]: " API_CA
      API_CA="${API_CA:-$default_ca}"
    else
      read -r -p 'Путь к CA API сервера (Enter = системное хранилище): ' API_CA
    fi
    [[ -z "$API_CA" || -r "$API_CA" ]] || die "CA не читается: $API_CA"
    ;;
  2) API_TLS=insecure ;;
  *) die 'нужно выбрать 1 или 2' ;;
esac

LOGIN_ARGS=(login --server "$API_SERVER" --username "$LOGIN_NAME")

printf '\nРезультат login:\n  1. --onlytoken\n  2. Полный kubeconfig, как в текущем маршруте Quaber\n'
read -r -p 'Выберите 1 или 2: ' login_choice

log "API server: $API_SERVER"
log "Логин: $LOGIN_NAME"
log "TLS API: $API_TLS"
log "Запуск kubectl login"
printf '\nПароль запросит сам kubectl; ввод не отображается.\n'
case "$login_choice" in
  1)
    "$KUBECTL" "${LOGIN_ARGS[@]}" --onlytoken >"$TOKEN_FILE" || die 'kubectl login завершился ошибкой'
    ;;
  2)
    printf 'ВНИМАНИЕ: будет обновлён %s\n' "$KUBECONFIG_FILE"
    KUBECONFIG="$KUBECONFIG_FILE" "$KUBECTL" "${LOGIN_ARGS[@]}" || die 'kubectl login завершился ошибкой'
    [[ -s "$KUBECONFIG_FILE" ]] || die "kubeconfig не создан: $KUBECONFIG_FILE"
    KUBECONFIG="$KUBECONFIG_FILE" "$KUBECTL" config view --raw --minify -o 'jsonpath={.users[0].user.token}' >"$TOKEN_FILE" || die 'не удалось прочитать token из kubeconfig'
    ;;
  *) die 'нужно выбрать 1 или 2' ;;
esac
TOKEN="$(tr -d '\r\n' <"$TOKEN_FILE")"
[[ -n "$TOKEN" ]] || die 'kubectl вернул пустой token'
log "Токен получен (${#TOKEN} символов; значение скрыто)"
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" >"$AUTH_CONFIG"
chmod 600 "$AUTH_CONFIG"
TOKEN=''
rm -f -- "$TOKEN_FILE"

log 'GET /version'
curl_get "$API_SERVER/version" "$RESULT_DIR/api-version.json"
if [[ "$HTTP_CODE" != 200 ]]; then print_error_excerpt 'Kubernetes version' "$RESULT_DIR/api-version.json" "$HTTP_CODE"; exit 1; fi
log 'OK: /version вернул HTTP 200'

log 'GET /api/v1/namespaces?pretty=true'
curl_get "$API_SERVER/api/v1/namespaces?pretty=true" "$RESULT_DIR/namespaces.json"
if [[ "$HTTP_CODE" != 200 ]]; then print_error_excerpt 'Namespace list' "$RESULT_DIR/namespaces.json" "$HTTP_CODE"; exit 1; fi
choose_item 'Доступные Namespace' "$RESULT_DIR/namespaces.json" NAMESPACE || die 'API не вернул доступных Namespace'
log "Выбран Namespace: $NAMESPACE"

log "GET /api/v1/namespaces/$NAMESPACE/pods?pretty=true"
curl_get "$API_SERVER/api/v1/namespaces/$NAMESPACE/pods?pretty=true" "$RESULT_DIR/pods.json"
if [[ "$HTTP_CODE" != 200 ]]; then print_error_excerpt 'Pod list' "$RESULT_DIR/pods.json" "$HTTP_CODE"; exit 1; fi
choose_item "Pods в $NAMESPACE" "$RESULT_DIR/pods.json" POD_NAME || die 'в Namespace нет Pods'
log "Выбран Pod: $NAMESPACE/$POD_NAME"

log "GET /api/v1/namespaces/$NAMESPACE/pods/$POD_NAME"
curl_get "$API_SERVER/api/v1/namespaces/$NAMESPACE/pods/$POD_NAME" "$RESULT_DIR/selected-pod.json"
if [[ "$HTTP_CODE" != 200 ]]; then print_error_excerpt 'Selected Pod' "$RESULT_DIR/selected-pod.json" "$HTTP_CODE"; exit 1; fi
NODE_NAME="$(json_node_name "$RESULT_DIR/selected-pod.json")"
[[ -z "$NODE_NAME" ]] || log "Нода Pod: $NODE_NAME"

log "GET /apis/metrics.k8s.io/v1beta1/namespaces/$NAMESPACE/pods/$POD_NAME"
curl_get "$API_SERVER/apis/metrics.k8s.io/v1beta1/namespaces/$NAMESPACE/pods/$POD_NAME" "$RESULT_DIR/pod-metrics-api.json"
METRICS_CODE="$HTTP_CODE"
if [[ "$METRICS_CODE" == 200 ]] && has_metric_containers "$RESULT_DIR/pod-metrics-api.json"; then
  log 'OK: Metrics API вернул метрики CPU/RAM'
else
  log "INFO: Metrics API недоступен или неполон (HTTP $METRICS_CODE); проверяется kubelet fallback"
  if [[ -n "$NODE_NAME" ]]; then
    log "GET /api/v1/nodes/$NODE_NAME/proxy/stats/summary"
    curl_get "$API_SERVER/api/v1/nodes/$NODE_NAME/proxy/stats/summary" "$RESULT_DIR/kubelet-stats-summary.json"
    [[ "$HTTP_CODE" == 200 ]] && log 'OK: kubelet stats/summary доступен' || log "INFO: kubelet stats/summary вернул HTTP $HTTP_CODE"
  fi
fi

if [[ -n "$NODE_NAME" ]]; then
  log "GET /api/v1/nodes/$NODE_NAME/proxy/metrics/cadvisor"
  curl_get "$API_SERVER/api/v1/nodes/$NODE_NAME/proxy/metrics/cadvisor" "$RESULT_DIR/cadvisor-metrics.txt"
  [[ "$HTTP_CODE" == 200 ]] && log 'OK: cAdvisor доступен' || log "INFO: cAdvisor вернул HTTP $HTTP_CODE"
fi

log 'SUCCESS: маршрут авторизации и read-only API-запросы Quaber проверены'
printf '\nПроверка завершена.\nЖурнал: %s\nОтветы API: %s\n' "$LOG" "$RESULT_DIR"
