# FishPass Modelling Engine

This engine will be implemented in python.

## Running

This process will be initiated via a GitHub action. A plan file will control parameters for the run. This plan file will be selected by the user when launching the action. Each run will clear all existing data out of the output schema and generate new output data.

### GitHub Limitation

Every job has a 6-hour max execution time on GitHub-hosted runners, regardless of tier. Standard `ubuntu-latest` runners provide 4 vCPU / 16 GB RAM, are free/unlimited on public repos, and billed per-minute on private repos. GitHub-hosted runners are available with Ubuntu Linux, Windows, or macOS; machine maintenance and upgrades are handled by GitHub.

If these limitations prevent us from using a GitHub job, we can containerize the process and run it in the Azure environment.


## FishPass Database

The database connection uses the `FISHPASS_HOST/PORT/DBNAME/USER/PASSWORD` environment variable / GitHub secrets.

## Input Datasets

| Dataset | Details |
| :---- | :---- |
| Model Plan File | [inputs/model_plan_file.md](./inputs/model_plan_file.md) |
| Fish Species Parameters | [fish_species_parameter_file.md](../../docs/fish_species_parameter_file.md) |
| Stream Network | [inputs/stream_network.md](./inputs/chyf_stream_network.md) |
| CABD Barriers | [inputs/cabd_barriers.md](./inputs/cabd_barriers.md) |
| Gradient Barriers | [inputs/gradient_barriers.md](./inputs/gradient_barriers.md)|
| Habitat Updates | [inputs/habitat_updates_dataset.md](./inputs/habitat_updates_dataset.md) |
| Structure Updates | [inputs/structure_updates_dataset.md](./inputs/structure_updates_dataset.md) |
| New Structures | [inputs/structure_new_dataset.md](./structure_new_dataset/cabd_barriers.md) |


## Output Datasets

Each model run will generate its own schema for the output. The schema name is defined in the model parameters (`output_schema`). **If the output schema already exists, the existing schema will be dropped and a new one created.**

| Dataset | Details |
| :---- | :---- |
| Streams | [outputs/streams.md](./outputs/streams.md) |
| Barriers | [outputs/barriers.md](./outputs/barriers.md) |
| CABD Features | [outputs/cabd_features.md](./outputs/cabd_features.md) |
| Gradient Barriers | [outputs/gradient_barriers.md](./outputs/gradient_barriers.md) |
| Habitat Updates | `<output_schema>.habitat_updates`  A copy of the habitat updates table that only includes the rows used for this model. |


## Computations

### Accessibility

* IF (the species if valid for that edge) AND (downstream natural impassable barrier for species/lifecycle count = 0) THEN 
    * NATURALLY ACCESSIBLE 
* ELSE
    * NATURALLY INACCESSIBLE 

### Habitat

* TRUE when:
  * species accessibility = ‘NATURALLY ACCESSIBLE ’
  * AND 
  * (segment_gradient >= min_\<lc>\_gradient and segment_gradient < max_\<lc>\_gradient)
  * AND
  * (strahler_order >=  min_\<lc>\_strahler_order and strahler_order <  max_\<lc>\_strahler_order)
* FALSE otherwise


### Stream Length Attributes

Each stream edge will have the following length fields added:
* Length - raw length in meters of the edge segments
* Effective Length - raw length except of skeleton edges that are not associated with the longest mainstem in the waterbody (connector skeleton edges don’t contribute to total length of calculations)
* <species> <lifestage> Weighted Length 
  * If the edge is not habit for <species> <lifestage> then 0
  * If the edge is not naturally accessible for <species> <lifestage> then 0
  * Otherwise the effective length * (<species> <lifestage> strahler order weighting) * (<species> <lifestage> passability status of the first not-passable anthropogenic downstream barrier) 
  * Note: Only need spawning and rearing as lifestage, NOT combined

### 'Functional' Upstream Length
 * A barrier ‘resets’ the upstream length calculation when the barrier is a non-passable anthropogenic barrier.
 


## Processing

### Initialize

Drop the existing output schema (if it exists) and create a new one using the output_schema name from the model parameters file.

