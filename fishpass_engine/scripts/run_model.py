#!/usr/bin/env python3
"""Run a FishPass model plan (fishpass/requirements/requirements.md).

Database connection details come from environment variables only (see README.md) -- never
from the plan file and never logged.

Status: all phases (Initialize, Load Stream Network, Load Structures, Process Habitat, Compute
Statistics) are implemented. See requirements.md's "Outstanding Decisions" section for known
gaps/assumptions (supports_species, AOI-boundary graph_id handling, and others) that haven't
been validated against a real database run yet.
"""

import argparse

from compute_statistics import compute_statistics
from db import db_connect, require_env
from load_habitat import load_habitat
from load_stream_network import get_source_srid, init_output_schema, load_stream_network
from load_structures import load_structures
from model_plan import load_model_plan
from snap_structures import snap_structures


def parse_args():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("plan_code", help="Plan code -- loads config/models/<plan_code>.yaml")
	return parser.parse_args()


def main():
	args = parse_args()
	require_env()
	plan = load_model_plan(args.plan_code)

	print(f"Running model plan {plan['code']!r} -> output schema {plan['output_schema']!r}")

	conn = db_connect()
	try:
		with conn.cursor() as cursor:
			init_output_schema(cursor, plan["output_schema"])
			conn.commit()

			srid = get_source_srid(cursor)
			print("Loading Stream Network")
			load_stream_network(conn, cursor, plan)

			print("Loading Structures")
			load_structures(conn, cursor, plan, srid)

			print("Snapping Structures")
			snap_structures(conn, cursor, plan, srid)

			load_habitat(conn, cursor, plan, srid)

			compute_statistics(conn, cursor, plan, srid)
	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()

	print("Model run complete.")


if __name__ == "__main__":
	main()
