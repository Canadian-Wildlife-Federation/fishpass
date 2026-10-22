#!/usr/bin/env python3
"""Export a model run's results to Azure Blob Storage (fishpass_engine/docs/azure_exports.md).

Two control tables in the plan's output schema drive the export, so it only needs the database:

  * <output_schema>.azure_exports -- one row per file: file_name, sql, format (csv/geopackage).
    The scripts that create each output add its row during the model run.
  * <output_schema>.run_metadata  -- one row of run details (run date/time, GitHub user/branch/
    commit, runtime, and the plan + species parameter files used for the run).

Files go to the container `fishpass-<plan code>` under
`runs/<type>/<code>[_<version>]_<yyyymmddhhmmss>/`, named with the same prefix.

Azure sign-in uses DefaultAzureCredential: on GitHub this is the azure/login (OIDC) session; locally
it is the user's `az login`. No Azure secret is stored. The storage account comes from the
AZURE_STORAGE_ACCOUNT_URL environment variable. GeoPackages are written with GDAL's ogr2ogr, which
must be on PATH.

Run on its own (the export-only GitHub action) with:
    python azure_export.py <plan_code> --export-type versioned --version v1.2
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from db import db_connect, quote_ident, require_env, table_exists
from utils import (
	MODELS_DIR,
	SAFE_NAME_RE,
	SPECIES_PARAMS_FILE,
	format_runtime,
	run_details,
	setup_logging,
	write_job_summary,
)

logger = logging.getLogger(__name__)

EXPORTS_TABLE = "azure_exports"
METADATA_TABLE = "run_metadata"

EXPORT_TYPES = ("versioned", "unversioned")
VERSION_RE = re.compile(r"^[A-Za-z0-9_.]{1,16}$")

# format -> (file extension, blob content type)
FORMATS = {
	"csv": ("csv", "text/csv"),
	"geopackage": ("gpkg", "application/geopackage+sqlite3"),
}
YAML_CONTENT_TYPE = "application/yaml"

CONTAINER_PREFIX = "fishpass-"
CONTAINER_MAX_LENGTH = 63

ACCOUNT_URL_ENV = "AZURE_STORAGE_ACCOUNT_URL"

# Separator for list (array) columns -- not a comma, which is the CSV column separator.
LIST_SEPARATOR = "|"


# =================================================================================
#  CONTROL TABLES (written during the model run)
# =================================================================================
def create_export_tables(cursor, output_schema):
	"""Create the empty azure_exports and run_metadata tables. Called right after the output
	schema is rebuilt, so both start empty on every model run."""
	schema = quote_ident(output_schema)
	cursor.execute(f"""
		CREATE TABLE {schema}.{EXPORTS_TABLE} (
			id serial PRIMARY KEY,
			file_name varchar NOT NULL UNIQUE,
			sql text NOT NULL,
			format varchar NOT NULL CHECK (format IN ('csv', 'geopackage'))
		);
	""")
	cursor.execute(f"""
		CREATE TABLE {schema}.{METADATA_TABLE} (
			run_datetime timestamptz NOT NULL,
			github_user varchar,
			github_branch varchar,
			commit_sha varchar,
			plan_code varchar NOT NULL,
			total_runtime interval,
			model_plan_file text,
			species_parameter_file text
		);
	""")


def add_export(cursor, output_schema, file_name, sql, fmt):
	"""Add one file to <output_schema>.azure_exports."""
	if not SAFE_NAME_RE.match(file_name):
		sys.exit(f"Invalid export file name (letters, numbers and _ only): {file_name!r}")
	if fmt not in FORMATS:
		sys.exit(f"Invalid export format {fmt!r} (expected one of {', '.join(FORMATS)})")
	cursor.execute(
		f"INSERT INTO {quote_ident(output_schema)}.{EXPORTS_TABLE} (file_name, sql, format) VALUES (%s, %s, %s);",
		(file_name, sql, fmt),
	)


def write_run_metadata(cursor, output_schema, plan_code, run_started, total_runtime):
	"""Save the run details to <output_schema>.run_metadata (one row). `run_started` is a
	timezone-aware datetime and `total_runtime` a timedelta."""
	model_plan_file = (MODELS_DIR / f"{plan_code}.yaml").read_text(encoding="utf-8")
	species_parameter_file = SPECIES_PARAMS_FILE.read_text(encoding="utf-8")
	user, branch, sha = run_details()
	schema = quote_ident(output_schema)
	cursor.execute(f"DELETE FROM {schema}.{METADATA_TABLE};")
	cursor.execute(
		f"""
		INSERT INTO {schema}.{METADATA_TABLE}
			(run_datetime, github_user, github_branch, commit_sha, plan_code, total_runtime,
			model_plan_file, species_parameter_file)
		VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
		""",
		(run_started, user, branch, sha, plan_code, total_runtime, model_plan_file, species_parameter_file),
	)


# =================================================================================
#  SQL HELPERS (used by the scripts that add rows to azure_exports)
# =================================================================================
def pipe_list(column, alias=None):
	"""A list (array) column written as `|`-separated text."""
	return f"array_to_string({quote_ident(column)}, '{LIST_SEPARATOR}') AS {quote_ident(alias or column)}"


def as_text(column, alias=None):
	"""A uuid / enum column cast to text (GeoPackage has neither type)."""
	return f"{quote_ident(column)}::text AS {quote_ident(alias or column)}"


def column(name, alias=None):
	if alias and alias != name:
		return f"{quote_ident(name)} AS {quote_ident(alias)}"
	return quote_ident(name)


def select_sql(columns, relation):
	"""SELECT <columns> FROM <relation> -- `relation` must already be quoted."""
	return "SELECT\n    " + ",\n    ".join(columns) + f"\nFROM {relation}"


def catalog_column_expr(name, typname, typtype, typcategory, geometry_column):
	"""The export expression for one column read from the catalog (see db.table_column_types),
	or None to leave it out. The chosen geometry column is renamed `geometry`; any other
	geometry column is dropped (a GeoPackage layer has one geometry)."""
	if typname in ("geometry", "geography"):
		return column(name, "geometry") if name == geometry_column else None
	if typcategory == "A":
		return pipe_list(name)
	if typname in ("uuid", "json", "jsonb") or typtype == "e":
		return as_text(name)
	return column(name)


# =================================================================================
#  NAMING + VALIDATION
# =================================================================================
def validate_export_args(export_type, version):
	"""Check the export type/version and return the version to use (None when unversioned --
	any version given with `unversioned` is ignored). Exits on invalid input."""
	if export_type not in EXPORT_TYPES:
		sys.exit(f"Invalid export type {export_type!r} (expected versioned or unversioned)")
	if export_type == "unversioned":
		if version:
			logger.warning("Ignoring version %r: export type is unversioned.", version)
		return None
	if not version:
		sys.exit("A version is required when the export type is versioned.")
	if not VERSION_RE.match(version):
		sys.exit(f"Invalid version {version!r}: at most 16 characters, using only letters, numbers, _ and .")
	return version


def container_name(plan_code):
	"""The Azure container for a plan: the plan code lower-cased, spaces and _ turned into -,
	other characters removed, repeated - collapsed, and prefixed with fishpass-."""
	name = plan_code.lower()
	name = re.sub(r"[ _]", "-", name)
	name = re.sub(r"[^a-z0-9-]", "", name)
	name = re.sub(r"-+", "-", name).strip("-")
	if not name:
		sys.exit(f"Plan code {plan_code!r} gives an empty Azure container name.")
	name = CONTAINER_PREFIX + name
	if len(name) > CONTAINER_MAX_LENGTH:
		sys.exit(f"Azure container name {name!r} is {len(name)} characters; the maximum is {CONTAINER_MAX_LENGTH}.")
	return name


def utc_stamp(dt):
	return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def utc_iso(dt):
	return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_folder(plan_code, version, run_datetime):
	"""<code>[_<version>]_<yyyymmddhhmmss> -- the folder name, also the prefix of every file."""
	parts = [plan_code] + ([version] if version else []) + [utc_stamp(run_datetime)]
	return "_".join(parts)


def blob_name(export_type, folder, file_name):
	"""runs/<type>/<folder>/<folder>_<file_name> (file_name includes the extension)."""
	return f"runs/{export_type}/{folder}/{folder}_{file_name}"


def _yaml_value(value):
	# JSON double-quoted strings are valid YAML double-quoted strings, so text is always read
	# back as text (e.g. a version of 1.2 or a runtime of 12:34:48).
	return "null" if value is None else json.dumps(str(value))


def metadata_yaml(meta, export_type, version, export_datetime):
	"""The metadata.yaml content, in the field order given in azure_exports.md."""
	fields = [
		("plan_code", meta["plan_code"]),
		("export_type", export_type),
		("version", version),
		("run_datetime", utc_iso(meta["run_datetime"])),
		("export_datetime", utc_iso(export_datetime)),
		("total_runtime", format_runtime(meta["total_runtime"])),
		("github_user", meta["github_user"]),
		("github_branch", meta["github_branch"]),
		("commit_sha", meta["commit_sha"]),
	]
	return "".join(f"{key}: {_yaml_value(value)}\n" for key, value in fields)


# =================================================================================
#  PRE-FLIGHT CHECKS
# =================================================================================
def check_ogr2ogr():
	if not shutil.which("ogr2ogr"):
		sys.exit(
			"ogr2ogr was not found on PATH. Install GDAL (on Windows: OSGeo4W or QGIS; on Ubuntu: "
			"apt-get install gdal-bin) -- it is needed to write the GeoPackage exports."
		)


def check_export_environment(plan_code):
	"""Checks that need no database or Azure sign-in, run before a model run starts so bad
	settings fail early: container name, ogr2ogr, the Azure packages and the account URL."""
	container_name(plan_code)
	check_ogr2ogr()
	if not os.environ.get(ACCOUNT_URL_ENV):
		sys.exit(f"Missing required environment variable {ACCOUNT_URL_ENV} (https://<account>.blob.core.windows.net)")
	try:
		import azure.identity  # noqa: F401
		import azure.storage.blob  # noqa: F401
	except ImportError:
		sys.exit("The Azure packages are not installed: pip install -r fishpass_engine/scripts/requirements.txt")


# =================================================================================
#  AZURE
# =================================================================================
def get_blob_service_client():
	"""The only place that signs in to Azure. DefaultAzureCredential uses the azure/login (OIDC)
	session on GitHub, or the user's `az login` locally."""
	account_url = os.environ.get(ACCOUNT_URL_ENV)
	if not account_url:
		sys.exit(f"Missing required environment variable {ACCOUNT_URL_ENV} (https://<account>.blob.core.windows.net)")
	from azure.identity import DefaultAzureCredential
	from azure.storage.blob import BlobServiceClient

	return BlobServiceClient(
		account_url, credential=DefaultAzureCredential(exclude_interactive_browser_credential=True)
	)


