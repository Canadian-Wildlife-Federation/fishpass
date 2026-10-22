#!/usr/bin/env python3
"""Run a FishPass model plan (fishpass/docs/fishpass_docs.md).

Database connection details come from environment variables only (see README.md) -- never
from the plan file and never logged.

Status: all phases (Initialize, Load Stream Network, Load Structures, Process Habitat, Compute
Statistics, Create Barrier Views, Rank Barriers, Create Combined View) are implemented. See the
"Outstanding Decisions" section for known gaps/assumptions (supports_species, AOI-boundary
graph_id handling, and others) that haven't been validated against a real database run yet.

WCRP tracking table: the plan's <code>_wcrp.tracking_table_<code> is created at the start of the
run if it doesn't exist yet, and skipped (left untouched) if it does. Either outcome is written to
the log and, when run in GitHub Actions, to the job summary. This happens before the output schema
is touched, so a missing database-wide support object fails immediately.

Azure export (docs/azure_exports.md): every run fills <output_schema>.azure_exports and
<output_schema>.run_metadata, so the export can be run later on its own (azure_export.py). With
--export-to-azure the export also runs at the end of the model run, and the export settings are
checked before the model starts. If the export fails the run exits with an error, but the model
results (already committed) are kept.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import azure_export
from compute_statistics import compute_statistics
from create_combined_view import check_cabd_fdw_sources, create_combined_view
from create_wcrp_tracking_table import (
	check_tracking_table_columns,
	ensure_tracking_table,
	sync_wcrp_tracking_enums,
)
from db import db_connect, require_env
from load_habitat import load_habitat
from load_stream_network import get_source_srid, init_output_schema, load_stream_network
from load_structures import load_structures
from model_plan import load_model_plan
from postprocess_views import create_barrier_views
from rank_barriers import run_ranking
from snap_structures import snap_structures
from utils import setup_logging, write_job_summary

setup_logging()
logger = logging.getLogger(__name__)


def parse_args():
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("plan_code", help="Plan code -- loads config/models/<plan_code>.yaml")
	parser.add_argument(
		"--export-to-azure", action="store_true", help="Export the results to Azure at the end of the run"
	)
	parser.add_argument("--export-type", choices=azure_export.EXPORT_TYPES, help="Required with --export-to-azure")
	parser.add_argument("--version", default=None, help="Version label (required when --export-type is versioned)")
	return parser.parse_args()


def run_azure_export(conn, plan, export_type, version):
	"""Run the export; on failure log it, note it in the job summary and exit with an error.
	The model results are already committed, so they are kept."""
	logger.info("Exporting to Azure")
	try:
		azure_export.run_export(conn, plan["code"], plan["output_schema"], export_type, version)
	except (Exception, SystemExit) as e:
		logger.error("Azure export failed: %s", e)
		write_job_summary(
			f"### Azure export\n:x: Export failed: {e}\n\nThe model results were kept. Once the cause "
			f"is fixed, re-run the export with the export-only action."
		)
		sys.exit(1)


def main():
	start = time.monotonic()
	run_started = datetime.now(timezone.utc)
	args = parse_args()
	require_env()
	plan = load_model_plan(args.plan_code)

	# Check the export settings before the model starts, so bad input fails now.
	export_version = None
	if args.export_to_azure:
		if not args.export_type:
			sys.exit("--export-type is required with --export-to-azure")
		export_version = azure_export.validate_export_args(args.export_type, args.version)
		azure_export.check_export_environment(plan["code"])

	logger.info("Running model plan %r -> output schema %r", plan["code"], plan["output_schema"])

	conn = db_connect()
	try:
		with conn.cursor() as cursor:
			logger.info("Syncing WCRP enums from config/fishpass.yaml")
			sync_wcrp_tracking_enums(conn, cursor)
			logger.info("Setting up WCRP tracking table")
			tracking_table = f"{plan['code']}_wcrp.tracking_table_{plan['code']}"
			if ensure_tracking_table(conn, cursor, plan):
				write_job_summary(
					f"### WCRP tracking table\n:white_check_mark: Created `{tracking_table}` "
					f"(first run for plan `{plan['code']}`)."
				)
			else:
				write_job_summary(
					f"### WCRP tracking table\n:information_source: Creation skipped -- "
					f"`{tracking_table}` already exists and was left unchanged."
				)
			conn.commit()

			# Pre-flight checks for the WCRP phases at the END of the run (Rank Barriers,
			# Create Combined View), so a missing tracking-table column or CABD foreign
			# table fails now rather than after the whole model has been computed.
			logger.info("Checking WCRP prerequisites")
			check_tracking_table_columns(cursor, plan)
			check_cabd_fdw_sources(cursor)
			conn.commit()

			init_output_schema(cursor, plan["output_schema"])
			azure_export.create_export_tables(cursor, plan["output_schema"])
			conn.commit()

			srid = get_source_srid(cursor)
			logger.info("Loading Stream Network")
			load_stream_network(conn, cursor, plan)

			logger.info("Loading Structures")
			load_structures(conn, cursor, plan, srid)

			logger.info("Snapping Structures")
			snap_structures(conn, cursor, plan, srid)

			logger.info("Loading Habitat")
			load_habitat(conn, cursor, plan, srid)

			logger.info("Computing Statistics")
			compute_statistics(conn, cursor, plan, srid)

			logger.info("Creating Barrier Views")
			create_barrier_views(conn, cursor, plan)

			logger.info("Ranking Barriers")
			run_ranking(conn, cursor, plan)

			logger.info("Creating Combined Output View")
			create_combined_view(conn, cursor, plan)

			# Saved on every run (not only when exporting) so the export can be re-run later.
			logger.info("Saving run metadata")
			total_runtime = timedelta(seconds=int(time.monotonic() - start))
			azure_export.write_run_metadata(cursor, plan["output_schema"], plan["code"], run_started, total_runtime)
			conn.commit()

		minutes, seconds = divmod(int(time.monotonic() - start), 60)
		logger.info("Model run complete (%dmin, %dsec)", minutes, seconds)

		if args.export_to_azure:
			run_azure_export(conn, plan, args.export_type, export_version)

	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()


if __name__ == "__main__":
	main()
