"""Postprocess phase (fishpass/docs/fishpass_docs.md's Outputs section): create the
reporting views over <output_schema>.all_barriers/streams once Compute Statistics has
populated species_stats -- natural_barriers/anthropogenic_barriers/unsnapped_barriers/
natural_barriers_<species>/anthropogenic_barriers_<species> (views over all_barriers, the latter
pair with that species' species_stats fields -- including upstream length -- exploded to columns)
and streams_<species> (view over streams, with species_stats exploded to columns).
"""

import logging
import sys

from db import quote_ident
from model_plan import IDENTIFIER_RE

logger = logging.getLogger(__name__)

STREAM_COLUMNS = "id, geometry, length, strahler_order, effective_length, segment_gradient"

SPECIES_LIFECYCLE_FIELDS = ("upstream_length", "functional_upstream_length")
SPECIES_LIFECYCLE_WEIGHTED_FIELDS = (
	"weighted_connected_upstream_length", "weighted_disconnected_upstream_length",
	"functional_weighted_connected_upstream_length", "functional_weighted_disconnected_upstream_length",
)


def _species_by_lifecycle_map(reporting_species_lifecycles):
	species_lifecycles = {}
	for sp, lc in reporting_species_lifecycles:
		species_lifecycles.setdefault(sp, set()).add(lc)
	return species_lifecycles


def create_natural_anthropogenic_views(cursor, output_schema):
	"""natural_barriers/anthropogenic_barriers as views over all_barriers, filtered to the
	structure_type and to rows write_barrier_stat_tables actually populated (species_stats
	IS NOT NULL) -- i.e. structures that snapped onto a processed edge, matching the row set the
	old natural_barriers/anthropogenic_barriers tables held."""

	schema_ident = quote_ident(output_schema)
	for table, structure_type in (("natural_barriers", "natural"), ("anthropogenic_barriers", "anthropogenic")):
		cursor.execute(f"""
			CREATE VIEW {schema_ident}.{table} AS
			SELECT id, feature_id, feature_type, species_passability_value, geometry, snapped_geometry, species_stats
			FROM {schema_ident}.all_barriers
			WHERE structure_type = '{structure_type}' AND species_stats IS NOT NULL
		""")


BARRIER_STAT_FIELDS = (
	"upstream_natural_spawnrear_count", "upstream_natural_spawn_count", "upstream_natural_rear_count",
	"upstream_anthro_spawnrear_count", "upstream_anthro_spawn_count", "upstream_anthro_rear_count",
	"downstream_natural_spawnrear_count", "downstream_natural_spawn_count", "downstream_natural_rear_count",
	"downstream_anthro_spawnrear_count", "downstream_anthro_spawn_count", "downstream_anthro_rear_count",
)
BARRIER_STAT_ID_FIELDS = (
	"downstream_natural_spawn_ids", "downstream_natural_rear_ids",
	"downstream_anthro_spawn_ids", "downstream_anthro_rear_ids",
	"upstream_anthro_spawn_ids", "upstream_anthro_rear_ids",
)


def create_species_barrier_views(cursor, output_schema, reporting_species_lifecycles):
	"""One <output_schema>.natural_barriers_<species> / anthropogenic_barriers_<species> view
	per reporting species -- same row set as natural_barriers/anthropogenic_barriers, with that
	species' species_stats fields exploded to columns (mirrors create_species_views), including
	that species' upstream length figures at the barrier."""

	schema_ident = quote_ident(output_schema)
	species_lifecycles = _species_by_lifecycle_map(reporting_species_lifecycles)

	for species, lifecycles in species_lifecycles.items():
		if not IDENTIFIER_RE.match(species):
			sys.exit(f"Invalid species code (must be a safe identifier): {species!r}")

		stats = f"species_stats->'{species}'"
		columns = [f"({stats}->>'{field}')::int AS {field}" for field in BARRIER_STAT_FIELDS]
		columns += [
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'{field}'))::uuid[] AS {field}"
			for field in BARRIER_STAT_ID_FIELDS
		]
		columns.append(f"({stats}->>'spawn_upstream_accessible_length')::double precision AS spawn_upstream_accessible_length")
		columns.append(f"({stats}->>'rear_upstream_accessible_length')::double precision AS rear_upstream_accessible_length")
		for lc in sorted(lifecycles):
			for field in SPECIES_LIFECYCLE_FIELDS + SPECIES_LIFECYCLE_WEIGHTED_FIELDS:
				column_name = f"{lc}_{field}"
				columns.append(f"({stats}->>'{column_name}')::double precision AS {column_name}")
		column_sql = ",\n\t\t\t".join(columns)

		for table_prefix, structure_type in (("natural_barriers", "natural"), ("anthropogenic_barriers", "anthropogenic")):
			view_ident = quote_ident(f"{table_prefix}_{species}")
			cursor.execute(f"""
				CREATE VIEW {schema_ident}.{view_ident} AS
				SELECT id, feature_id, feature_type,
				(species_passability_value->>'{species}_spawn')::double precision AS passability_status_spawn,
				(species_passability_value->>'{species}_rear')::double precision AS passability_status_rear,
				geometry, snapped_geometry,
				{column_sql}
				FROM {schema_ident}.all_barriers
				WHERE structure_type = '{structure_type}' AND species_stats IS NOT NULL
			""")


