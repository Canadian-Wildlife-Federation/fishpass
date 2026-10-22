"""Postprocess phase (fishpass/docs/fishpass_docs.md's Outputs section): create the
reporting views over <output_schema>.all_barriers/streams once Compute Statistics has
populated species_stats -- natural_barriers/anthropogenic_barriers/unsnapped_barriers/
natural_barriers_<species>/anthropogenic_barriers_<species> (views over all_barriers, the latter
pair with that species' species_stats fields -- including upstream length -- exploded to columns)
and streams_<species> (view over streams, with species_stats exploded to columns).
"""

import logging
import sys

import azure_export
from cabd_client import CUSTOM_FEATURE_TYPE_FIELDS
from db import quote_ident
from utils import IDENTIFIER_RE, LIFESTAGES, LIFESTAGES_ALL, REAR, SPAWN, SPAWNREAR

logger = logging.getLogger(__name__)

STREAM_COLUMNS = "id, geometry, length, strahler_order, effective_length, segment_gradient, stream_name_en"

SPECIES_LIFECYCLE_FIELDS = ("upstream_length", "functional_upstream_length")
SPECIES_LIFECYCLE_WEIGHTED_FIELDS = (
	"weighted_connected_upstream_length",
	"weighted_disconnected_upstream_length",
	"functional_weighted_connected_upstream_length",
	"functional_weighted_disconnected_upstream_length",
)
# Accessible-length fields, written for both spawn and rear for every target species
# (independent of the plan's reporting_values).
SPECIES_LENGTH_FIELDS = tuple(f"{lc}_upstream_accessible_length" for lc in (SPAWN, REAR))


def barrier_length_fields(lifecycles):
	"""Every length column (metres) on an anthropogenic_barriers_<species> /
	natural_barriers_<species> view for a species reporting the given lifecycles, in view
	column order: SPECIES_LENGTH_FIELDS, then <lc>_<field> for each lifecycle (sorted).
	Single source of truth for these names -- rank_barriers.py and create_combined_view.py
	derive their length columns from here too."""
	fields = list(SPECIES_LENGTH_FIELDS)
	for lc in sorted(lifecycles):
		for field in SPECIES_LIFECYCLE_FIELDS + SPECIES_LIFECYCLE_WEIGHTED_FIELDS:
			fields.append(f"{lc}_{field}")
	return fields


def _species_by_lifecycle_map(reporting_species_lifecycles):
	species_lifecycles = {}
	for sp, lc in reporting_species_lifecycles:
		species_lifecycles.setdefault(sp, set()).add(lc)
	return species_lifecycles


def create_natural_anthropogenic_views(cursor, output_schema, plan):
	"""natural_barriers/anthropogenic_barriers as views over all_barriers, filtered to the
	structure_type and to rows write_barrier_stat_tables actually populated (species_stats
	IS NOT NULL) -- i.e. structures that snapped onto a processed edge, matching the row set the
	old natural_barriers/anthropogenic_barriers tables held. The natural_barriers view also
	includes the CABD waterfall CUSTOM_FEATURE_TYPE_FIELDS (NULL when the plan loads no waterfalls)."""

	schema_ident = quote_ident(output_schema)
	waterfall_fields = CUSTOM_FEATURE_TYPE_FIELDS["waterfalls"]
	has_waterfalls = "waterfalls" in plan["structure_types"]
	for table, structure_type in (("natural_barriers", "natural"), ("anthropogenic_barriers", "anthropogenic")):
		waterfall_columns = ""
		waterfall_join = ""
		if structure_type == "natural":
			if has_waterfalls:
				waterfall_columns = "".join(f", wf.{field['name']}" for field in waterfall_fields)
				waterfall_join = f"""
				LEFT JOIN {schema_ident}.cabd_waterfalls wf
					ON wf.cabd_id = ab.feature_id
				"""
			else:
				waterfall_columns = "".join(f", NULL::{field['type']} AS {field['name']}" for field in waterfall_fields)

		cursor.execute(f"""
			CREATE VIEW {schema_ident}.{table} AS
			SELECT ab.feature_id, ab.feature_type, s.stream_name_en, ab.upstream_edge_id, ab.downstream_edge_id,
			ab.species_passability_value, ab.geometry, ab.snapped_geometry, ab.species_stats{waterfall_columns}
			FROM {schema_ident}.all_barriers ab
			LEFT JOIN {schema_ident}.streams s
				ON s.id = ab.upstream_edge_id
			{waterfall_join}
			WHERE ab.structure_type = '{structure_type}' AND ab.species_stats IS NOT NULL
		""")