### Load Stream Network

Create a working copy of the stream network, copying relevant records the flowpath and aoi tables into the output schema. This working copy is `<output_schema>.streams` — the same table named in
Outputs above, created here directly rather than as a separate `<output_schema>.flowpath` table
that gets copied again later. It accumulates the statistics columns from Compute Statistics in
place, and is split into more rows in place during that phase's network-breaking step.

Inputs: 

`chyf_raw.flowpath`, `chyf_raw.aoi`

Outputs: 

The output schema is populated from the output_schema parameter in the model parameter file. 

`<output_schema>.aoi`
| Field | Type | 
| :---- | :---- | 
| id | uuid | 
| short_name | varchar |
| province_territory_code | varchar[] |


`<output_schema>.streams`

| Field | Type | 
| :---- | :---- | 
| id | uuid | 
| aoi_id | uuid |
| ef_type | smallint |
| ef_subtype | smallint |
| rank | int |
| length | double |
| from_nexus_id | uuid |
| to_nexus_id | uuid |
| ecatchment_id | uuid |
| mainstem_id | uuid |
| graph_id | int4 |
| is_isolated | boolean |
| strahler_order | int4 |
| effective_length | double precision |
| segment_gradient | double precision |
| downstream_route_measure | double precision |
| upstream_route_measure | double precision |
| species_stats | jsonb |
| geometry | LineStringZM (4617) | 

`graph_id` and `is_isolated` are copied as-is from `chyf_raw.flowpath` (populated by chyf_loader's
`030_compute_isolation.sql`). `graph_id` identifies the connected group of edges this edge
belongs to; Compute Statistics partitions the network into independently-processable connected
components by grouping on it, rather than deriving connectivity separately. Note `graph_id` is
global across the whole chyf network (not scoped to a single AOI/workunit), consistent with how
chyf_loader documents it.

Edges where `is_isolated = true` are excluded from Compute Statistics entirely -- no statistics
are computed for them.

`strahler_order` is copied as-is from `chyf_raw.flowpath` (via `chyf2.eflowpath_properties`,
same source as `graph_id`) -- it is required by
Compute Statistics step 7 (habitat assignment's `strahler_order >= min_<lc>_strahler_order`
test) and was missing from this field list.

`effective_length` and `segment_gradient` are populated by Compute Statistics steps 3-4.

`downstream_route_measure` and `upstream_route_measure` are populated by Compute Statistics steps
5-9, alongside `species_stats`. They are a per-`mainstem_id` linear-referencing measure, in `length`
units (not `effective_length`): 0 at the mouth of the edge's `mainstem_id` chain (the edge with no
successor, i.e. a network outlet, or whose successor belongs to a *different* `mainstem_id`, i.e.
it joins a larger mainstem), increasing upstream to the chain's headwater.
`downstream_route_measure` is the distance from the chain's mouth to this edge's downstream end;
`upstream_route_measure` is that plus this edge's own `length` (its distance to the edge's upstream
end). Each mainstem's measure resets to 0 at its own mouth -- it is not a single measure running
continuously from the network outlet. Edges with `mainstem_id IS NULL` are left NULL for both
columns (there is no chain to measure them along).

`species_stats` holds steps 5-9's per-species output values (see the Outputs section) as a JSON
object keyed by species code, rather than one SQL column per species -- species codes are
plan-defined (from `target_species`), not a fixed schema, so a dynamic column set isn't practical.
Same pattern as `all_barriers.species_passability_value`/`species_stats`.

Copy Filters:

Model Parameter File: aoi_filter

* aoi = 'workunit'
  * all - copy all data (not filter required)
  * individual units - create an aoi_id filter on the shortname, by joining to the chyf_raw.aoi table
* aoi = 'province'
  * create a filter using the province_territory_code attribute from the chyf_raw.aoi table
* aoi = 'upstream_of'
  * Fail saying not yet supported. We will implement this later.

### Load Structures (CABD, New, Updates)