def get_container(service, name):
	"""Return a client for the container, creating it with anonymous blob access if it doesn't
	exist. This is also the first call to Azure, so sign-in and permission problems are reported
	here, before any files are written."""
	from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ResourceNotFoundError

	client = service.get_container_client(name)
	try:
		try:
			client.get_container_properties()
		except ResourceNotFoundError:
			logger.info("Creating Azure container %s (anonymous blob access)", name)
			client = service.create_container(name, public_access="blob")
	except ClientAuthenticationError as e:
		sys.exit(
			f"Could not sign in to Azure ({e.message}). Locally, run `az login`; on GitHub, check the Azure login step."
		)
	except HttpResponseError as e:
		if e.status_code == 403:
			sys.exit(
				f"Azure denied access to container {name} ({e.message}). Check that the account has the "
				f"Storage Blob Data Contributor role on the storage account."
			)
		raise
	return client


def upload_file(container, path, blob, content_type):
	from azure.storage.blob import ContentSettings

	with open(path, "rb") as f:
		container.upload_blob(
			blob, f, overwrite=True, content_settings=ContentSettings(content_type=content_type), max_concurrency=4
		)


# =================================================================================
#  FILE WRITERS
# =================================================================================
def _clean_sql(sql):
	return sql.strip().rstrip(";").strip()