# The field lists in this module set the column order of the views and export files, so each
# one spells out its lifecycle order instead of using LIFESTAGES / LIFESTAGES_ALL.
BARRIER_STAT_FIELDS = tuple(
	f"{direction}_{struct}_{lc}_count"
	for direction in ("upstream", "downstream")
	for struct in ("natural", "anthro")
	for lc in (SPAWNREAR, SPAWN, REAR)
)
BARRIER_STAT_ID_FIELDS = tuple(
	f"{prefix}_{lc}_ids"
	for prefix in ("downstream_natural", "downstream_anthro", "upstream_anthro")
	for lc in (SPAWN, REAR)
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

		stats = f"ab.species_stats->'{species}'"
		columns = [f"({stats}->>'{field}')::int AS {field}" for field in BARRIER_STAT_FIELDS]
		columns += [
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'{field}'))::uuid[] AS {field}"
			for field in BARRIER_STAT_ID_FIELDS
		]
		for column_name in barrier_length_fields(lifecycles):
			columns.append(f"({stats}->>'{column_name}')::double precision AS {column_name}")
		column_sql = ",\n\t\t\t".join(columns)

		for table_prefix, structure_type in (
			("natural_barriers", "natural"),
			("anthropogenic_barriers", "anthropogenic"),
		):
			view_ident = quote_ident(f"{table_prefix}_{species}")
			cursor.execute(f"""
				CREATE VIEW {schema_ident}.{view_ident} AS
				SELECT ab.feature_id, ab.feature_type, s.stream_name_en, ab.upstream_edge_id, ab.downstream_edge_id,
				(ab.species_passability_value->>'{species}_{SPAWN}')::double precision AS passability_status_{SPAWN},
				(ab.species_passability_value->>'{species}_{REAR}')::double precision AS passability_status_{REAR},
				ab.geometry, ab.snapped_geometry,
				{column_sql}
				FROM {schema_ident}.all_barriers ab
				LEFT JOIN {schema_ident}.streams s
					ON s.id = ab.upstream_edge_id
				WHERE ab.structure_type = '{structure_type}' AND ab.species_stats IS NOT NULL
			""")


def create_unsnapped_barriers_view(cursor, output_schema):
	"""unsnapped_barriers as a view over all_barriers, restricted to rows that never
	snapped onto the stream network (snapped_geometry IS NULL -- see snap_structures.py)."""

	schema_ident = quote_ident(output_schema)
	cursor.execute(f"""
		CREATE VIEW {schema_ident}.unsnapped_barriers AS
		SELECT feature_id, feature_type, species_passability_value, source, structure_type, geometry
		FROM {schema_ident}.all_barriers
		WHERE snapped_geometry IS NULL
	""")


# streams.species_stats fields, in streams_<species> view column order.
STREAM_ACCESSIBILITY_FIELDS = tuple(f"{lc}_accessibility" for lc in (SPAWN, REAR))
STREAM_STAT_COUNT_FIELDS = tuple(
	f"{direction}_{struct}_{lc}_count"
	for direction, struct in (
		("upstream", "anthro"),
		("downstream", "anthro"),
		("upstream", "natural"),
		("downstream", "natural"),
	)
	for lc in (SPAWNREAR, SPAWN, REAR)
)
STREAM_STAT_ID_FIELDS = tuple(
	f"{direction}_anthro_{lc}_ids" for direction in ("upstream", "downstream") for lc in (SPAWN, REAR)
)
STREAM_HABITAT_FIELDS = tuple(f"{lc}_habitat" for lc in LIFESTAGES_ALL)
# Per-edge weighted lengths -- rear/spawn only (there is no stream-order weight for spawnrear).
STREAM_WEIGHTED_FIELDS = ("weighted_length", "weighted_connected_length", "weighted_disconnected_length")