This step creates and populates the `<output_schema>.cabd_<feature_type>` cache tables (see
Outputs section), one per `structure_types` entry (excluding `gradients`), processed one feature
type at a time: create the table, fetch that feature type from CABD, and insert directly into the
table -- without holding the fetched features or converted rows for every feature type in memory
at once -- then commit before moving to the next feature type. Once every feature type's cache
table has been populated this way, `all_barriers` is populated in a single pass by reading back
from the `cabd_<feature_type>` tables.

#### Create `all_barriers` table

This table holds all structures used in processing and associated passability status information and statistics.

Table: `<output_schema>.all_barriers`

| Field | Type | Comment |
| :---- | :---- | :---- |
| id | uuid | system generated primary key |
| feature_id | uuid | cabd_id for feature from cabd, new_structure_id for other features |
| feature_type | string | |
| species_passability_value | jsonb | a json string whose key is the species_lifestage and value is the passability status |
| source | enum (cabd, new_structure, gradient_barriers) | where the structure/barrier originated |
| structure_type | enum (natural, anthropogenic) | the classification of the structure based on feature_type |
| geometry | point (4617) | original location of barrier |
| snapped_geometry | point (4617) | point snapped to the chyf stream network |
| upstream_edge_id | uuid | id of the `streams` edge immediately upstream of this structure's snapped location. NULL until Compute Statistics' network-breaking step runs; also NULL if the structure snapped onto an edge's own first vertex, since a confluence can have more than one incoming edge |
| downstream_edge_id | uuid | id of the `streams` edge this structure snapped to (immediately downstream of the structure's snapped location) |
| network_vertex_x, network_vertex_y | double precision | the same snapped location as `snapped_geometry`, but in the `streams` table's native SRID rather than 4617, so Compute Statistics can match it against `streams` vertices exactly rather than through a lossy reprojection round-trip. Not in the original spec, added for the same reason as `downstream_edge_id`. |
| species_stats | jsonb | per-species upstream/downstream barrier passability stats and upstream length figures (see the natural_barriers/anthropogenic_barriers Outputs entries), written by Compute Statistics step 9 (NULL until then).  Stays NULL for structures that never snapped onto a processed edge  |

#### Populate the CABD structure tables from CABD API

For each feature type (specified by structure_types parameter), download the appropriate data from the CABD api and store in the `<output_schema>.cabd_<feature_type>` output table. There will be one record per CABD feature. This data is never maniuplated and will represent the CABD data at the time it was downloaded. Only data in the model plan AOI will be downloaded.

Any features with a passability type code of 5 (no structure) or with a use_analysis value of false will be excluded and not added or used in the analysis.

Output Table: `<output_schema>.cabd_<feature_type>`

CABD API Filters:
 * aoi filter: `nhn_watershed_id:in:shortname1,shortname2`
 * passability_type_code: `passability_type_code:neq:5`

Field Mapping:

| CABD Field | Structures Table Field |
| :---- | :---- |
| cabd_id | cabd_id |
| lat/lon | geometry |
| passability_status_code | passability_status_code |
| passability_status_code | species_passability_value* |

*the species_passability_value is populated with one key for each species in the target_species (from the model parameter file), for each life stage (rear/spawn), the value computed by mapping the passability_status_code as follows:

  * 4 (Unknown) = 0
  * 1 (Barrier) = 0
  * 2 (Partial Barrier) = 0
  * 3 (Passable) = 1
  * 5 (NA - No Structure) = 1
  * 6 (NA - Decommissioned / Removed) = 1

#### Initialize `all_barriers` table

The `all_barriers` table is initially populated with all CABD features. The source is identified as either 'cabd' or 'gradient_barriers'

#### Load new structures

Add to the `all_barriers` table, add the structures from the new_structures input database filtering on the fields provided in the model configuration. Each new structure adds one record to the all_barriers table.

* new_structures source table:  either the structure_new_table parameter (from the model plan) OR the default `support.new_structures`
 
* structure types: only include structure_types listed in the structure_types field from the model plan

* update_scope: only include features where the update_scope = 'all' OR update_scope contains the value of the update_scope parameter from the model plan

Field Mapping:

| Structures Table | New Structure Table |
| :---- | :---- |
| feature_id | new_structure_id |
| feature_type | feature_type |
| geometry | point |
| species_passability_value (rear) | passability_status_rear key/value pairs. If a species of interest is missing or the value is null, assume a full barrier (impassable) for that species |
| species_passability_value (spawn) | passability_status_spawn key/value pairs, same rule as rear |
| source | fixed - 'new_structure' |


#### Apply structure updates

Apply the structure updates to the structure tables. This reads the records from the updates table and modifies the passability status for various species.

* structure updates source table: either the structure_update_table parameter (from the model plan) OR the default `support.structure_updates`
* update_scope: only include features where the update_scope = 'all' OR update_scope contains the value of the update_scope parameter from the model plan
* ordering:  apply the updates in the order:

  1. update_type = authoritative by update_date asc
  2. update_type = local_override by update_date asc

Field Mapping:

| Structures Table | Structure Updates Table |
| :---- | :---- |
| feature_id | barrier_id |
| feature_type | feature_type |
| species_passability_value (rear) | passability_status_rear key/value pairs |
| species_passability_value (spawn) | passability_status_spawn key/value pairs |

For each species of interest, if the species has no entry in passability_status_rear (or
passability_status_spawn), leave its existing species_passability_value entry unchanged rather
than forcing it to a full barrier -- that existing value is the structure's default from CABD or
`support.new_structures`.

#### Snap to the CHyF Network

Snap the structures in the `all_barriers` table to the stream network.

* source geometry: `<output_schema>.all_barriers.geometry`
* target (snapped) geometry: `<output_schema>.all_barriers.snapped_geometry`
* target (snapped) edge_id: `<output_schema>.all_barriers.downstream_edge_id` (Compute Statistics' network-breaking step later derives `upstream_edge_id` for the other side of the split)
* stream network geometry: `<output_schema>.streams.geometry`

Snapping has the following steps:

1. find the nearest flowpath within the specified snapping distance
2. if there is a vertex within the specified snapping distance on this edge, snap to that vertex
3. if there is no vertex within the specified snapping distance then add a vertex to the stream segment at that point, ensuring the smoothed elevation is computed via interpolation

Snapping Parameters:

* model parameter field: structure_snap_edge_distance_m
  * the maximum distance to search for a flowpath to snap to
  * always snap to the nearest flowpath within this distance
  * if not provided use a default value of 100m
* model parameter field: structure_snap_vertex_distance_m
  * the maximum distance to search for a vertex to snap to
  * always snap to the nearest vertex within this distance
  * if not provided use a default value of 50m

#### Add Gradient Barriers

This step is only run if the plan's `structure_types` list contains `gradients`; otherwise
it is skipped and gradient barriers are excluded from the statistics.

This step adds all the gradient barriers to the `all_barriers` table; each gradient barrier
becomes a single row. The `<output_schema>.gradient_barriers` cache table (see Outputs section)
is not created here -- it is populated later, during Compute Statistics, from the rows this step
adds to `all_barriers`.
* gradient barriers table: the gradient_barriers_table parameter (from the model plan), defaults to `support.gradient_barriers`

Field Mapping:

| Structures Table | Gradient Barriers Table |
| :---- | :---- |
| feature_id | id |
| feature_type | Fixed - 'gradients' |
| geometry | geometry |
| species_passability_value | actual_species - one key for each entry (already encoded as `<species>_<lifestage>`), value 0 (not passable) |
| source | fixed - 'gradient_barriers' |

#### Classify Structures

Populate the `output_schema.all_barriers.structure_type` field: a `feature_type` in the
effective natural-feature-types list is classified as `natural`; every other `feature_type` is
classified as `anthropogenic`. **Anthropogenic is the fallback classification** -- any
`feature_type` not explicitly listed as natural (including a typo, or a new CABD feature type
added in the future) is treated as anthropogenic, never natural.

The effective natural-feature-types list comes from one of two places, in this order of
precedence:

1. The model plan's `natural_feature_types_override` field, if present (see
   [model_plan_file.md](../inputs/model_plan_file.md)) -- entirely replaces (does not merge with)
   the config file's list, for that plan only.
2. Otherwise, `config/fishpass.yaml`'s `structure_classification.natural_feature_types` list.
   The shipped default value is:

   | feature_type | structure_type |
   | :---- | :---- |
   | waterfalls | natural |
   | gradients | natural |
   | _all others_ | anthropogenic |

### Process Habitat

### 1. Load Habitat Updates

Create a working copy of the habitat points, copying relevant records and updating as required.

Inputs:

* The habitat_update_table specified in the model parameter file. If not specified the default is `support.habitat_updates`
* habitat_point_snap_edge_distance_m
* habitat_point_snap_vertex_distance_m

Outputs:

The output schema is populated from the output_schema parameter in the model parameter file.

`<output_schema>.habitat_updates`

| Column | Type | Description |
| :---- | :---- | :---- |
| id | uuid | Unique system generated id |
| species\_lifestage | string\[\] | Array of species/lifecycle combinations with habitat in the specified area, each encoded as `<species>`, `not_<species>`, `<species>_<lifestage>`, `not_<species>_<lifestage>`. Omitting the lifestage suffix means both lifecycles; a `not_` prefix explicitly excludes (clears) that species/lifecycle combination instead of setting it. |
| update\_scope | string | Values: 'all', any specific plan code (e.g.: cheticamp\_wcrp). This field determines how new structures are added to the model run. The model run will include updates where update\_scope \= 'all' or update\_scope \= the plan code. |
| points | Geometry (multipoint) | Represents the upstream and/or downstream point of the habitat area. This is modelled as a multi point that can have one or two points and used in conjunction with the location\_type field. It is modelled as a multi point rather than two point fields to support editing in QGIS. QGIS does not easily support editing tables with multiple geometries. |
| location\_type | enum: upstream, downstream, between | To identify if the point represents the downstream habitat point, the upstream habitat point or if the habitat is between the points. In the cases where the value is upstream or downstream, only one point can be specified in the points; for between two points must be specified. We will implement database triggers to enforce this constraint and reduce user errors. |
| chyf\_upstream\_edge\_id | uuid | Optional \- if provided the upstream point is projected onto the chyf stream edge identified by this id. If the point is more than \<habitat\_point\_snap\_edge\_distance\_m\> from that edge, or the chyf stream edge isn't found, an error will be recorded and the model processing stopped. If not provided (and an upstream geometry exists) we will instead project the upstream point onto the nearest chyf stream edge within \<habitat\_point\_snap\_edge\_distance\_m\>. Either way, if an existing vertex is within \<habitat\_point\_snap\_vertex\_distance\_m\> of the projected point, the habitat point will be snapped to the existing vertex. Otherwise a new vertex will be added to the stream network. |
| chyf\_downstream\_edge\_id | uuid | Optional \- if provided the downstream point is projected onto the chyf stream edge identified by this id. If the point is more than \<habitat\_point\_snap\_edge\_distance\_m\> from that edge, or the chyf stream edge isn't found, an error will be recorded and the model processing stopped. If not provided (and a downstream geometry exists), we will instead project the downstream point onto the nearest chyf stream edge within \<habitat\_point\_snap\_edge\_distance\_m\>. Either way, if an existing vertex is within \<habitat\_point\_snap\_vertex\_distance\_m\> of the projected point, the habitat point will be snapped to the existing vertex. Otherwise a new vertex will be added to the stream network. |
| update\_source | varchar | Source of the update \- e.g., a particular organization, workshop, assessment, or data source. |
| update\_date | date | Date the update was collected. For field visit data, this will be the date of field visit. For other sources, this will be the date the information was received or collected. |
| notes | varchar | Reason for update to habitat (i.e., local knowledge from WCRP partner) |

Filter:

The process will only import the features where the points are within a distance to the `output_schema.streams` geometries. The distance is the `habitat_point_snap_edge_distance_m` parameter in the model parameter file.

### 2. Snap Habitat Points

This step identifies which flowpath edges should be considered habitat for a given species/lifestage.

For each record in this table we need:

1. snap any provided points to the stream network within the `habitat_point_snap_edge_distance_m`.
   * If the location_type = upstream then a single point must be provided
     * If the chyf_upstream_edge_id is provided then this point must be snapped to the specific edge within the `habitat_point_snap_edge_distance_m` tolerance; if it can't be snapped the process must throw an error and stop
     * If the chyf_upstream_edge_id is not provided, it must snap to the closest edge within the `habitat_point_snap_edge_distance_m` tolerance otherwise this habitat point is ignored
   * If the location_type = downstream then a single point must be provided
     * If the chyf_downstream_edge_id is provided then this point must be snapped to the specific edge within the `habitat_point_snap_edge_distance_m` tolerance; if it can't be snapped the process must throw an error and stop
     * If the chyf_downstream_edge_id is not provided, it must snap to the closest edge within the `habitat_point_snap_edge_distance_m` tolerance otherwise this habitat point is ignored
   * If the location_type = between then two points must be provided
     * If chyf_upstream_edge_id is provided then the first point must be snapped to the specific edge within the `habitat_point_snap_edge_distance_m` tolerance; if it can't be snapped the process must throw an error and stop
     * If the chyf_upstream_edge_id is not provided, the first point must snap to the closest edge within the `habitat_point_snap_edge_distance_m` tolerance otherwise this habitat point is ignored
     * If chyf_downstream_edge_id is provided then the second point must be snapped to the specific edge within the `habitat_point_snap_edge_distance_m` tolerance; if it can't be snapped the process must throw an error and stop
     * If the chyf_downstream_edge_id is not provided, the second point must snap to the closest edge within the `habitat_point_snap_edge_distance_m` tolerance otherwise this habitat point is ignored
2. snap to a vertex on line
   * snap to the closest vertex along the line within the `habitat_point_snap_vertex_distance_m` vertex distance
   * if no vertex is found, insert a vertex into the line, ensuring the z values are interpolated



### Compute Statistics

This phase also creates and populates the `<output_schema>.gradient_barriers` cache table (see
Outputs section), if the plan includes gradient barriers -- copying the relevant rows back out of
`all_barriers` after they've been snapped to the network.

Processing is partitioned by `graph_id` (see Load Stream Network): each connected group of edges
is processed independently, which bounds memory/compute to one river system at a time instead of
the whole network, and makes runs resumable/parallelizable per component. Any `graph_id` group
with `is_isolated = true` edges is skipped entirely -- no statistics are computed for it. Steps 5-9 below (the upstream/downstream aggregate and barrier-count statistics) must be
computed as linear passes over each component's edges in topological order (upstream-to-downstream
for upstream aggregates, downstream-to-upstream for downstream barrier counts) rather than by
independently walking the graph from every edge -- with 10M+ edges network-wide, a per-edge walk
does not finish in a reasonable time. Each edge's full vector of per-species/lifecycle values is
computed together in the same pass (one combined upstream/downstream traversal per pipeline
stage, covering every species/lifecycle at once via `propagate_upstream_multi`/
`propagate_downstream_multi`/`propagate_upstream_with_reset_multi` in `graph_stats.py`), rather
than repeating the traversal once per species/lifecycle combination in `reporting_values`.

