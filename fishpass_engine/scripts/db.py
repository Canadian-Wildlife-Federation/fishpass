"""Shared database connection helpers -- same FISHPASS_HOST/PORT/DBNAME/USER/PASSWORD
env-var-only convention as chyf_loader and gradient_barriers. Connection details are never
stored in a config file and never logged.
"""

import os
import re
import sys
from contextlib import contextmanager

import psycopg

from utils import IDENTIFIER_RE

_IDENT = IDENTIFIER_RE.pattern.strip("^$")
QUALIFIED_IDENT_RE = re.compile(rf"^{_IDENT}\.{_IDENT}$")

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
	utils.IDENTIFIER_RE) -- this only guards against embedded double-quotes/injection as
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


def table_columns(cursor, schema, table):
	"""Column names of <schema>.<table> (a table, view, or foreign table) as a set -- empty
	if the relation doesn't exist. RAW names. Reads pg_attribute rather than
	information_schema.columns, which hides relations the current user has no privileges on."""
	cursor.execute(
		"""
		select a.attname
		from pg_attribute a
		join pg_class c on c.oid = a.attrelid
		join pg_namespace n on n.oid = c.relnamespace
		where n.nspname = %s and c.relname = %s and a.attnum > 0 and not a.attisdropped;
		""",
		(schema, table),
	)
	return {row[0] for row in cursor.fetchall()}


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


def table_column_types(cursor, schema, table):
	"""Columns of <schema>.<table> (a table or view) in column order, as
	(name, typname, typtype, typcategory) tuples -- empty if the relation doesn't exist. RAW names.
	typtype 'e' is an enum; typcategory 'A' is an array."""
	cursor.execute(
		"""
		select a.attname, t.typname, t.typtype, t.typcategory
		from pg_attribute a
		join pg_class c on c.oid = a.attrelid
		join pg_namespace n on n.oid = c.relnamespace
		join pg_type t on t.oid = a.atttypid
		where n.nspname = %s and c.relname = %s and a.attnum > 0 and not a.attisdropped
		order by a.attnum;
		""",
		(schema, table),
	)
	return [tuple(row) for row in cursor.fetchall()]