def create_species_views(cursor, output_schema, reporting_species_lifecycles):
	"""One <output_schema>.streams_<species> view per reporting species -- stream attributes
	plus that species' species_stats fields exploded to columns."""

	schema_ident = quote_ident(output_schema)
	species_lifecycles = _species_by_lifecycle_map(reporting_species_lifecycles)

	for species, lifecycles in species_lifecycles.items():
		if not IDENTIFIER_RE.match(species):
			sys.exit(f"Invalid species code (must be a safe identifier): {species!r}")

		stats = f"species_stats->'{species}'"
		columns = [f"({stats}->>'{field}') AS {field}" for field in STREAM_ACCESSIBILITY_FIELDS]
		columns += [f"({stats}->>'{field}')::int AS {field}" for field in STREAM_STAT_COUNT_FIELDS]
		columns += [
			f"ARRAY(SELECT jsonb_array_elements_text({stats}->'{field}'))::uuid[] AS {field}"
			for field in STREAM_STAT_ID_FIELDS
		]
		columns += [f"({stats}->>'{field}')::boolean AS {field}" for field in STREAM_HABITAT_FIELDS]
		for lc in LIFESTAGES:
			if lc in lifecycles:
				for field in STREAM_WEIGHTED_FIELDS:
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

	if not valid_species:
		logger.warning("No reporting species configured; skipping watershed_summary_stats.")
		return False

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
			COALESCE(SUM((stats->>'{SPAWN}_weighted_length')::double precision), 0) / 1000.0 AS total_{SPAWN}_km,
			COALESCE(SUM((stats->>'{REAR}_weighted_length')::double precision), 0) / 1000.0 AS total_{REAR}_km,
			COALESCE(SUM(
				LEAST(
					COALESCE((stats->>'{SPAWN}_weighted_length')::double precision, 0),
					COALESCE((stats->>'{REAR}_weighted_length')::double precision, 0)
				)
			), 0) / 1000.0 AS total_{SPAWNREAR}_km,

			COALESCE(SUM((stats->>'{SPAWN}_weighted_connected_length')::double precision), 0) / 1000.0 AS connected_{SPAWN}_km,
			COALESCE(SUM((stats->>'{SPAWN}_weighted_disconnected_length')::double precision), 0) / 1000.0 AS disconnected_{SPAWN}_km,

			COALESCE(SUM((stats->>'{REAR}_weighted_connected_length')::double precision), 0) / 1000.0 AS connected_{REAR}_km,
			COALESCE(SUM((stats->>'{REAR}_weighted_disconnected_length')::double precision), 0) / 1000.0 AS disconnected_{REAR}_km,

			-- Spawnrear is habitat suitable for both spawn and rear life stages. 
			-- It is taken as the least length between the spawn and rear lengths
			COALESCE(SUM(
				LEAST(
					COALESCE((stats->>'{SPAWN}_weighted_connected_length')::double precision, 0),
					COALESCE((stats->>'{REAR}_weighted_connected_length')::double precision, 0)
				)
			), 0) / 1000.0 AS connected_{SPAWNREAR}_km,

			COALESCE(SUM(
				LEAST(
					COALESCE((stats->>'{SPAWN}_weighted_disconnected_length')::double precision, 0),
					COALESCE((stats->>'{REAR}_weighted_disconnected_length')::double precision, 0)
				)
			), 0) / 1000.0 AS disconnected_{SPAWNREAR}_km
		FROM expanded
		GROUP BY species
	)
	SELECT
		species,
		total_km,
		total_{SPAWN}_km,
		total_{REAR}_km,
		total_{SPAWNREAR}_km,
		connected_{SPAWN}_km,
		disconnected_{SPAWN}_km,
		connected_{REAR}_km,
		disconnected_{REAR}_km,
		connected_{SPAWNREAR}_km,
		disconnected_{SPAWNREAR}_km,
		ROUND((disconnected_{SPAWN}_km / NULLIF(total_{SPAWN}_km, 0))::numeric, 2) AS pct_disconnected_{SPAWN},
		ROUND((disconnected_{REAR}_km / NULLIF(total_{REAR}_km, 0))::numeric, 2) AS pct_disconnected_{REAR},
		ROUND((disconnected_{SPAWNREAR}_km / NULLIF(total_{SPAWNREAR}_km, 0))::numeric, 2) AS pct_disconnected_{SPAWNREAR}
	FROM aggregated;	
    """

	cursor.execute(query)
	return True


# =================================================================================
#  AZURE EXPORTS (fishpass_engine/docs/azure_exports.md -- Data Exports)
# =================================================================================
# Column lists are hard-coded to match azure_exports.md. upstream_edge_id/downstream_edge_id
# are never exported: edge ids are internal to a run and change every model run.

# Lifecycle order of the columns in the export files.
EXPORT_LIFECYCLE_ORDER = (SPAWN, REAR, SPAWNREAR)

EXPORT_BARRIER_COUNT_FIELDS = tuple(
	f"{direction}_{struct}_{lc}_count"
	for direction in ("upstream", "downstream")
	for struct in ("natural", "anthro")
	for lc in EXPORT_LIFECYCLE_ORDER
)
# Only the anthropogenic id lists are exported (not downstream_natural_*_ids).
EXPORT_BARRIER_ID_FIELDS = tuple(
	f"{direction}_anthro_{lc}_ids" for direction in ("downstream", "upstream") for lc in (SPAWN, REAR)
)
EXPORT_STREAM_FIELDS = ("length", "strahler_order", "effective_length", "segment_gradient", "stream_name_en")
EXPORT_STREAM_HABITAT_FIELDS = (
	*(f"{lc}_accessibility" for lc in (SPAWN, REAR)),
	*(f"{lc}_habitat" for lc in EXPORT_LIFECYCLE_ORDER),
)
EXPORT_STREAM_COUNT_FIELDS = tuple(
	f"{direction}_{struct}_{lc}_count"
	for direction, struct in (
		("upstream", "anthro"),
		("downstream", "anthro"),
		("upstream", "natural"),
		("downstream", "natural"),
	)
	for lc in EXPORT_LIFECYCLE_ORDER
)
EXPORT_STREAM_ID_FIELDS = STREAM_STAT_ID_FIELDS
EXPORT_STREAM_WEIGHTED_FIELDS = STREAM_WEIGHTED_FIELDS
EXPORT_SUMMARY_FIELDS = (
	"species",
	"total_km",
	*(f"total_{lc}_km" for lc in EXPORT_LIFECYCLE_ORDER),
	*(f"{kind}_{lc}_km" for lc in EXPORT_LIFECYCLE_ORDER for kind in ("connected", "disconnected")),
	*(f"pct_disconnected_{lc}" for lc in EXPORT_LIFECYCLE_ORDER),
)


def natural_barriers_export_sql(output_schema, species, lifecycles):
	"""SQL for the natural_barriers_<species> GeoPackage export."""
	columns = [
		azure_export.as_text("feature_id"),
		azure_export.column("feature_type"),
		azure_export.column("stream_name_en"),
		azure_export.column(f"passability_status_{SPAWN}"),
		azure_export.column(f"passability_status_{REAR}"),
	]
	columns += [azure_export.column(f) for f in EXPORT_BARRIER_COUNT_FIELDS]
	columns += [azure_export.pipe_list(f) for f in EXPORT_BARRIER_ID_FIELDS]
	columns += [azure_export.column(f) for f in barrier_length_fields(lifecycles)]
	columns.append(azure_export.column("snapped_geometry", "geometry"))
	relation = f"{quote_ident(output_schema)}.{quote_ident(f'natural_barriers_{species}')}"
	return azure_export.select_sql(columns, relation)


def streams_export_sql(output_schema, species, lifecycles):
	"""SQL for the streams_<species> GeoPackage export."""
	columns = [azure_export.as_text("id")]
	columns += [azure_export.column(f) for f in EXPORT_STREAM_FIELDS + EXPORT_STREAM_HABITAT_FIELDS]
	columns += [azure_export.column(f) for f in EXPORT_STREAM_COUNT_FIELDS]
	columns += [azure_export.pipe_list(f) for f in EXPORT_STREAM_ID_FIELDS]
	for lc in (SPAWN, REAR):  # spawn first in the export (the view has rear first)
		if lc in lifecycles:
			columns += [azure_export.column(f"{lc}_{f}") for f in EXPORT_STREAM_WEIGHTED_FIELDS]
	columns.append(azure_export.column("geometry"))
	relation = f"{quote_ident(output_schema)}.{quote_ident(f'streams_{species}')}"
	return azure_export.select_sql(columns, relation)


def connectivity_summary_export_sql(output_schema):
	"""SQL for the connectivity_summary CSV export."""
	columns = [azure_export.column(f) for f in EXPORT_SUMMARY_FIELDS]
	return azure_export.select_sql(columns, f"{quote_ident(output_schema)}.watershed_summary_stats")


def create_barrier_views(conn, cursor, plan):
	"""Postprocess phase entry point: create the reporting views over all_barriers/streams
	(the Outputs section) once Compute Statistics has populated species_stats, and add their
	rows to the azure_exports table."""

	output_schema = plan["output_schema"]
	species_lifecycles = _species_by_lifecycle_map(plan["reporting_species_lifecycles"])

	create_natural_anthropogenic_views(cursor, output_schema, plan)
	create_species_barrier_views(cursor, output_schema, plan["reporting_species_lifecycles"])
	create_unsnapped_barriers_view(cursor, output_schema)
	for species, lifecycles in species_lifecycles.items():
		azure_export.add_export(
			cursor,
			output_schema,
			f"natural_barriers_{species}",
			natural_barriers_export_sql(output_schema, species, lifecycles),
			"geopackage",
		)
	conn.commit()
	logger.info(
		"natural_barriers/anthropogenic_barriers/natural_barriers_<species>/anthropogenic_barriers_<species>/unsnapped_barriers: done."
	)

	create_species_views(cursor, output_schema, plan["reporting_species_lifecycles"])
	for species, lifecycles in species_lifecycles.items():
		azure_export.add_export(
			cursor,
			output_schema,
			f"streams_{species}",
			streams_export_sql(output_schema, species, lifecycles),
			"geopackage",
		)
	conn.commit()
	logger.info("streams_<species>: done.")

	if create_watershed_summary_statistics(cursor, output_schema, plan["reporting_species_lifecycles"]):
		azure_export.add_export(
			cursor, output_schema, "connectivity_summary", connectivity_summary_export_sql(output_schema), "csv"
		)
	conn.commit()
	logger.info("watershed_summary_stats: done.")