1. Load the stream network from the output_schema.streams table
2. Break the network at all barriers points and habitat points, updating the length attribute and ensure it is computed in meters
3. Compute effective_length for each edge. For each waterbody(ecatchment_id), the longest mainstem will be determined, and any edges in that waterbody that are not a part of that mainsteam are given an effective_length of 0, all other edges are given their geometric length in meters. An edge with no ecatchment_id or no mainstem_id has nothing to compare it against, so it keeps its own geometric length as effective_length (same outcome as if it were the winning mainstem in its own single-edge waterbody).
4. For each stream segments compute the segments gradient (upstream_elevation - downstream elevation) / segment_length. segment_gradient is NULL if the segment's length is 0 (avoiding division by zero), or if either endpoint's elevation is missing -- either a SQL NULL, or CHyF's -9999 sentinel for "no smoothed-elevation data at this vertex". A NULL segment_gradient falls out of the range check in step 7 below (SQL NULL comparisons are neither true nor false), so such a segment is never assigned as habitat for any species/lifecycle by that check.
5. Compute upstream/downstream barriers per species; Add the following statistics to each stream network edge:
    * For each species, number of impassable anthropogenic barriers upstream of the stream edge (spawnrear)
    * For each species, number of impassable anthropogenic barriers downstream of the stream edge (spawnrear)
    * For each species, number of impassable natural barriers upstream of the stream edge (spawnrear)
    * For each species, number of impassable natural barriers downstream of the stream edge (spawnrear)
    * For each species, the id’s of the impassable anthropogenic barriers upstream of the stream edge
    * For each species, the id’s of the impassable anthropogenic barriers downstream of the stream edge
    * For each species and lifestage (spawn, rear), number of impassable anthropogenic barriers upstream of the stream edge
    * For each species and lifestage (spawn, rear), number of impassable anthropogenic barriers downstream of the stream edge
    * For each species and lifestage (spawn, rear), number of impassable natural barriers upstream of the stream edge
    * For each species and lifestage (spawn, rear), number of impassable natural barriers downstream of the stream edge

    "impassable" (spawnrear) = passability_status_value < impassable_threshold for either lifestage,
