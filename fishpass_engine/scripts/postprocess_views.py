"""Postprocess phase (fishpass/requirements/requirements.md's Outputs section): create the
reporting views over <output_schema>.all_structures/streams once Compute Statistics has
populated species_stats/lifecycle_stats -- natural_barriers/anthropogenic_barriers/
unsnapped_structures (views over all_structures) and streams_<species>/streams_lifecycle
(views over streams, with species_stats/lifecycle_stats exploded to columns).
"""

import sys

from db import quote_ident
from model_plan import IDENTIFIER_RE

STREAM_COLUMNS = "id, geometry, length, strahler_order, effective_length, segment_gradient"

SPECIES_LIFECYCLE_FIELDS = (
	"upstream_length", "functional_upstream_length",
	"weighted_upstream_length", "functional_weighted_upstream_length",
)

LIFECYCLE_ROLLUP_FIELDS = ("upstream_length", "functional_upstream_length", "weighted_upstream_length")


def _species_by_lifecycle_map(reporting_species_lifecycles):
	species_lifecycles = {}
	for sp, lc in reporting_species_lifecycles:
		species_lifecycles.setdefault(sp, set()).add(lc)
	return species_lifecycles


def create_natural_anthropogenic_views(cursor, output_schema):
	"""natural_barriers/anthropogenic_barriers as views over all_structures, filtered to the
	structure_type and to rows write_barrier_stat_tables actually populated (species_stats
	IS NOT NULL) -- i.e. structures that snapped onto a processed edge, matching the row set the
	old natural_barriers/anthropogenic_barriers tables held."""

	schema_ident = quote_ident(output_schema)
	for table, structure_type in (("natural_barriers", "natural"), ("anthropogenic_barriers", "anthropogenic")):
		cursor.execute(f"""
			CREATE VIEW {schema_ident}.{table} AS
			SELECT id, feature_id, feature_type, species_passability_value, geometry, snapped_geometry, species_stats
			FROM {schema_ident}.all_structures
			WHERE structure_type = '{structure_type}' AND species_stats IS NOT NULL
		""")


BARRIER_STAT_FIELDS = (
	"upstream_natural_count", "upstream_anthro_count",
	"downstream_natural_count", "downstream_anthro_count",
)
BARRIER_STAT_ID_FIELDS = ("downstream_natural_ids", "downstream_anthro_ids")


def create_species_barrier_views(cursor, output_schema, reporting_species_lifecycles):
	"""One <output_schema>.natural_barriers_<species> / anthropogenic_barriers_<species> view
	per reporting species -- same row set as natural_barriers/anthropogenic_barriers, with that
	species' species_stats fields exploded to columns (mirrors create_species_views)."""

	schema_ident = quote_ident(output_schema)
	species_codes = {sp for sp, _ in reporting_species_lifecycles}

	for species in species_codes:
		if not IDENTIFIER_RE.match(species):
			sys.exit(f"Invalid species code (must be a safe identifier): {species!r}")

		stats = f"species_stats->'{species}'"
		columns = [f"({stats}->>'{field}')::int AS {field}" for field in BARRIER_STAT_FIELDS]
		columns += [
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'{field}'))::uuid[] AS {field}"
			for field in BARRIER_STAT_ID_FIELDS
		]
		column_sql = ",\n\t\t\t".join(columns)

		for table_prefix, structure_type in (("natural_barriers", "natural"), ("anthropogenic_barriers", "anthropogenic")):
			view_ident = quote_ident(f"{table_prefix}_{species}")
			cursor.execute(f"""
				CREATE VIEW {schema_ident}.{view_ident} AS
				SELECT id, feature_id, feature_type, species_passability_value, geometry, snapped_geometry,
				{column_sql}
				FROM {schema_ident}.all_structures
				WHERE structure_type = '{structure_type}' AND species_stats IS NOT NULL
			""")


