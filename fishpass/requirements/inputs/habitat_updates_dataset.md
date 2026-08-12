# Habitat Updates Dataset

**Source:** FishPass Database

**Table:** support.habitat_updates

| Column | Type | Description |
| :---- | :---- | :---- |
| id | uuid | Unique system-generated id |
| species | string[] | Array of species with habitat in the specified area. |
| lifestage | string[] | Array of valid lifestages. Valid values: spawning, rearing, not_spawning, not_rearing. Maps to the `lifecycle` values used elsewhere (model plan `reporting_values`, `requirements.md` streams output): spawning → spawn, rearing → rear. not_spawning/not_rearing are exclusions used to mark habitat as explicitly not that lifestage. (Note: the `general` lifecycle isn't captured by this table — TODO: determine how general habitat updates should be represented.) |
| update_scope | string | Values: 'all', or any specific plan code (e.g.: cheticamp_wcrp). Determines how this update is included in a model run: the run will include updates where `update_scope` = 'all' or `update_scope` = the plan code. |
| points | Geometry (multipoint) | Represents the upstream and/or downstream point of the habitat area. Modelled as a multipoint that can have one or two points, used in conjunction with the `location_type` field. It is modelled as a multipoint rather than two separate point fields to support editing in QGIS, which does not easily support editing tables with multiple geometry columns. |
| location_type | enum: upstream, downstream, between | Identifies whether the point represents the downstream habitat point, the upstream habitat point, or whether the habitat is between the two points. When the value is upstream or downstream, only one point can be specified in `points`; for between, two points must be specified. We will implement database triggers to enforce this constraint and reduce user errors. |
| chyf_upstream_edge_id | uuid | Optional — if provided, we will snap to the upstream point geometry of the CHyF stream edge identified by this id. If the two are more than `habitat_point_snap_edge_distance_m` apart, or the CHyF stream edge isn't found, an error will be recorded and model processing stopped. If not provided (and an upstream geometry exists), we will snap the upstream point to the nearest CHyF stream edge within `habitat_point_snap_edge_distance_m`. When snapping, if an existing vertex is within `habitat_point_snap_vertex_distance_m` of the closest point along the stream edge, the habitat point will be snapped to that existing vertex; otherwise a new vertex will be added to the stream network. |
| chyf_downstream_edge_id | uuid | Optional — if provided, we will snap to the downstream point geometry of the CHyF stream edge identified by this id. If the two are more than `habitat_point_snap_edge_distance_m` apart, or the CHyF stream edge isn't found, an error will be recorded and model processing stopped. If not provided (and a downstream geometry exists), we will snap to the nearest CHyF stream edge within `habitat_point_snap_edge_distance_m`. When snapping, if an existing vertex is within `habitat_point_snap_vertex_distance_m` of the closest point along the stream edge, the habitat point will be snapped to that existing vertex; otherwise a new vertex will be added to the stream network. |
| update_source | varchar | Source of the update — e.g., a particular organization, workshop, assessment, or data source. |
| update_date | date | Date the update was collected. For field visit data, this is the date of the field visit. For other sources, this is the date the information was received or collected. |
| notes | varchar | Reason for the update to habitat (i.e., local knowledge from a WCRP partner) |

The way this is set up, if multiple species share the same lifestages, they can be one row. If species have different lifestages for the same habitat, one record per species is needed.

#### CWF Requirements

There are two outcomes for habitat updates:

1. Authoritative updates that we want applied across all model outputs
2. WCRP-specific updates (local overrides) that we only want applied for specific WCRP outputs
