"""Web connection budgets must not silently enable pooling in durable workers."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from korfbal import settings as application_settings
from korfbal.settings import services


def read_services() -> ModuleType:
    """Evaluate configuration independently without mutating Django's settings."""
    spec = importlib.util.spec_from_file_location(
        "korfbal.settings.pool_probe", services.__file__
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value", [None, "0", "1", "8"])
def test_database_pool_is_explicit_and_bounded(
    value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers default to direct connections; web opts into a per-process ceiling."""
    if value is None:
        monkeypatch.delenv("KORFBAL_DB_POOL_MAX_SIZE", raising=False)
    else:
        monkeypatch.setenv("KORFBAL_DB_POOL_MAX_SIZE", value)
    database = read_services().DATABASES["default"]
    if value in {None, "0"}:
        assert "OPTIONS" not in database
    else:
        assert database["OPTIONS"]["pool"] == {
            "min_size": 0,
            "max_size": int(value),
            "timeout": 5,
        }
        assert database.get("CONN_MAX_AGE", 0) == 0


def test_negative_pool_limit_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not become an unbounded pool or silently disable the budget."""
    monkeypatch.setenv("KORFBAL_DB_POOL_MAX_SIZE", "-1")
    with pytest.raises(ValueError, match="non-negative"):
        read_services()


def test_production_pool_budget_is_only_applied_to_web() -> None:
    """Celery's session-scoped advisory locks retain direct PostgreSQL connections."""
    project = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((project / "docker-compose.prod.yaml").read_text())
    for name, service in compose["services"].items():
        environment = service.get("environment", {})
        if isinstance(environment, dict):
            if name == "kwt-uwsgi":
                assert (
                    environment["KORFBAL_DB_POOL_MAX_SIZE"]
                    == "${KORFBAL_WEB_DB_POOL_MAX_SIZE:-8}"
                )
            else:
                assert "KORFBAL_DB_POOL_MAX_SIZE" not in environment


def test_loadtest_preserves_application_service_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep production pool/cache options while isolating every measured service."""
    pool = {"pool": {"min_size": 0, "max_size": 8, "timeout": 5}}
    monkeypatch.setattr(
        application_settings, "DATABASES", {"default": {"OPTIONS": pool}}
    )
    for key, value in {
        "KORFBAL_LOADTEST": "isolated",
        "KORFBAL_LOADTEST_SECRET": "synthetic",
        "KORFBAL_LOADTEST_DB_PORT": "54321",
        "KORFBAL_LOADTEST_VALKEY_PORT": "54322",
        "KORFBAL_LOADTEST_ORIGIN": "http://127.0.0.1:54323",
    }.items():
        monkeypatch.setenv(key, value)
    path = Path(__file__).resolve().parents[2] / "loadtest" / "settings.py"
    spec = importlib.util.spec_from_file_location("loadtest.settings_probe", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DATABASES["default"]["OPTIONS"] == pool
    assert module.DATABASES["default"]["NAME"] == "korfbal_loadtest"
    assert module.DATABASES["default"]["HOST"] == "127.0.0.1"
    assert module.CACHES["public_live"]["LOCATION"] == "redis://127.0.0.1:54322/1"
    assert (
        module.CACHES["public_live"]["OPTIONS"]
        == application_settings.CACHES["public_live"]["OPTIONS"]
    )