where impassable_threshold is a model plan parameter (see model_plan_file.md), default 1.0 -- i.e.
by default anything short of fully passable (0, or a fractional partial-passability value such as
0.25) counts as impassable. "impassable" for a specific lifestage (spawn or rear) checks only that
lifestage's passability_status_value against the same threshold, independent of the other
lifestage's value.

6. compute accessibility; Adds the following values to each stream network edge: For each species
   and lifestage (spawn, rear), that lifestage's accessibility based on:
    * If downstream natural <lifestage> barrier count = 0 then NATURALLY ACCESSIBLE
    * Else NATURALLY INACCESSIBLE

    spawn_accessibility is driven by downstream_natural_spawn_count; rear_accessibility is driven
    by downstream_natural_rear_count -- the two are computed independently of each other. Only
    natural barriers count -- anthropogenic barriers never affect accessibility of either lifestage.

7. Assign Habitat - This script computes habitat possibility for each stream network edge for each species/lifestage:
   * (species <lc>_accessibility = ‘NATURALLY ACCESSIBLE’)
 AND 
(segment_gradient >= min_<lc>_gradient and segment_gradient < max_<lc>_gradient)
AND
(strahler_order >=  min_<lc>_strahler_order and strahler_order <  max_<lc>_strahler_order)

   rear habitat gates on rear_accessibility, spawn habitat gates on spawn_accessibility -- each
   lifestage's habitat assignment uses only its own accessibility value.

