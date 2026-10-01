"""Canonical YAML; JSON numeric spellings preserve every accepted finite float."""
import json

from backend.domain.mapping import mapping_dict, validate_mapping


def print_config(config) -> str:
    validate_mapping(config)
    lines = []
    for key, value in mapping_dict(config).items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for name, v in value.items():
                # Quoting 'none' avoids resolvers treating it as a null value.
                if name in ("sensitivity", "hysteresis"):
                    number = repr(float(v)) if v else "0.0"
                    if "e" in number:
                        mantissa, exponent = number.split("e")
                        number = (mantissa if "." in mantissa else mantissa + ".0") + "e" + exponent
                    text = number
                else:
                    text = json.dumps(v, allow_nan=False)
                lines.append(f"  {name}: {text}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"
