"""Shared database connection helpers -- same FISHPASS_HOST/PORT/DBNAME/USER/PASSWORD
env-var-only convention as chyf_loader and gradient_barriers. Connection details are never
stored in a config file and never logged.
"""

import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import psycopg

QUALIFIED_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")

REQUIRED_ENV_VARS = [
	"FISHPASS_HOST",
	"FISHPASS_PORT",
	"FISHPASS_DBNAME",
	"FISHPASS_USER",
	"FISHPASS_PASSWORD",
]


def require_env():
	missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
	if missing:
		sys.exit(f"Missing required environment variable(s): {', '.join(missing)}")


def db_connect():
	return psycopg.connect(
		host=os.environ["FISHPASS_HOST"],
		port=os.environ["FISHPASS_PORT"],
		dbname=os.environ["FISHPASS_DBNAME"],
		user=os.environ["FISHPASS_USER"],
		password=os.environ["FISHPASS_PASSWORD"],
	)


def quote_ident(identifier):
	"""Quote a SQL identifier (schema/table name) that can't be passed as a bound parameter.

	Callers must have already validated the identifier against a safe charset (see
	model_plan.IDENTIFIER_RE) -- this only guards against embedded double-quotes/injection as
	a second line of defense, it does not substitute for that validation.
	"""
	return '"' + identifier.replace('"', '""') + '"'


def quote_qualified_ident(name):
	"""Validate and quote a "<schema>.<table>" identifier that came from a model plan field
	(structure_new_table, structure_update_table, habitat_update_table) -- these are
	interpolated directly into SQL since table names can't be bound parameters, so are
	restricted to a safe charset first. Exits on an invalid name."""

	if not QUALIFIED_IDENT_RE.match(name):
		sys.exit(f"Invalid table name (expected schema.table): {name!r}")
	schema, table = name.split(".", 1)
	return f"{quote_ident(schema)}.{quote_ident(table)}"


# =================================================================================
#  SHARED CONFIG (config/fishpass.yaml)
# =================================================================================

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = REPO_ROOT / "config" / "fishpass.yaml"

ROLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Settings that a model plan may override (mirrors model_plan.WCRP_PLAN_OVERRIDES); the
# fishpass.yaml 'wcrp' section supplies the default for each.
WCRP_SETTINGS = ("label_in_wcrp_rank_threshold", "min_avg_gain_km")


def load_config(config_path=DEFAULT_CONFIG_FILE):
	"""Load config/fishpass.yaml as a dict. yaml is imported lazily so modules that only
	need the connection helpers don't require it."""
	import yaml

	if not Path(config_path).is_file():
		sys.exit(f"Config file not found: {config_path}")
	with open(config_path) as f:
		return yaml.safe_load(f) or {}


def config_section(name, config_path=DEFAULT_CONFIG_FILE):
	"""Return one top-level section of fishpass.yaml (exits if missing or not a mapping)."""
	section = load_config(config_path).get(name)
	if not isinstance(section, dict):
		sys.exit(f"Config file {config_path} is missing the '{name}' section")
	return section


def get_db_roles(config_path=DEFAULT_CONFIG_FILE):
	"""Return the database_roles section as {'owner': str, 'grant_all': tuple,
	'grant_select': tuple}. Role names are interpolated into GRANT/ALTER ... OWNER TO, so
	they are held to a safe identifier charset (and quoted by callers)."""
	section = config_section("database_roles", config_path)
	owner = section.get("owner")
	grant_all = section.get("grant_all") or []
	grant_select = section.get("grant_select") or []
	if not isinstance(owner, str) or not ROLE_NAME_RE.match(owner):
		sys.exit(f"database_roles.owner must be a valid role name, got {owner!r}")
	for key, roles in (("grant_all", grant_all), ("grant_select", grant_select)):
		if not isinstance(roles, list) or not all(
			isinstance(r, str) and ROLE_NAME_RE.match(r) for r in roles
		):
			sys.exit(f"database_roles.{key} must be a list of valid role names, got {roles!r}")
	return {"owner": owner, "grant_all": tuple(grant_all), "grant_select": tuple(grant_select)}


def wcrp_setting(plan, key, config_path=DEFAULT_CONFIG_FILE):
	"""Resolve a WCRP setting: the plan's own value wins when set (model_plan has already
	validated it); otherwise the default from fishpass.yaml's 'wcrp' section."""
	if key not in WCRP_SETTINGS:
		raise KeyError(f"Unknown WCRP setting: {key!r}")
	value = plan.get(key)
	if value is not None:
		return value
	from model_plan import wcrp_value_error  # lazy: model_plan imports yaml at load

	value = config_section("wcrp", config_path).get(key)
	error = wcrp_value_error(key, value)
	if error:
		sys.exit(f"Invalid wcrp setting in {config_path}: {error}")
	return value


# =================================================================================
#  ROLE SWITCHING + CATALOG CHECKS
# =================================================================================

@contextmanager
def as_role(conn, cursor, role):
	"""Run the wrapped block as `role` (via SET ROLE), then restore the caller's role.

	For use where the connecting user is only a granted member of `role` (no password
	needed for SET ROLE) and the connection is reused for later phases, so the role change
	must not leak past the block. When already connected AS `role` this is a no-op.

	Rolls back before RESET ROLE: a failed statement inside the block aborts the
	transaction, and RESET ROLE would itself be rejected by an aborted transaction, masking
	the real error with "current transaction is aborted, commands ignored until end of
	transaction block". Work inside the block must therefore commit what it wants to keep.
	"""
	cursor.execute(f"set role {quote_ident(role)};")
	conn.commit()
	try:
		yield
	finally:
		conn.rollback()
		cursor.execute("reset role;")
		conn.commit()


def table_exists(cursor, schema, table):
	"""True if <schema>.<table> exists. Takes RAW (unquoted) names -- they are bound
	parameters here, not interpolated."""
	cursor.execute(
		"select 1 from information_schema.tables where table_schema = %s and table_name = %s;",
		(schema, table),
	)
	return cursor.fetchone() is not None


def function_exists(cursor, schema, function):
	"""True if a function named <schema>.<function> exists (any signature). RAW names."""
	cursor.execute(
		"""
		select 1
		from pg_proc p
		join pg_namespace n on n.oid = p.pronamespace
		where n.nspname = %s and p.proname = %s;
		""",
		(schema, function),
	)
	return cursor.fetchone() is not None