def create_unsnapped_barriers_view(cursor, output_schema):
	"""unsnapped_barriers as a view over all_barriers, restricted to rows that never
	snapped onto the stream network (snapped_geometry IS NULL -- see snap_structures.py)."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(f"""
		CREATE VIEW {schema_ident}.unsnapped_barriers AS
		SELECT id, feature_id, feature_type, species_passability_value, source, structure_type, geometry
		FROM {schema_ident}.all_barriers
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
			f"({stats}->>'spawn_accessibility') AS spawn_accessibility",
			f"({stats}->>'rear_accessibility') AS rear_accessibility",
			f"({stats}->>'upstream_anthro_spawnrear_count')::int AS upstream_anthro_spawnrear_count",
			f"({stats}->>'upstream_anthro_spawn_count')::int AS upstream_anthro_spawn_count",
			f"({stats}->>'upstream_anthro_rear_count')::int AS upstream_anthro_rear_count",
			f"({stats}->>'downstream_anthro_spawnrear_count')::int AS downstream_anthro_spawnrear_count",
			f"({stats}->>'downstream_anthro_spawn_count')::int AS downstream_anthro_spawn_count",
			f"({stats}->>'downstream_anthro_rear_count')::int AS downstream_anthro_rear_count",
			f"({stats}->>'upstream_natural_spawnrear_count')::int AS upstream_natural_spawnrear_count",
			f"({stats}->>'upstream_natural_spawn_count')::int AS upstream_natural_spawn_count",
			f"({stats}->>'upstream_natural_rear_count')::int AS upstream_natural_rear_count",
			f"({stats}->>'downstream_natural_spawnrear_count')::int AS downstream_natural_spawnrear_count",
			f"({stats}->>'downstream_natural_spawn_count')::int AS downstream_natural_spawn_count",
			f"({stats}->>'downstream_natural_rear_count')::int AS downstream_natural_rear_count",
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'upstream_anthro_spawn_ids'))::uuid[] AS upstream_anthro_spawn_ids",
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'upstream_anthro_rear_ids'))::uuid[] AS upstream_anthro_rear_ids",
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'downstream_anthro_spawn_ids'))::uuid[] AS downstream_anthro_spawn_ids",
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'downstream_anthro_rear_ids'))::uuid[] AS downstream_anthro_rear_ids",
			f"({stats}->>'rear_habitat')::boolean AS rear_habitat",
			f"({stats}->>'spawn_habitat')::boolean AS spawn_habitat",
			f"({stats}->>'spawnrear_habitat')::boolean AS spawnrear_habitat",
		]
		for lc in ("rear", "spawn"):
			if lc in lifecycles:
				for field in ("weighted_length", "weighted_connected_length", "weighted_disconnected_length"):
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

