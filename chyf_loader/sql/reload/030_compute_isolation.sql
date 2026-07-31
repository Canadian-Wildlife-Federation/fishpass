-- Flag isolated stream segments.
WITH aoi_boundaries AS (
    SELECT id, ST_Boundary(geometry) AS geometry
    FROM :target_aoi_table
),
connected_graphs AS (
    SELECT DISTINCT ef.graph_id
    FROM :target_flowpath_table ef
    JOIN :target_shoreline_table c ON ST_Intersects(ef.geometry, c.geometry)
    WHERE ef."rank" = 1 AND ef.ef_type != 2

    UNION

    SELECT DISTINCT ef.graph_id
    FROM :target_flowpath_table ef
    JOIN aoi_boundaries ab ON ab.id = ef.aoi_id AND ST_Intersects(ab.geometry, ef.geometry)
    WHERE ef."rank" = 1 AND ef.ef_type != 2
)
UPDATE :target_flowpath_table f
SET is_isolated = true
WHERE NOT EXISTS (
        SELECT 1 FROM connected_graphs cg WHERE cg.graph_id = f.graph_id
    );