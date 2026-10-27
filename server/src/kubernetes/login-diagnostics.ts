function outputLines(output: string): string[] {
  return output
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, ' ')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export function describeKubectlLoginFailure(stderr: string, stdout: string, code: number | null): string {
  const lines = [...outputLines(stderr), ...outputLines(stdout)];
  const output = lines.join(' ');

  if (/invalid username or password|incorrect password|wrong password|неверн(?:ый|ое) (?:логин|пароль)/i.test(output)) {
    return 'Неверный логин или пароль кластера.';
  }
  if (/unknown (?:flag|command)|flag provided but not defined|unknown shorthand flag|see ['"]?kubectl login --help|^usage:/im.test(output)) {
    return 'kubectl login не принял параметры вызова. Проверьте, что addons/kubectl поддерживает login URL --username USER --password PASS.';
  }
  if (/requires? (?:a )?(?:tty|terminal)|no tty|not a tty|interactive (?:terminal|input)/i.test(output)) {
    return 'kubectl login требует интерактивный терминал. Нужен клиент, поддерживающий неинтерактивный вызов с --password.';
  }
  if (/\bx509\b|certificate|tls handshake|сертификат/i.test(output)) {
    return 'kubectl login не смог проверить TLS-сертификат или установить защищённое соединение. Проверьте доверенные CA и адрес API.';
  }
  if (/connection refused|no route to host|network is unreachable|connection timed out|i\/o timeout|context deadline exceeded/i.test(output)) {
    return 'kubectl login не смог подключиться к API-серверу. Проверьте адрес, сеть и порт.';
  }
  if (/\bunauthorized\b|\bforbidden\b|access denied|доступ запрещ[её]н/i.test(output)) {
    return 'Сервер отклонил авторизацию kubectl login. Проверьте учётные данные и права доступа.';
  }
  const httpCode = output.match(/\b(?:HTTP(?:-Code)?|status)\s*[:= ]\s*(\d{3})\b/i)?.[1];
  if (httpCode) return `kubectl login получил HTTP ${httpCode}. Проверьте адрес API-сервера и настройки авторизации.`;

  const result = `kubectl login завершился с кодом ${code ?? 'unknown'}`;
  if (lines.length === 0) {
    return `${result} без диагностического вывода. Проверьте, поддерживает ли ваш kubectl неинтерактивный вызов login URL --username USER --password PASS.`;
  }
  return `${result}; клиент не вернул безопасное для показа сообщение. Проверьте вывод kubectl login непосредственно на сервере.`;
}