8. Process Habitat Access - upddate the species accessiblity values with the data in the habitat_updates table.
    * For habitat updates with only up_points this will flag all upstream stream segments from that point along the mainstem as habitat (or not habitat) for the identified species and lifecycles.  
    * For habitat updates with only down_points this will flag all downstream stream segments from that point along the mainstem as habitat (or not habitat) for the identified species and lifecycles.  
    * For habitat updates with up_ and down_points this will flag the stream segments between these two points as habitat (or not habitat) for the identified species and lifecycles.

    "along the mainstem" is literal: flagging is restricted to the single `mainstem_id` chain
    passing through the snapped point(s), walked upstream/downstream/between along that chain
    only. Edges on a different `mainstem_id` (e.g. a tributary joining partway along the flagged
    stretch) are not flagged, even though they are hydrologically upstream/downstream of the
    point -- this differs from how "upstream"/"downstream" are used elsewhere in Compute
    Statistics (steps 5, 9), which do walk the full branching network across all tributaries.

9. Compute Stream Length

    Each stream edge will have the following length fields added:

    * **Length** - raw length in meters of the edge segment
    * **Effective Length** - raw length, except skeleton edges that are not associated with the
  longest mainstem in their waterbody (connector skeleton edges don't contribute to total
  length calculations)
    * **\<species\> \<lifestage\> Weighted Length**
      * if the edge is not `<lifecycle>` habitat for the species (`<lifecycle>_habitat = false`) then `weighted_length = 0`
      * if the edge's own-lifestage accessibility (`spawn_accessibility` for the spawn `weighted_length`, `rear_accessibility` for the rear `weighted_length`) is not `naturally accessible` then `weighted_length = 0`
      * Otherwise,  `effective_length * weight[lifestage][strahler_order] *
        downstream_first_anthropogenic_not_passability_barrier[species][lifestage]`
      * Notes:
        * only spawning and rearing are computed as lifestages here, NOT the combined
    `spawnrear` lifestage
        * `downstream_first_anthropogenic_not_passability_barrier` is that qualifying barrier's raw `species_passability_value` for the given species/lifestage -- e.g. if th  nearest downstream qualifying barrier has a passability of 0.25 for a species/lifestage, that edge's weighted length gets a 0.25 multiplier, regardless of any barriers further downstream.
        * If multiple qualifying (anthropogenic, passability < 1) barriers are snapped to the same  location, their raw values combine by product for that location, same as step 5's per-location stacking convention -- natural barriers and fully-passable anthropogenic barriers at that same location are excluded from the product.
        * An edge with no qualifying downstream barrier at all gets a multiplier of 1.0 (no degradation).
        * A missing species_lifestage key on a barrier is treated as 0.0 (full barrier), consistent with step 5's "missing = full barrier" convention -- it always qualifies as passability < 1.
        * This value (not itself an upstream aggregate) is stored per stream edge and feeds the weighted upstream \<lifecycle\> length / functional weighted upstream \<lifecycle\> length aggregates for barriers