def write_csv(conn, sql, path):
	"""Stream the query result to a CSV file with a header row."""
	with conn.cursor() as cursor, open(path, "wb") as f:
		with cursor.copy(f"COPY ({_clean_sql(sql)}) TO STDOUT WITH (FORMAT csv, HEADER)") as copy:
			for data in copy:
				f.write(data)


def _pg_value(value):
	return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def ogr2ogr_command(path, sql, layer_name):
	"""The ogr2ogr command for one GeoPackage. The password is NOT in it -- it is passed in the
	PGPASSWORD environment variable, so it never shows in a process list or log."""
	pg = "PG:" + " ".join(
		f"{key}={_pg_value(os.environ[env])}"
		for key, env in (
			("host", "FISHPASS_HOST"),
			("port", "FISHPASS_PORT"),
			("dbname", "FISHPASS_DBNAME"),
			("user", "FISHPASS_USER"),
		)
	)
	return [
		"ogr2ogr",
		"-f",
		"GPKG",
		str(path),
		pg,
		"-sql",
		_clean_sql(sql),
		"-nln",
		layer_name,
		"-lco",
		"GEOMETRY_NAME=geometry",
	]


def write_geopackage(sql, path, layer_name):
	env = dict(os.environ, PGPASSWORD=os.environ["FISHPASS_PASSWORD"])
	result = subprocess.run(ogr2ogr_command(path, sql, layer_name), env=env, capture_output=True, text=True)
	if result.returncode != 0:
		raise RuntimeError(f"ogr2ogr failed for {layer_name}: {result.stderr.strip()}")


# =================================================================================
#  EXPORT
# =================================================================================
def read_run_metadata(cursor, output_schema):
	cursor.execute(
		f"""
		SELECT run_datetime, github_user, github_branch, commit_sha, plan_code, total_runtime,
			model_plan_file, species_parameter_file
		FROM {quote_ident(output_schema)}.{METADATA_TABLE};
		"""
	)
	rows = cursor.fetchall()
	if len(rows) != 1:
		sys.exit(f"{output_schema}.{METADATA_TABLE} should hold one row, found {len(rows)}. Re-run the model.")
	keys = (
		"run_datetime",
		"github_user",
		"github_branch",
		"commit_sha",
		"plan_code",
		"total_runtime",
		"model_plan_file",
		"species_parameter_file",
	)
	return dict(zip(keys, rows[0]))


