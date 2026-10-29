"""DB fixtures for shared tests: reuse the schema suite's base_db / db fixtures.
Needs TEST_ADMIN_DSN and psycopg; without them the DB tests skip."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

try:
    import psycopg  # noqa: F401
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

if _HAS_PG:
    _SRC = pathlib.Path(__file__).resolve().parents[2] / "infra" / "schema" / "tests" / "conftest.py"
    _spec = importlib.util.spec_from_file_location("schema_db_fixtures", _SRC)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    base_db = _mod.base_db
    db = _mod.db
else:
    @pytest.fixture
    def base_db():
        pytest.skip("psycopg not installed")

    @pytest.fixture
    def db():
        pytest.skip("psycopg not installed")