def create_unsnapped_structures_view(cursor, output_schema):
	"""unsnapped_structures as a view over all_structures, restricted to rows that never
	snapped onto the stream network (snapped_geometry IS NULL -- see snap_structures.py)."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(f"""
		CREATE VIEW {schema_ident}.unsnapped_structures AS
		SELECT id, feature_id, feature_type, species_passability_value, source, structure_type, geometry
		FROM {schema_ident}.all_structures
		WHERE snapped_geometry IS NULL
	""")


def create_species_views(cursor, output_schema, reporting_species_lifecycles):
	"""One <output_schema>.streams_<species> view per reporting species -- stream attributes
	plus that species' species_stats fields exploded to columns."""

	schema_ident = quote_ident(output_schema)
	species_lifecycles = _species_by_lifecycle_map(reporting_species_lifecycles)

	for species, lifecycles in species_lifecycles.items():
		if not IDENTIFIER_RE.match(species):
			sys.exit(f"Invalid species code (must be a safe identifier): {species!r}")

		stats = f"species_stats->'{species}'"
		columns = [
			f"({stats}->>'accessibility') AS accessibility",
			f"({stats}->>'upstream_anthro_count')::int AS upstream_anthro_count",
			f"({stats}->>'downstream_anthro_count')::int AS downstream_anthro_count",
			f"({stats}->>'upstream_natural_count')::int AS upstream_natural_count",
			f"({stats}->>'downstream_natural_count')::int AS downstream_natural_count",
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'upstream_anthro_ids'))::uuid[] AS upstream_anthro_ids",
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'downstream_anthro_ids'))::uuid[] AS downstream_anthro_ids",
			f"({stats}->>'rear_habitat')::boolean AS rear_habitat",
			f"({stats}->>'spawn_habitat')::boolean AS spawn_habitat",
			f"({stats}->>'general_habitat')::boolean AS general_habitat",
			f"({stats}->>'upstream_accessible_length')::double precision AS upstream_accessible_length",
		]
		for lc in sorted(lifecycles):
			for field in SPECIES_LIFECYCLE_FIELDS:
				column_name = f"{lc}_{field}"
				columns.append(f"({stats}->>'{column_name}')::double precision AS {column_name}")

		view_ident = quote_ident(f"streams_{species}")
		column_sql = ",\n\t\t\t".join(columns)
		cursor.execute(f"""
			CREATE VIEW {schema_ident}.{view_ident} AS
			SELECT {STREAM_COLUMNS},
			{column_sql}
			FROM {schema_ident}.streams
			WHERE {stats} IS NOT NULL
		""")


def create_lifecycle_view(cursor, output_schema, reporting_species_lifecycles):
	"""<output_schema>.streams_lifecycle -- stream attributes plus lifecycle_stats' rollup
	fields (rear/spawn, aggregated across every reporting species) exploded to columns."""

	schema_ident = quote_ident(output_schema)
	lifecycles = sorted({lc for _, lc in reporting_species_lifecycles})

	columns = []
	for lc in lifecycles:
		for field in LIFECYCLE_ROLLUP_FIELDS:
			column_name = f"{lc}_{field}"
			columns.append(f"(lifecycle_stats->>'{column_name}')::double precision AS {column_name}")

	view_ident = quote_ident("streams_lifecycle")
	column_sql = ",\n\t\t\t".join(columns)
	cursor.execute(f"""
		CREATE VIEW {schema_ident}.{view_ident} AS
		SELECT {STREAM_COLUMNS},
		{column_sql}
		FROM {schema_ident}.streams
		WHERE lifecycle_stats IS NOT NULL
	""")


def create_barrier_views(conn, cursor, plan):
	"""Postprocess phase entry point: create the reporting views over all_structures/streams
	(requirements.md's Outputs section) once Compute Statistics has populated
	species_stats/lifecycle_stats."""

	output_schema = plan["output_schema"]

	create_natural_anthropogenic_views(cursor, output_schema)
	create_species_barrier_views(cursor, output_schema, plan["reporting_species_lifecycles"])
	create_unsnapped_structures_view(cursor, output_schema)
	conn.commit()
	print("natural_barriers/anthropogenic_barriers/natural_barriers_<species>/anthropogenic_barriers_<species>/unsnapped_structures: done.")

	create_species_views(cursor, output_schema, plan["reporting_species_lifecycles"])
	create_lifecycle_view(cursor, output_schema, plan["reporting_species_lifecycles"])
	conn.commit()
	print("streams_<species>/streams_lifecycle: done.")
