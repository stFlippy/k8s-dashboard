"""Application operation policy, independent of kubectl/proxy transport."""
import copy
import math


def validate_document(value):
    active = set()
    remaining = [50000]
    def visit(node, depth):
        remaining[0] -= 1
        if depth > 40 or remaining[0] < 0:
            raise ValueError("YAML превышает допустимую глубину/число элементов.")
        if isinstance(node, (dict, list)):
            if id(node) in active:
                raise ValueError("Циклические YAML aliases не поддерживаются.")
            active.add(id(node))
            for child in (node.values() if isinstance(node, dict) else node):
                visit(child, depth+1)
            active.remove(id(node))
    visit(value, 0)
from .metrics import cpu_cores, memory_bytes


def check_identity(resource, uid, version=None):
    metadata = resource.get("metadata", {})
    if not uid or metadata.get("uid") != uid or (version is not None and metadata.get("resourceVersion") != version):
        raise RuntimeError("Ресурс изменился после просмотра. Откройте карточку заново.")


def validate_resources(resources):
    for name, parser in (("cpu", cpu_cores), ("memory", memory_bytes)):
        values = {}
        for category in ("requests", "limits"):
            value = resources.get(category, {}).get(name)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > 100:
                raise ValueError(f"Некорректное значение {category}/{name}.")
            amount = parser(value)
            if amount is None or not math.isfinite(amount) or amount <= 0 or (name == "memory" and (value.endswith("m") or amount < 1)):
                raise ValueError(f"Некорректное значение {category}/{name}: CPU — cores/m, RAM — bytes/Mi/Gi; значение должно быть положительным.")
            values[category] = amount
        if "requests" in values and "limits" in values and values["requests"] > values["limits"]:
            raise ValueError(f"{name}: request не может быть больше limit.")


def resource_changes(original, fields, remove_limits=False):
    result = copy.deepcopy(original)
    for (category, resource), value in fields.items():
        value = value.strip()
        bucket = result.setdefault(category, {})
        if value:
            bucket[resource] = value
        elif resource in bucket:
            if category == "limits" and not remove_limits:
                raise ValueError("Для удаления limit явно включите «Снять пустые лимиты».")
            bucket.pop(resource)
        if not bucket:
            result.pop(category, None)
    validate_resources(result)
    return result
