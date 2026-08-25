#!/usr/bin/env python3
"""Reload CHyF stream network data into the FishPass chyf_raw schema for the
workunit(s) configured in config/chyf_loader.yaml.

Database connection details come from environment variables only (see
README.md) -- never from the config file and never logged.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RELOAD_SQL_DIR = REPO_ROOT / "sql" / "reload"
DEFAULT_CONFIG = REPO_ROOT.parent / "config" / "chyf_loader.yaml"

REQUIRED_ENV_VARS = [
	"FISHPASS_HOST",
	"FISHPASS_PORT",
	"FISHPASS_DBNAME",
	"FISHPASS_USER",
	"FISHPASS_PASSWORD",
]


def parse_args():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
	return parser.parse_args()


def load_config(config_path):
	if not config_path.is_file():
		sys.exit(f"Config file not found: {config_path}")
	with open(config_path) as f:
		data = yaml.safe_load(f) or {}

	short_names = (data.get("workunits") or {}).get("short_names") or []
	if not short_names:
		sys.exit("No workunits configured in workunits.short_names -- refusing to run.")

	behavior = data.get("behavior") or {}
	dry_run = behavior.get("dry_run", False)
	verbosity = behavior.get("verbosity", "info")

	target = data.get("target") or {}
	source = data.get("source") or {}
	schema_vars = {
		"target_flowpath_table": target["flowpath_table"],
		"target_aoi_table": target["aoi_table"],
		"target_shoreline_table": target["shoreline_table"],
		"source_flowpath_table": source["flowpath_table"],
		"source_flowpath_properties_table": source["flowpath_properties_table"],
		"source_aoi_table": source["aoi_table"],
		"source_shoreline_table": source["shoreline_table"],
	}

	return short_names, dry_run, verbosity, schema_vars


def require_env():
	missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
	if missing:
		sys.exit(f"Missing required environment variable(s): {', '.join(missing)}")


def target_conn_args():
	return [
		"-h", os.environ["FISHPASS_HOST"],
		"-p", os.environ["FISHPASS_PORT"],
		"-d", os.environ["FISHPASS_DBNAME"],
		"-U", os.environ["FISHPASS_USER"],
	]


def psql_env():
	env = os.environ.copy()
	env["PGPASSWORD"] = os.environ["FISHPASS_PASSWORD"]
	return env


def schema_var_args(schema_vars):
	args = []
	for name, value in schema_vars.items():
		args += ["-v", f"{name}={value}"]
	return args


def resolve_workunit_ids(short_names, dry_run, verbosity, schema_vars):
	names_literal = "{" + ",".join(short_names) + "}"
	query = (
		"SELECT coalesce(array_agg(id), '{}') FROM :source_aoi_table "
		"WHERE short_name = ANY(:'short_names'::varchar[]);"
	)

	if dry_run:
		print(f"[dry-run] would resolve short_names {short_names} via {schema_vars['source_aoi_table']}")
		return None

	# psql on this environment does not interpolate :variables when the SQL is
	# passed via -c, only when read from a file via -f, so write it to a temp file.
	with tempfile.NamedTemporaryFile(
		mode="w", suffix=".sql", delete=False
	) as query_file:
		query_file.write(query)
		query_path = query_file.name

	try:
		cmd = [
			"psql",
			*target_conn_args(),
			*schema_var_args(schema_vars),
			"-v", f"short_names={names_literal}",
			"-t", "-A",
			"-f", query_path,
		]
		if verbosity == "debug":
			resolved_query = query.replace(
				":source_aoi_table", schema_vars["source_aoi_table"]
			).replace(":'short_names'", f"'{names_literal}'")
			print("Resolving workunit ids with query:\n", resolved_query)
			print("Command:", " ".join(cmd))

		result = subprocess.run(cmd, env=psql_env(), capture_output=True, text=True)
	finally:
		os.unlink(query_path)

	if result.returncode != 0:
		sys.exit(f"Failed to resolve workunit ids:\n{result.stderr}")

	raw = result.stdout.strip()
	ids = raw.strip("{}").split(",") if raw and raw != "{}" else []
	if len(ids) != len(short_names):
		sys.exit(
			f"Expected {len(short_names)} workunit(s) for short_names={short_names}, "
			f"found {len(ids)} in chyf2_fdw.aoi -- check for typos or missing AOIs."
		)
	return ids


def run_reload_sql(workunit_ids, dry_run, verbosity, schema_vars):
	sql_files = sorted(RELOAD_SQL_DIR.glob("*.sql"))
	if not sql_files:
		sys.exit(f"No SQL files found in {RELOAD_SQL_DIR}")

	workunit_ids_literal = "{" + ",".join(workunit_ids or []) + "}"

	for sql_file in sql_files:
		cmd = [
			"psql",
			*target_conn_args(),
			"-v", "ON_ERROR_STOP=1",
			"-v", f"workunit_ids={workunit_ids_literal}",
			*schema_var_args(schema_vars),
			"-f", str(sql_file),
		]
		if dry_run:
			print(f"[dry-run] would run: {' '.join(cmd)}")
			continue

		if verbosity in ("debug", "info"):
			print(f"Running {sql_file.name} ...")

		result = subprocess.run(cmd, env=psql_env())
		if result.returncode != 0:
			sys.exit(f"{sql_file.name} failed (exit code {result.returncode})")


def format_elapsed(seconds):
	minutes, secs = divmod(int(seconds), 60)
	return f"{minutes}m {secs:02d}s"


def main():
	start_time = time.monotonic()

	args = parse_args()
	short_names, dry_run, verbosity, schema_vars = load_config(args.config)
	require_env()

	print(f"Reloading workunit(s): {', '.join(short_names)}")
	workunit_ids = resolve_workunit_ids(short_names, dry_run, verbosity, schema_vars)
	run_reload_sql(workunit_ids, dry_run, verbosity, schema_vars)

	elapsed = format_elapsed(time.monotonic() - start_time)
	print(
		f"Dry run complete in {elapsed} -- no changes made."
		if dry_run
		else f"Reload complete in {elapsed}."
	)


if __name__ == "__main__":
	main()
