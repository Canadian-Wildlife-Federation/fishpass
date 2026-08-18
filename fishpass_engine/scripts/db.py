"""Shared database connection helpers -- same FISHPASS_HOST/PORT/DBNAME/USER/PASSWORD
env-var-only convention as chyf_loader and gradient_barriers. Connection details are never
stored in a config file and never logged.
"""

import os
import re
import sys

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
