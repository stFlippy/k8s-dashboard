"""Initial selection for Pods with one application container and an Istio sidecar."""


def preferred_container(names):
    names = list(names)
    if len(names) == 2 and any("istio" in name.casefold() for name in names):
        return next((name for name in names if "istio" not in name.casefold()), names[0])
    return names[0] if names else ""
