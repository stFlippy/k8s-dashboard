#!/bin/sh

# Run this script as the same user that will run quaber.
set +x

errors=0

ok() {
  printf 'OK: %s\n' "$1"
}

fail() {
  printf 'ОШИБКА: %s\n' "$1" >&2
  errors=$((errors + 1))
}

trim() {
  trimmed=$1
  while :; do
    case $trimmed in
      [[:space:]]*) trimmed=${trimmed#?} ;;
      *) break ;;
    esac
  done
  while :; do
    case $trimmed in
      *[[:space:]]) trimmed=${trimmed%?} ;;
      *) break ;;
    esac
  done
}

case $0 in
  */*) script_path=$0 ;;
  *) script_path=$(command -v "$0") ;;
esac
script_dir=${script_path%/*}
[ "$script_dir" = "$script_path" ] && script_dir=.
package_dir=$(CDPATH= cd -P "$script_dir" 2>/dev/null && pwd -P)
if [ -z "$package_dir" ]; then
  printf 'ОШИБКА: не удалось определить каталог пакета Quaber.\n' >&2
  exit 1
fi

printf 'Проверка Quaber в %s\n' "$package_dir"

if command -v uname >/dev/null 2>&1; then
  os_name=$(uname -s 2>/dev/null)
  machine=$(uname -m 2>/dev/null)
  if [ "$os_name" = Linux ] && [ "$machine" = x86_64 ]; then
    ok 'Linux x86-64'
  else
    fail "нужен Linux x86-64; обнаружено: ${os_name:-неизвестная ОС} ${machine:-неизвестная архитектура}."
  fi
else
  fail 'не найдена команда uname; невозможно проверить ОС и архитектуру.'
fi

if [ -x "$package_dir/quaber" ]; then
  ok 'quaber доступен для запуска'
else
  fail 'нет исполняемого quaber рядом с check-runtime.sh; проверьте распаковку и права файла.'
fi

if [ -r "$package_dir/public/index.html" ] && [ -d "$package_dir/public/assets" ]; then
  ok 'файлы интерфейса доступны'
else
  fail 'не найден public/index.html или каталог public/assets; распакуйте архив целиком.'
fi

if [ -d "$package_dir/data" ] && [ -w "$package_dir/data" ]; then
  ok 'каталог data доступен для записи'
else
  fail 'каталог data отсутствует или недоступен для записи текущему пользователю; создайте его и исправьте владельца/права.'
fi

if [ -r "$package_dir/data/bootstrap-user.json" ] || [ -r "$package_dir/data/users.sqlite" ]; then
  ok 'найден файл первичного пользователя или база пользователей'
else
  fail 'нет data/bootstrap-user.json и data/users.sqlite; скопируйте data/bootstrap-user.example.json в data/bootstrap-user.json и задайте логин/временный пароль.'
fi

master_key=
if [ "${DASHBOARD_MASTER_KEY+x}" = x ]; then
  master_key=$DASHBOARD_MASTER_KEY
elif [ -r "$package_dir/.env" ]; then
  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    case $raw_line in
      *=*) ;;
      *) continue ;;
    esac
    env_key=${raw_line%%=*}
    trim "$env_key"
    [ "$trimmed" = DASHBOARD_MASTER_KEY ] || continue
    master_key=${raw_line#*=}
    trim "$master_key"
    master_key=$trimmed
    case $master_key in
      \"*\") master_key=${master_key#\"}; master_key=${master_key%\"} ;;
      \'*\') master_key=${master_key#\'}; master_key=${master_key%\'} ;;
    esac
    break
  done < "$package_dir/.env"
fi

if [ "${#master_key}" -ge 32 ]; then
  ok 'ключ DASHBOARD_MASTER_KEY задан'
else
  fail 'DASHBOARD_MASTER_KEY должен содержать не менее 32 символов; задайте его в .env рядом с backend или в окружении процесса.'
fi
unset master_key

if [ -e "$package_dir/addons/kubectl" ] || [ -L "$package_dir/addons/kubectl" ]; then
  if [ ! -x "$package_dir/addons/kubectl" ]; then
    fail 'addons/kubectl найден, но не исполняемый; проверьте файл и права доступа.'
  elif "$package_dir/addons/kubectl" version --client >/dev/null 2>&1; then
    ok 'addons/kubectl запускается с командой version --client'
  else
    fail 'addons/kubectl не запускается с командой version --client; проверьте ОС, архитектуру и зависимости.'
  fi
else
  printf 'ПРЕДУПРЕЖДЕНИЕ: addons/kubectl отсутствует. Quaber может запускаться, но функции кластеров недоступны до добавления исполняемого addons/kubectl.\n' >&2
fi

if [ "$errors" -eq 0 ]; then
  printf '\nПроверка пройдена. Quaber готов к запуску.\n'
  exit 0
fi

printf '\nНайдено проблем: %s. Исправьте их и повторите ./check-runtime.sh.\n' "$errors" >&2
exit 1
