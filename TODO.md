### 17. P2: передача и обработка diagnostics могут раскрывать credentials

Источники: [kubectl-login-cli.ts:18](server/src/kubernetes/kubectl-login-cli.ts#L18), [kubectl-login-cli.ts:48](server/src/kubernetes/kubectl-login-cli.ts#L48), [logger.ts:39](server/src/logging/logger.ts#L39), [addons/kubectl:293](addons/kubectl#L293).

Пароль передаётся через argv и доступен локальным пользователям/процессам с правом читать аргументы. Это уже документировано; helper умеет читать пароль из stdin. При ошибке stdout/stderr маскируются только по точному совпадению введённого пароля; токены, client secrets и kubeconfig, если helper их выведет, попадут в журнал и текст API-ошибки. describeKubectlLoginFailure существует и тестируется, но в этом пути не используется.

Фактическая утечка токена в рабочих журналах не устанавливалась; подтверждён небезопасный путь обработки. Исправление: stdin для поддерживающего его helper, безопасная классификация клиентских ошибок, редактирование чувствительных значений в diagnostics и ограничение доступа к журналам.

TODO: Пока что не трогай, проанализируй проблему с чем связана, и возможные пути решения
DONE: По этому TODO код diagnostics не менялся. Пароль в `--password` виден в argv локальным процессам с соответствующими правами; точная замена строки пароля в stderr/stdout не защищает от отражённых токенов, client secret или частей kubeconfig. Помочь могут stdin для совместимого helper, минимальные права ОС и изоляция процессов, классификация ошибок вместо возврата сырого stderr клиенту, строгая редакция JWT/PEM/Authorization в диагностике, лимит объёма и доступ только администратору. Для корпоративного бинарника сначала нужен тест совместимости stdin, иначе изменение может сломать вход.


Сделать вывод неймспейса в 2 строки в таблице

при редактировании configmap "сохранить" в углу где должен быть крестик, крестик под сохранить

в кубконфиге выводит только один хост, и неймспейс, а не все сразу

выгрузка сразу всех вкалдок, а не одной

счетчик тотал не учитывает фильтрацию

2026-10-29T08:19:44.259Z ERROR action="API request failed" user="admin" userId="128553ce- f6a5-4983-be8a-229ad00589ae" status="500" message="HTTP-Code: 406 { \"kind\": \"Status\", \"apiversion\":\"v1\",\"metadata\":{}, \"Failure\", \"messageI": \"only the following media types are accepted: application" 

request failed" user="admin" userId="128553ce-f6a5-4983-be8a-229ad00589ae" status="50"v1I", I"metadata\": ), I"status\": I"Failure\",