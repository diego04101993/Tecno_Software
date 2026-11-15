"""Typed interpretation of optional Docker inspect Config fields."""

from release_state import require


OPTIONAL_CONFIG_TYPES = {
    "Cmd": list,
    "Entrypoint": list,
    "Env": list,
    "Labels": dict,
    "OnBuild": list,
    "User": str,
    "Volumes": dict,
    "WorkingDir": str,
}


def parse_optional_config(config, *, validate=require):
    """Interpret omitted and null fields without changing the exact Config evidence."""
    validate(type(config) is dict, "DOCKER_CONFIG_INVALID")
    parsed = {}
    for field, expected_type in OPTIONAL_CONFIG_TYPES.items():
        value = config.get(field)
        if value is None:
            value = expected_type()
        error = f"DOCKER_CONFIG_{field.upper()}_INVALID"
        validate(type(value) is expected_type, error)
        if expected_type is list:
            validate(all(type(entry) is str for entry in value), error)
            value = value.copy()
        elif field == "Labels":
            validate(all(type(key) is str and type(entry) is str for key, entry in value.items()), error)
            value = value.copy()
        elif field == "Volumes":
            validate(all(type(key) is str and type(entry) is dict and not entry
                         for key, entry in value.items()), error)
            value = {path: {} for path in value}
        parsed[field] = value
    return parsed
