"""Size-bounded JSON / safe YAML configuration parsing, without partial application."""
from __future__ import annotations

import json
import yaml

from backend.domain.mapping import invalid_config, mapping_from_dict

MAX_CONFIG_BYTES = 64 * 1024


class _Pairs(list):
    pass


def _json_object(value, path="document"):
    if isinstance(value, _Pairs):
        result = {}
        for key, child in value:
            child_path = key if path == "document" else f"{path}.{key}"
            if key in result:
                raise invalid_config(child_path, "each key only once")
            result[key] = _json_object(child, child_path)
        return result
    if isinstance(value, list):
        return [_json_object(v, path) for v in value]
    return value


def _yaml_object(loader, node, path="document", ancestors=(), depth=0):
    if depth > 10 or id(node) in ancestors:
        raise invalid_config(path, "a shallow mapping without recursive aliases")
    if not node.tag.startswith("tag:yaml.org,2002:") or node.tag.split(":")[-1] not in ("map", "seq", "str", "null", "bool", "int", "float", "timestamp"):
        loader.construct_object(node)  # SafeLoader rejects executable/custom tags.
    if isinstance(node, yaml.MappingNode):
        result = {}
        for key_node, child in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                raise invalid_config(path, "string keys")
            key = loader.construct_object(key_node)
            if not isinstance(key, str):
                raise invalid_config(path, "string keys")
            child_path = key if path == "document" else f"{path}.{key}"
            if key in result:
                raise invalid_config(child_path, "each key only once")
            result[key] = _yaml_object(loader, child, child_path, (*ancestors, id(node)), depth + 1)
        return result
    if isinstance(node, yaml.SequenceNode):
        raise invalid_config(path, "a mapping, not a sequence")
    return loader.construct_object(node)


def parse_config(document: str | bytes):
    try:
        raw = document.encode("utf-8") if isinstance(document, str) else document
        if len(raw) > MAX_CONFIG_BYTES:
            raise invalid_config("document", f"UTF-8 documents of at most {MAX_CONFIG_BYTES} bytes (64 KB)")
        text = raw.decode("utf-8")
    except UnicodeError:
        raise invalid_config("document", "UTF-8 encoding") from None
    try:
        data = _json_object(json.loads(text, object_pairs_hook=_Pairs))
    except json.JSONDecodeError:
        loader = yaml.SafeLoader(text)
        try:
            node = loader.get_single_node()
            data = {} if node is None or (isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:null" and node.value == "") else _yaml_object(loader, node)
        except yaml.YAMLError as err:
            line = getattr(getattr(err, "problem_mark", None), "line", 0) + 1
            raise invalid_config(f"line {line}", "valid JSON or safe YAML mappings") from None
        except (ValueError, RecursionError, OverflowError):
            raise invalid_config("document", "finite numeric values and shallow mappings") from None
        finally:
            loader.dispose()
    except (ValueError, RecursionError):
        raise invalid_config("document", "finite numeric values and shallow mappings") from None
    return mapping_from_dict(data)
