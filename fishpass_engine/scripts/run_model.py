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
"""

import argparse
import logging
import os
import time

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

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def write_job_summary(markdown):
	"""Append markdown to the GitHub Actions job summary (shown on the run's summary page).
	GITHUB_STEP_SUMMARY is only set inside GitHub Actions, so this is a no-op when run locally."""
	summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
	if not summary_path:
		return
	with open(summary_path, "a", encoding="utf-8") as f:
		f.write(markdown.rstrip() + "\n\n")


def parse_args():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("plan_code", help="Plan code -- loads config/models/<plan_code>.yaml")
	return parser.parse_args()


def main():
	start = time.monotonic()
	args = parse_args()
	require_env()
	plan = load_model_plan(args.plan_code)

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

	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()

	minutes, seconds = divmod(int(time.monotonic() - start), 60)
	logger.info("Model run complete (%dmin, %dsec)", minutes, seconds)


if __name__ == "__main__":
	main()
