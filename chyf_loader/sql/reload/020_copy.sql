-- Copy source aoi rows for the target workunit(s) into the target schema.

INSERT INTO :target_aoi_table (id, short_name, province_territory_code, geometry)
SELECT id, short_name, province_territory_code, geometry
FROM :source_aoi_table
WHERE id = ANY(:'workunit_ids'::uuid[]);

INSERT INTO :target_shoreline_table (id, aoi_id, geometry)
SELECT id, aoi_id, geometry
FROM :source_shoreline_table
WHERE aoi_id = ANY(:'workunit_ids'::uuid[]);


INSERT INTO :target_flowpath_table (
	id, nid, ef_type, ef_subtype, "rank", length, rivernameid1, rivernameid2,
	aoi_id, from_nexus_id, to_nexus_id, ecatchment_id, geometry,
	strahler_order, graph_id, mainstem_id, max_uplength, hack_order,
	horton_order, mainstem_seq, shreve_order, length_km
)
SELECT
	ef.id, ef.nid, ef.ef_type, ef.ef_subtype, ef."rank", ef.length,
	ef.rivernameid1, ef.rivernameid2, ef.aoi_id, ef.from_nexus_id,
	ef.to_nexus_id, ef.ecatchment_id, ef.geometry,
	p.strahler_order, p.graph_id, p.mainstem_id, p.max_uplength,
	p.hack_order, p.horton_order, p.mainstem_seq, p.shreve_order,  ST_Length(geography(geometry)) / 1000.0
FROM :source_flowpath_table ef
JOIN :source_flowpath_properties_table p ON p.id = ef.id
WHERE ef."rank" = 1
	AND ef.ef_type != 2
	AND ef.aoi_id = ANY(:'workunit_ids'::uuid[]);
