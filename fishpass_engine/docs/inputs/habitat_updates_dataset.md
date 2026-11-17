# Habitat Updates Dataset

**Source:** FishPass Database

**Table:** support.habitat_updates

| Column | Type | Description |
| :---- | :---- | :---- |
| id | uuid | Unique system-generated id |
| species_lifestage | string[] | Array of species/lifecycle combinations with habitat in the specified area, each encoded as `[not_]<species>[_<spawn\|rear>]` (same `<species>_<lifecycle>` convention as the model plan's `reporting_values`). Omitting the `_spawn`/`_rear` suffix means both lifecycles. A `not_` prefix explicitly excludes (clears) that species/lifecycle combination rather than setting it — e.g. `as_spawn` sets spawning habitat for species `as`; `as` sets both spawning and rearing habitat for `as`; `not_as_rear` clears rearing habitat for `as`; `not_as` clears both lifecycles for `as`. (Note: the `spawnrear` lifecycle is not supported, skip lifecycle entirely to represent both) |
| update_scope | string | Values: 'all', or any specific plan code (e.g.: cheticamp_wcrp). Determines how this update is included in a model run: the run will include updates where `update_scope` = 'all' or `update_scope` = the plan code. |
| points | Geometry (multipoint) | Represents the upstream and/or downstream point of the habitat area. Modelled as a multipoint that can have one or two points, used in conjunction with the `location_type` field. It is modelled as a multipoint rather than two separate point fields to support editing in QGIS, which does not easily support editing tables with multiple geometry columns. |
| location_type | enum: upstream, downstream, between | Identifies whether the point represents the downstream habitat point, the upstream habitat point, or whether the habitat is between the two points. When the value is upstream or downstream, only one point can be specified in `points`; for between, two points must be specified. We will implement database triggers to enforce this constraint and reduce user errors. |
| chyf_upstream_edge_id | uuid | Optional — if provided, the upstream point is projected onto the CHyF stream edge identified by this id. If the point is more than `habitat_point_snap_edge_distance_m` from that edge, or the CHyF stream edge isn't found, an error will be recorded and model processing stopped. If not provided (and an upstream geometry exists), we will instead project the upstream point onto the nearest CHyF stream edge within `habitat_point_snap_edge_distance_m`. Either way, if an existing vertex is within `habitat_point_snap_vertex_distance_m` of the projected point, the habitat point will be snapped to that existing vertex; otherwise a new vertex will be added to the stream network. |
| chyf_downstream_edge_id | uuid | Optional — if provided, the downstream point is projected onto the CHyF stream edge identified by this id. If the point is more than `habitat_point_snap_edge_distance_m` from that edge, or the CHyF stream edge isn't found, an error will be recorded and model processing stopped. If not provided (and a downstream geometry exists), we will instead project the downstream point onto the nearest CHyF stream edge within `habitat_point_snap_edge_distance_m`. Either way, if an existing vertex is within `habitat_point_snap_vertex_distance_m` of the projected point, the habitat point will be snapped to that existing vertex; otherwise a new vertex will be added to the stream network. |
| update_source | varchar | Source of the update — e.g., a particular organization, workshop, assessment, or data source. |
| update_date | date | Date the update was collected. For field visit data, this is the date of the field visit. For other sources, this is the date the information was received or collected. |
| notes | varchar | Reason for the update to habitat (i.e., local knowledge from a WCRP partner) |

Because each `species_lifestage` entry already carries its own species and lifecycle, a single row's array can mix species with different lifestages (e.g. `["as_spawn", "ae"]`) — a separate row per species is only needed if the habitat points/scope/source differ.

#### Points at confluences and outlets

Habitat is applied along a single river (`mainstem_id`), so a point that marks the downstream end of the habitat (the point of an `upstream` row, or the downstream point of a `between` row) has to be tied to one of the rivers that meet at a confluence. When such a point is placed at or near a confluence without an edge id, the model uses the edge the point is closest to. If the point is exactly on the confluence, so that two or more edges are equally close (within 0.000001 m), the model assumes the habitat is on the main river (the river that continues below the confluence) and logs a warning.

To put the habitat on a tributary, either place the point clearly on the tributary or, more reliably, set the edge id (`chyf_upstream_edge_id` for an `upstream` row, `chyf_downstream_edge_id` for a `between` row's downstream point) to the tributary's edge.

For a `between` row the model first looks at the row's upstream point: if that point is on one of the rivers meeting at the confluence, the downstream point is put on the same river and no warning is logged.

The same applies where two or more edges end at the same network outlet. There the model has no river below to go by, so a point exactly on the outlet stops the run with an error until an edge id is set (unless a `between` row's upstream point settles it).

#### Short `between` reaches

Each point snaps to an existing stream vertex if one is within `habitat_point_snap_vertex_distance_m` of it. If both points of a `between` row snap to the same vertex, there is no stream between them: the model logs a warning and the row applies to no edges. Move the points further apart, or lower `habitat_point_snap_vertex_distance_m` in the model plan.

The full set of rules is in the "Top points and bottom points" table in [fishpass_docs.md](../fishpass_docs.md) (Process Habitat).

#### CWF Requirements

There are two outcomes for habitat updates:

1. Authoritative updates that we want applied across all model outputs
2. WCRP-specific updates (local overrides) that we only want applied for specific WCRP outputs
