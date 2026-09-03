#!/usr/bin/env python3
"""Run a FishPass model plan (fishpass/docs/fishpass_docs.md).

Database connection details come from environment variables only (see README.md) -- never
from the plan file and never logged.

Status: all phases (Initialize, Load Stream Network, Load Structures, Process Habitat, Compute
Statistics) are implemented. See the "Outstanding Decisions" section for known
gaps/assumptions (supports_species, AOI-boundary graph_id handling, and others) that haven't
been validated against a real database run yet.
"""

import argparse
import logging
import time

from compute_statistics import compute_statistics
from db import db_connect, require_env
from load_habitat import load_habitat
from load_stream_network import get_source_srid, init_output_schema, load_stream_network
from load_structures import load_structures
from model_plan import load_model_plan
from postprocess_views import create_barrier_views
from snap_structures import snap_structures

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


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
	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()

	minutes, seconds = divmod(int(time.monotonic() - start), 60)
	logger.info("Model run complete (%dmin, %dsec)", minutes, seconds)


if __name__ == "__main__":
	main()
