"""DB fixtures for the contract suite's database check: reuse the schema suite's base_db / db fixtures by file
path (single source of truth). Needs TEST_ADMIN_DSN; without it the DB tests skip."""
import importlib.util
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "infra" / "schema" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("schema_db_fixtures_contracts", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

base_db = _mod.base_db
db = _mod.db
