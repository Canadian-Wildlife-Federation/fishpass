# Habitat Updates Dataset

**Source:** FishPass Database

**Table:** support.habitat_updates

| Column | Type | Description |
| :---- | :---- | :---- |
| id | uuid | Unique system-generated id |
| species_lifestage | string[] | Array of species/lifecycle combinations with habitat in the specified area, each encoded as `[not_]<species>[_<spawn\|rear>]` (same `<species>_<lifecycle>` convention as the model plan's `reporting_values`). Omitting the `_spawn`/`_rear` suffix means both lifecycles. A `not_` prefix explicitly excludes (clears) that species/lifecycle combination rather than setting it — e.g. `as_spawn` sets spawning habitat for species `as`; `as` sets both spawning and rearing habitat for `as`; `not_as_rear` clears rearing habitat for `as`; `not_as` clears both lifecycles for `as`. (Note: the `spawnrear` lifecycle isn't captured by this table — TODO: determine how spawnrear habitat updates should be represented.) |
| update_scope | string | Values: 'all', or any specific plan code (e.g.: cheticamp_wcrp). Determines how this update is included in a model run: the run will include updates where `update_scope` = 'all' or `update_scope` = the plan code. |
| points | Geometry (multipoint) | Represents the upstream and/or downstream point of the habitat area. Modelled as a multipoint that can have one or two points, used in conjunction with the `location_type` field. It is modelled as a multipoint rather than two separate point fields to support editing in QGIS, which does not easily support editing tables with multiple geometry columns. |
| location_type | enum: upstream, downstream, between | Identifies whether the point represents the downstream habitat point, the upstream habitat point, or whether the habitat is between the two points. When the value is upstream or downstream, only one point can be specified in `points`; for between, two points must be specified. We will implement database triggers to enforce this constraint and reduce user errors. |
| chyf_upstream_edge_id | uuid | Optional — if provided, the upstream point is projected onto the CHyF stream edge identified by this id. If the point is more than `habitat_point_snap_edge_distance_m` from that edge, or the CHyF stream edge isn't found, an error will be recorded and model processing stopped. If not provided (and an upstream geometry exists), we will instead project the upstream point onto the nearest CHyF stream edge within `habitat_point_snap_edge_distance_m`. Either way, if an existing vertex is within `habitat_point_snap_vertex_distance_m` of the projected point, the habitat point will be snapped to that existing vertex; otherwise a new vertex will be added to the stream network. |
| chyf_downstream_edge_id | uuid | Optional — if provided, the downstream point is projected onto the CHyF stream edge identified by this id. If the point is more than `habitat_point_snap_edge_distance_m` from that edge, or the CHyF stream edge isn't found, an error will be recorded and model processing stopped. If not provided (and a downstream geometry exists), we will instead project the downstream point onto the nearest CHyF stream edge within `habitat_point_snap_edge_distance_m`. Either way, if an existing vertex is within `habitat_point_snap_vertex_distance_m` of the projected point, the habitat point will be snapped to that existing vertex; otherwise a new vertex will be added to the stream network. |
| update_source | varchar | Source of the update — e.g., a particular organization, workshop, assessment, or data source. |
| update_date | date | Date the update was collected. For field visit data, this is the date of the field visit. For other sources, this is the date the information was received or collected. |
| notes | varchar | Reason for the update to habitat (i.e., local knowledge from a WCRP partner) |

Because each `species_lifestage` entry already carries its own species and lifecycle, a single row's array can mix species with different lifestages (e.g. `["as_spawn", "ae"]`) — a separate row per species is only needed if the habitat points/scope/source differ.

#### CWF Requirements

There are two outcomes for habitat updates:

1. Authoritative updates that we want applied across all model outputs
2. WCRP-specific updates (local overrides) that we only want applied for specific WCRP outputs