def read_exports(cursor, output_schema):
	cursor.execute(f"SELECT file_name, sql, format FROM {quote_ident(output_schema)}.{EXPORTS_TABLE} ORDER BY id;")
	return cursor.fetchall()


def _schema_hint(output_schema):
	return (
		f"The export looks in output schema {output_schema!r}, which comes from the current plan file. "
		f"If the plan's output_schema has changed since the model ran, run the export from a branch or "
		f"commit that still has the old value, or re-run the model."
	)


def run_export(conn, plan_code, output_schema, export_type, version):
	"""Write every file listed in azure_exports, plus the three metadata files, to Azure.
	Existing files with the same name are overwritten. Returns the uploaded blob URLs."""
	version = validate_export_args(export_type, version)
	check_ogr2ogr()

	with conn.cursor() as cursor:
		missing = [t for t in (EXPORTS_TABLE, METADATA_TABLE) if not table_exists(cursor, output_schema, t)]
		if missing:
			sys.exit(
				f"Cannot export: {output_schema} has no {' or '.join(missing)} table. {_schema_hint(output_schema)}"
			)
		meta = read_run_metadata(cursor, output_schema)
		if meta["plan_code"] != plan_code:
			sys.exit(
				f"Cannot export: {output_schema}.{METADATA_TABLE} is for plan {meta['plan_code']!r}, "
				f"not {plan_code!r}. {_schema_hint(output_schema)}"
			)
		exports = read_exports(cursor, output_schema)
	conn.commit()

	container = get_container(get_blob_service_client(), container_name(plan_code))
	folder = run_folder(plan_code, version, meta["run_datetime"])
	export_datetime = datetime.now(timezone.utc)
	logger.info(
		"Exporting %d file(s) + metadata for plan %s to %s/runs/%s/%s",
		len(exports),
		plan_code,
		container.container_name,
		export_type,
		folder,
	)

	uploaded = []
	with tempfile.TemporaryDirectory() as tmp:
		tmp = Path(tmp)

		def upload(local_path, file_name, content_type):
			blob = blob_name(export_type, folder, file_name)
			upload_file(container, local_path, blob, content_type)
			uploaded.append(f"{container.url}/{blob}")
			logger.info("  uploaded %s", blob)

		for file_name, sql, fmt in exports:
			ext, content_type = FORMATS[fmt]
			path = tmp / f"{file_name}.{ext}"
			logger.info("  writing %s.%s", file_name, ext)
			if fmt == "csv":
				write_csv(conn, sql, path)
				conn.commit()
			else:
				write_geopackage(sql, path, file_name)
			upload(path, f"{file_name}.{ext}", content_type)
			path.unlink()

		for file_name, text in (
			("model_plan.yaml", meta["model_plan_file"]),
			("fish_species_parameters.yaml", meta["species_parameter_file"]),
			("metadata.yaml", metadata_yaml(meta, export_type, version, export_datetime)),
		):
			path = tmp / file_name
			path.write_text(text or "", encoding="utf-8")
			upload(path, file_name, YAML_CONTENT_TYPE)

	write_job_summary(
		f"### Azure export\n:white_check_mark: Exported {len(uploaded)} file(s) to "
		f"`{container.container_name}/runs/{export_type}/{folder}/`.\n\n" + "\n".join(f"- {url}" for url in uploaded)
	)
	logger.info("Azure export complete: %d file(s).", len(uploaded))
	return uploaded


# =================================================================================
#  CLI (export-only)
# =================================================================================
def parse_args():
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("plan_code", help="Plan code -- loads config/models/<plan_code>.yaml")
	parser.add_argument("--export-type", required=True, choices=EXPORT_TYPES)
	parser.add_argument("--version", default=None, help="Version label (required when versioned)")
	parser.add_argument(
		"--check-only",
		action="store_true",
		help="Only check the inputs and tools (no database or Azure); used before a model run starts",
	)
	return parser.parse_args()


def main():
	setup_logging()
	args = parse_args()
	version = validate_export_args(args.export_type, args.version)

	from model_plan import load_model_plan  # lazy: imports yaml

	plan = load_model_plan(args.plan_code)
	check_export_environment(plan["code"])
	if args.check_only:
		logger.info("Export settings for plan %s are valid.", plan["code"])
		return

	require_env()
	conn = db_connect()
	try:
		run_export(conn, plan["code"], plan["output_schema"], args.export_type, version)
	finally:
		conn.close()


if __name__ == "__main__":
	main()
