"""Summarize effective rights without inferring a role from the username."""
import re


def is_resource_write(method, path):
    identity_review=(method.upper()=='POST' and path.split('?',1)[0]=='/apis/authentication.k8s.io/v1/selfsubjectreviews')
    return method.upper() in ("POST", "PUT", "PATCH", "DELETE") and not path.startswith("/apis/authorization.k8s.io/") and not identity_review


def server_forbidden(message):
    return bool(re.search(r"Error from server \(Forbidden\)|\bis forbidden: User\b", str(message), re.I))


def scope_access(status):
    if not isinstance(status.get("resourceRules"), list):
        return "Не определён", "Кластер не вернул список правил доступа.", False
    operations = []
    checks = (("Просмотр Pod", "", "pods", "list"),
              ("Просмотр Pod по имени", "", "pods", "get"),
              ("Просмотр ConfigMap", "", "configmaps", "get"),
              ("Просмотр Deployment", "apps", "deployments", "get"),
              ("Масштабирование", "apps", "deployments/scale", "update"),
              ("Deployment YAML", "apps", "deployments", "update"),
              ("Restart rollout", "apps", "deployments", "patch"),
              ("ConfigMap YAML", "", "configmaps", "update"),
              ("Удаление Pod", "", "pods", "delete"),
              ("Eviction Pod", "", "pods/eviction", "create"),
              ("Terminal", "", "pods/exec", "create"))
    for label, group, resource, verb in checks:
        for rule in status.get("resourceRules", []):
            resources = rule.get("resources", [])
            if (group in rule.get("apiGroups", []) or "*" in rule.get("apiGroups", [])) and (verb in rule.get("verbs", []) or "*" in rule.get("verbs", [])) and (
                    resource in resources or "*" in resources or ("/" in resource and "*/"+resource.split("/", 1)[1] in resources)):
                operations.append(label + (" (отдельные имена)" if rule.get("resourceNames") else ""))
                break
    complete = not status.get("incomplete") and not status.get("evaluationError")
    writes = [operation for operation in operations if not operation.startswith("Просмотр")]
    level = "Редактирование" if writes else "Чтение" if operations and complete else "Нет доступа" if complete else "Не определён"
    detail = ", ".join(operations) or (status.get("evaluationError") or "Действия приложения не разрешены")
    if not complete:
        detail += " · правила неполные"
    return level, detail, complete
