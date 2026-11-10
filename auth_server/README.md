# Тестовый сервер авторизации Quaber

Это локальный mock на Vite, не production identity provider. Он слушает только `127.0.0.1:4175`. Quaber использует адрес `http://127.0.0.1:4175/api/auth/verify` из `quaber.config.json`.

```sh
npm install
npm run add-user -- admin
npm run dev
```

`add-user` создаёт `users.json` с salted scrypt-хэшем и один раз печатает случайный пароль. Имя должно совпадать с тем, которое будет введено в Quaber, **включая регистр**. Файл `users.json` не находится в раздаваемом Vite каталоге `web` и добавлен в `.gitignore`; не публикуйте его. Начальный пустой шаблон — `users.json.example`.

Единственная прикладная ручка:

```http
POST /api/auth/verify
Content-Type: application/json

{"username":"admin","password":"введённый-пароль"}
```

Ответ при корректном запросе — JSON `true` или `false` с HTTP 200 и `Cache-Control: no-store`. Quaber не делает локальный fallback: при ошибке сети, редиректе, таймауте или ответе не типа boolean вход блокируется. Для реального сервера замените `authentication.url` на HTTPS URL, реализующий тот же контракт. Перезапустите Quaber после изменения конфигурации.