def create_watershed_summary_statistics(cursor, output_schema, reporting_species_lifecycles):
    """Creates <output_schema>.watershed_summary_stats view in a single pass over streams
    by unnesting species_stats keys laterally.
    """
    schema_ident = quote_ident(output_schema)
    species_lifecycles = _species_by_lifecycle_map(reporting_species_lifecycles)
    valid_species = list(species_lifecycles.keys())

    for species in valid_species:
        if not IDENTIFIER_RE.match(species):
            sys.exit(f"Invalid species code: {species!r}")

    species_list_sql = ", ".join(f"'{s}'" for s in valid_species)

    query = f"""
    CREATE MATERIALIZED VIEW {schema_ident}.watershed_summary_stats AS
    WITH expanded AS (
        SELECT
            s.id,
            s.effective_length,
            sp.key AS species,
            sp.value AS stats
        FROM {schema_ident}.streams s,
        LATERAL jsonb_each(s.species_stats) sp
        WHERE sp.key IN ({species_list_sql})
    ),
    aggregated AS (
        SELECT
            species,
            COALESCE(SUM(effective_length), 0) / 1000.0 AS total_km,
            COALESCE(SUM(effective_length) FILTER (WHERE (stats->>'spawn_habitat')::boolean), 0) / 1000.0 AS total_spawn_km,
            COALESCE(SUM(effective_length) FILTER (WHERE (stats->>'rear_habitat')::boolean), 0) / 1000.0 AS total_rear_km,
            COALESCE(SUM(effective_length) FILTER (WHERE (stats->>'spawnrear_habitat')::boolean), 0) / 1000.0 AS total_spawnrear_km,

            COALESCE(SUM((stats->>'spawn_weighted_connected_length')::double precision), 0) / 1000.0 AS connected_spawn_km,
            COALESCE(SUM((stats->>'spawn_weighted_disconnected_length')::double precision), 0) / 1000.0 AS disconnected_spawn_km,

            COALESCE(SUM((stats->>'rear_weighted_connected_length')::double precision), 0) / 1000.0 AS connected_rear_km,
            COALESCE(SUM((stats->>'rear_weighted_disconnected_length')::double precision), 0) / 1000.0 AS disconnected_rear_km,

            COALESCE(SUM(
                LEAST(
                    COALESCE((stats->>'spawn_weighted_connected_length')::double precision, 0),
                    COALESCE((stats->>'rear_weighted_connected_length')::double precision, 0)
                )
            ), 0) / 1000.0 AS connected_spawnrear_km,

            COALESCE(SUM(
                LEAST(
                    COALESCE((stats->>'spawn_weighted_disconnected_length')::double precision, 0),
                    COALESCE((stats->>'rear_weighted_disconnected_length')::double precision, 0)
                )
            ), 0) / 1000.0 AS disconnected_spawnrear_km
        FROM expanded
        GROUP BY species
    )
    SELECT
        species,
        total_km,
        total_spawn_km,
        total_rear_km,
        total_spawnrear_km,
        connected_spawn_km,
        disconnected_spawn_km,
        connected_rear_km,
        disconnected_rear_km,
        connected_spawnrear_km,
        disconnected_spawnrear_km,
        ROUND((disconnected_spawn_km / NULLIF(total_spawn_km, 0))::numeric, 2) AS pct_disconnected_spawn,
        ROUND((disconnected_rear_km / NULLIF(total_rear_km, 0))::numeric, 2) AS pct_disconnected_rear,
        ROUND((disconnected_spawnrear_km / NULLIF(total_spawnrear_km, 0))::numeric, 2) AS pct_disconnected_spawnrear
    FROM aggregated;
    """

    cursor.execute(query)

def create_barrier_views(conn, cursor, plan):
	"""Postprocess phase entry point: create the reporting views over all_barriers/streams
	(the Outputs section) once Compute Statistics has populated species_stats."""

	output_schema = plan["output_schema"]

	create_natural_anthropogenic_views(cursor, output_schema)
	create_species_barrier_views(cursor, output_schema, plan["reporting_species_lifecycles"])
	create_unsnapped_barriers_view(cursor, output_schema)
	conn.commit()
	logger.info("natural_barriers/anthropogenic_barriers/natural_barriers_<species>/anthropogenic_barriers_<species>/unsnapped_barriers: done.")

	create_species_views(cursor, output_schema, plan["reporting_species_lifecycles"])
	conn.commit()
	logger.info("streams_<species>: done.")

	create_watershed_summary_statistics(cursor, output_schema, plan["reporting_species_lifecycles"])
	conn.commit()
	logger.info("watershed_summary_stats: done.")