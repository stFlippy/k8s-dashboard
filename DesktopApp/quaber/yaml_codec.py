"""Safe UI serialization using LibYAML when available."""
import yaml

_SAFE_DUMPER = getattr(yaml, 'CSafeDumper', yaml.SafeDumper)


def dump_resource(value, *, allow_unicode=True, sort_keys=False):
    return yaml.dump(value, Dumper=_SAFE_DUMPER, allow_unicode=allow_unicode, sort_keys=sort_keys)
