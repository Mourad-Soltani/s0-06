"""DB fixtures for the bus tests: reuse the schema suite's base_db / db fixtures (template database with
every migration applied, cloned per test). Loaded by file path so there is a single source of truth.
Needs TEST_ADMIN_DSN (superuser); without it the DB tests skip, exactly like the schema tests."""
import importlib.util
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "infra" / "schema" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("schema_db_fixtures", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

base_db = _mod.base_db   # session: template DB, all migrations applied
db = _mod.db             # per test: a clone, with .conn (autocommit superuser) and .dsn
