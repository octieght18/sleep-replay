"""Setup, privacy, startup and documentation contracts."""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

from backend.api.runtime import bind_socket, check_python, warn_host
from backend.domain.errors import ERROR_CODES
from backend.domain.mapping import DEFAULT_MAPPING
from backend.sonification.config_parser import parse_config

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_has_no_external_assets():
    text = "\n".join(
        p.read_text() for p in (ROOT / "frontend").rglob("*") if p.is_file()
    )
    assert not re.search(r"https?://|@import\s|url\(\s*['\"]?//", text)
    shell = (ROOT / "frontend/index.html").read_text()
    assert "medical" in shell and "health" in shell
    assert 'id="disclaimer"' in shell


def test_compose_and_ignore_contract():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {"backend", "frontend"}
    for service in compose["services"].values():
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])
        source = (ROOT / service["build"]["dockerfile"]).read_text()
        assert re.search(
            r"^FROM .*:\d+\.\d+\.\d+.*@sha256:[0-9a-f]{64}$", source, re.MULTILINE
        )
    assert compose["services"]["backend"]["volumes"] == ["sleep-replay-data:/data"]
    for name in (".gitignore", ".dockerignore"):
        lines = (ROOT / name).read_text().splitlines()
        assert any(line.rstrip("/") == "sleep-replay-data" for line in lines)
        assert all(
            line.rstrip("/") not in ("sample_data", "examples") for line in lines
        )


def test_all_dependency_pins_are_exact():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pins = (
        config["project"]["dependencies"]
        + config["project"]["optional-dependencies"]["test"]
        + config["build-system"]["requires"]
    )
    pins += [
        line.split("#")[0].strip()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert all(re.match(r"^[\w-]+==\d+(?:\.\d+)+(?:;.*)?$", pin) for pin in pins)


def test_python_and_port_checks(capsys):
    with pytest.raises(SystemExit, match="Python 3.10.9 detected"):
        check_python((3, 10, 9))
    check_python((3, 11, 0))
    warn_host("127.0.0.1")
    assert not capsys.readouterr().out
    warn_host("0.0.0.0")
    assert "no authentication" in capsys.readouterr().out
    with bind_socket("127.0.0.1", 0) as occupied:
        port = occupied.getsockname()[1]
        with pytest.raises(OSError, match=str(port)):
            bind_socket("127.0.0.1", port)


def test_troubleshooting_contains_every_error_code():
    docs = (ROOT / "docs/troubleshooting.md").read_text()
    assert ERROR_CODES <= set(re.findall(r"`([A-Z_]+)`", docs))


def test_root_example_is_accepted_and_has_defaults():
    assert (
        parse_config((ROOT / "mapping-config.example.yaml").read_text())
        == DEFAULT_MAPPING
    )