10. Compute Stream and Barrier output Statistics


## Outstanding Decisions

Items identified during implementation that are real gaps in this spec, not yet resolved, and
currently implemented as a documented placeholder rather than blocking development.

### `supports_species` / "fish species model aoi"

The Outputs section's per-species/lifecycle `streams` fields include:

> `supports_species` - true/false if species is applicable for this stream segment (based on
> fish species model aoi)

No dataset, table, or join key for a "fish species model aoi" (a species range/presence
boundary, as distinct from habitat *suitability*, which the fish species parameter file already
covers via gradient/discharge/channel-confinement/strahler thresholds) is defined anywhere in
this repo's requirements docs. Without it, there's no way to compute which stream segments a
given species actually ranges into versus which are merely physically accessible.

**Current implementation:** `supports_species` is not computed or written anywhere -- it remains
an undefined/unimplemented output field pending a real species-range data source. (An earlier
version of `graph_stats.compute_accessibility` accepted a `supports_species_fn(species, edge_id)
-> bool` callback, always defaulting to `true`, as a placeholder hook for this; it was removed
when accessibility was redefined to depend only on downstream natural spawn-barrier counts, since
the hook was never wired to real data and the placeholder value never affected any accessibility
result.)

### AOI-scoped runs and graph_id boundaries (Compute Statistics steps 5-9)

The implementation plan called for expanding beyond the plan's requested AOI(s), for the
statistics phase only, to include neighboring edges sharing a `graph_id` -- mirroring
`gradient_barriers`' AOI-scoped reprocessing, which reads across into neighboring AOIs to keep
its walk correct near a boundary while only writing barriers for the requested AOI(s).

**Current implementation does not do this.** Compute Statistics steps 5-9 are computed using
only the edges already present in `<output_schema>.streams` -- i.e. only the requested AOI(s).
For an `aoi: workunit: all` run (or any run whose requested AOI(s) happen to fully contain every
`graph_id` they touch) this is fully correct. For a `aoi: workunit`/`aoi: province` run whose
selection is cut by a `graph_id` that extends into a neighboring, non-requested AOI, upstream/
downstream statistics near that boundary will undercount barriers and lengths from the excluded
portion of the network -- edges beyond the boundary are simply absent, not accounted for as
"unknown".

Recommendation until this is addressed: for accurate results near AOI boundaries, run with
`aoi: workunit: all` (the full network) rather than a partial AOI selection, or treat statistics
for edges near a partial run's AOI boundary as approximate.
