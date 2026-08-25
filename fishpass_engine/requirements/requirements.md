# FishPass Modelling Engine

This engine will be implemented in python.

## Running

This process will be initiated via a GitHub action. A plan file will control parameters for the run. This plan file will be selected by the user when launching the action. Each run will clear all existing data out of the output schema and generate new output data.

### GitHub Limitation

Every job has a 6-hour max execution time on GitHub-hosted runners, regardless of tier. Standard `ubuntu-latest` runners provide 4 vCPU / 16 GB RAM, are free/unlimited on public repos, and billed per-minute on private repos. GitHub-hosted runners are available with Ubuntu Linux, Windows, or macOS; machine maintenance and upgrades are handled by GitHub.

If these limitations prevent us from using a GitHub job, we can containerize the process and run it in the Azure environment.

## Input Data Sources

### 1. FishPass Database

The database connection uses the `FISHPASS_HOST/PORT/DBNAME/USER/PASSWORD` environment variable / GitHub secrets.

### 2. CABD Barrier API

Base URL: `https://cabd-web.azurewebsites.net/cabd-api/`

Feature Type Endpoint: `https://cabd-web.azurewebsites.net/cabd-api/features/<feature_type>/`

Returns a GeoJSON feature collection for the requested `<feature_type>` (e.g. `dams`, `waterfalls`, `stream_crossings`).

**Filtering**

Results can be filtered with one or more `filter=<field>:in:<value1>;<value2>` query parameters, combined with `&`. Two filters are relevant here:

* `nhn_watershed_id` — filters by AOI (workunit short name)
* `feature_type` — filters by structure type (only needed if not already filtering via the feature type endpoint above)

Example — waterfalls, dams, and stream crossings in watersheds `02PH002` and `02PH001`:

```text
https://cabd-web.azurewebsites.net/cabd-api/features/waterfalls?filter=nhn_watershed_id:in:02PH002;02PH001&filter=feature_type:in:dams;stream_crossings
```

Full documentation: https://cabd-docs.netlify.app/docs_tech/docs_tech_arch_api

**Result cap:** a single query is capped at 50,000 features. Structure loading calls the API once
per feature type by default, which normally keeps each call under the cap. If a single feature
type's query for the requested AOI(s) still exceeds 50,000 features, that request must be further
split by work-unit subgroup. A response of exactly 50,000 features should be treated as a signal
the result was truncated, not assumed to be a complete result.


## Input Datasets

| Dataset | Details |
| :---- | :---- |
| Model Plan File | fishpass/requirements/inputs/model_plan_file.md |
| Fish Species Parameters | docs/fish_species_parameter_file.md |
| Stream Network | See below |
| Barriers | See below |
| Gradient Barriers | See below |
| Habitat Updates | fishpass/requirements/inputs/habitat_updates_dataset.md |
| Structure Updates | fishpass/requirements/inputs/structure_updates_dataset.md |
| New Structures | fishpass/requirements/inputs/structure_new_dataset.md |

### CHyF Stream Network

Source: FishPass Database

Table: chyf_raw.flowpath

Loaded using the chyf_loader tools.

### Structure Barriers

Source: CABD API

### Gradient Barriers

Source: FishPass Database

Table: support.gradient_barriers

Loaded using the gradient_barriers tools.

## Outputs

Each model run will generate its own schema for the output. The schema name is defined in the model plan parameters file (`output_schema`). If the output schema already exists, the existing schema will be dropped and a new one created.

**Table:** <output_schema>.streams

This is the single output table for the stream network. It is created directly from `chyf_raw.flowpath`
during Load Stream Network (see below), then is maniuplated to have vertices inserted during the snapping phases, and
edges split during the network breaking phases. The output statistics columns below populated on this data. Due to the snapping and breaking there will be more edges in the output table than the raw input table.

The output statistics are stored in a single jsonb column.

For each species/lifecycle identified in the reporting_values:

* supports_species - true/false if species is applicable for this stream segment (based on fish species model aoi)
* number of impassable anthropogenic barriers upstream of the stream edge (spawnrear)
* number of impassable anthropogenic barriers downstream of the stream edge (spawnrear)
* number of impassable natural barriers upstream of the stream edge (spawnrear)
* number of impassable natural barriers downstream of the stream edge (spawnrear)
* for each lifestage (spawn, rear): number of impassable anthropogenic barriers upstream of the stream edge
* for each lifestage (spawn, rear): number of impassable anthropogenic barriers downstream of the stream edge
* for each lifestage (spawn, rear): number of impassable natural barriers upstream of the stream edge
* for each lifestage (spawn, rear): number of impassable natural barriers downstream of the stream edge
* the ids of the impassable anthropogenic barriers upstream of the stream edge
* the ids of the impassable anthropogenic barriers downstream of the stream edge
* accessibility - naturally accessible, naturally inaccessible (see Compute Statistics step 6)
* \<lifecycle\>_habitat - true/false if habitat for given species
* for each lifestage (spawn, rear): weighted_length - see Compute Statistics step 9's
  `weighted_length` formula, degraded by the passability of the nearest ("first") downstream
  barrier of any type (natural or anthropogenic)

`spawnrear` is not a lifecycle tracked by the fish species parameter file or the habitat_updates
dataset (neither has a `spawnrear` value). `spawnrear` habitat values are computed as the union of
`rear` and `spawn`: an edge is `spawnrear` habitat for a species if it is `rear` habitat or `spawn`
habitat.

Upstream length aggregates (accessible length, and per-lifecycle upstream/functional
upstream/weighted upstream/functional weighted upstream length) are not stored per stream edge --
they are computed per edge internally but only written out at each barrier's own position (see the
`natural_barriers`/`anthropogenic_barriers` Outputs entries below), since the values are of
interest relative to a barrier, not per-edge. `weighted_length` (singular, per lifestage) is the
exception: unlike those aggregates, it's already a per-edge, non-aggregate quantity, so it's
written to every stream edge's own `species_stats` in addition to being surfaced at barrier
positions.


**View:** <output_schema>.natural_barriers

A view over `<output_schema>.all_barriers` (`WHERE structure_type = 'natural' AND species_stats
IS NOT NULL` -- the latter restricts it to structures that snapped onto a processed edge, the same
row set the table version held), exposing:

* Passability status per species
* Number of impassable upstream anthropogenic barriers (spawnrear)
* Number of impassable upstream natural barriers (spawnrear)
* Number of impassable downstream anthropogenic barriers (spawnrear)
* Number of impassable downstream natural barriers (spawnrear)
* For each lifestage (spawn, rear): number of impassable upstream anthropogenic barriers
* For each lifestage (spawn, rear): number of impassable upstream natural barriers
* For each lifestage (spawn, rear): number of impassable downstream anthropogenic barriers
* For each lifestage (spawn, rear): number of impassable downstream natural barriers
* The ids of the impassable downstream anthropogenic barriers
* The ids of the impassable downstream natural barriers
* Per reporting species: upstream accessible length - the upstream accessible length value (see
  Compute Statistics step 9) at the barrier's own position
* Per reporting species, per lifecycle: upstream \<lifecycle\> length, functional upstream
  \<lifecycle\> length, and (spawn/rear only) weighted upstream \<lifecycle\> length/functional
  weighted upstream \<lifecycle\> length -- the corresponding step 9 value at the barrier's own
  position, taken as-is (no adjustment for the barrier's own edge)
* Per reporting species, per lifestage (spawn, rear only): weighted length, degraded by the
  passability of the nearest ("first") downstream barrier of any type (see step 9's
  `weighted_length` bullet) -- also taken as-is at the barrier's own position

**View:** <output_schema>.anthropogenic_barriers

Same as `natural_barriers` above, but `WHERE structure_type = 'anthropogenic' AND species_stats IS
NOT NULL`:

* Passability status per species
* Number of impassable upstream anthropogenic barriers (spawnrear)
* Number of impassable upstream natural barriers (spawnrear)
* Number of impassable downstream anthropogenic barriers (spawnrear)
* Number of impassable downstream natural barriers (spawnrear)
* For each lifestage (spawn, rear): number of impassable upstream anthropogenic barriers
* For each lifestage (spawn, rear): number of impassable upstream natural barriers
* For each lifestage (spawn, rear): number of impassable downstream anthropogenic barriers
* For each lifestage (spawn, rear): number of impassable downstream natural barriers
* The ids of the impassable downstream anthropogenic barriers
* The ids of the impassable downstream natural barriers
* Per reporting species: upstream accessible length, and per lifecycle the same upstream length
  figures as `natural_barriers` above

**View:** <output_schema>.unsnapped_structures

A view over `<output_schema>.all_barriers` (`WHERE snapped_geometry IS NULL`), exposing all
structures that never snapped onto the stream network.

**Table:** <output_schema>.cabd_<feature_type>

Cached raw values of the barrier as imported from CABD, before any structure_updates,
new_structures, gradient, classification, or snapping logic is applied -- an immutable record of
the source data, distinct from `all_barriers` which holds the working/modelled values used
throughout the rest of the pipeline. For each <feature_type> included in the plan (excluding
`gradients`).

| Field | Type | Comment |
| :---- | :---- | :---- |
| cabd_id | uuid | cabd_id for the feature; primary key |
| species_passability_value | jsonb | as computed in Load Structures step 2, before any later updates |
| passability_status_code | integer | raw passability_status_code as returned by CABD, unmapped |
| geometry | point | original location of barrier, as returned by CABD |

(No `source` column here -- every row in this table came from CABD by construction, so a column
that would always read `'cabd'` was dropped. No `feature_type` column either -- the table is
already scoped to a single feature type via its `<feature_type>`-suffixed name.)

**Table:** <output_schema>.gradient_barriers

Cached version of gradient barriers containing only barriers used in this analysis, with their
snapped location. Unlike `cabd_<feature_type>`, this is not an immutable pre-processing copy of
the source table -- it is created and populated during Compute Statistics (after Add Gradient
Barriers and Snap to the CHyF Network have already run), by copying the relevant rows back out of
`all_barriers` (`WHERE source = 'gradient_barriers'`). Only created when the plan's
`structure_types` list contains `gradients`.

| Field | Type | Comment |
| :---- | :---- | :---- |
| id | uuid | system generated primary key, copied from `all_barriers.id` |
| feature_id | uuid | id of the source row in the gradient barriers table |
| species_passability_value | jsonb | as computed in Add Gradient Barriers |
| geometry | point | original location of barrier |
| snapped_geometry | point (4617) | point snapped to the chyf stream network |

**Table:** <output_schema>.all_barriers

All structures used in the analysis, including those loaded from the CABD database, the gardient barriers table, and the new structures table. This table
will contain all the updates applied to the structures and used in the modelling.  

**Table:** <output_schema>.habitat_updates

A copy of the habitat updates table that only includes the rows used for this model.


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

6. compute accessibility; Adds the following values to each stream network edge: For each species, the accessibility based on:
    * If downstream natural spawn barrier count = 0 then NATURALLY ACCESSIBLE
    * Else NATURALLY INACCESSIBLE

    Only natural barriers that are impassable for the spawn lifestage count -- anthropogenic
    barriers and rear-only impassability do not affect accessibility.

7. Assign Habitat - This script computes habitat possibility for each stream network edge for each species/lifestage:
   * (species accessibility = ‘NATURALLY ACCESSIBLE’)
 AND 
(segment_gradient >= min_<lc>_gradient and segment_gradient < max_<lc>_gradient)
AND
(strahler_order >=  min_<lc>_strahler_order and strahler_order <  max_<lc>_strahler_order)

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

9. Compute Barrier Upstream Values
    * all length computations should use effective_length
    * For each stream edge, for each reporting species, compute (internally -- not stored per
      edge, only surfaced at each barrier's position, see below):
      * Upstream accessible length - sum of all upstream edge lengths that are accessible or potentially accessible habitat
      * Upstream <lifecycle> length - sum of all upstream edge lengths that are <lifecycle> habitat
      * Functional upstream <lifecycle> length - sum of the upstream edge lengths between barriers that are <lifecycle> habitat
      * Weighted upstream <lifecycle> length - sum of all upstream edge weighted lengths (see
        `weighted_length` below, already degraded by each edge's own nearest downstream barrier)
        that are <lifecycle> habitat
      * Functional weighted upstream <lifecycle> length - sum of the upstream weighted edge lengths
        (same degraded `weighted_length` values) between barriers that are <lifecycle> habitat

    * For each barrier and each species compute:
      * Number of impassable upstream anthropogenic barriers (spawnrear)
      * Number of impassable upstream natural barriers (spawnrear)
      * Number of impassable downstream anthropogenic barriers (spawnrear)
      * Number of impassable downstream natural barriers (spawnrear)
      * The id’s of the impassable downstream natural barriers
      * The id’s of the impassable downstream anthropogenic barriers
      * For each lifestage (spawn, rear): number of impassable upstream anthropogenic barriers
      * For each lifestage (spawn, rear): number of impassable upstream natural barriers
      * For each lifestage (spawn, rear): number of impassable downstream anthropogenic barriers
      * For each lifestage (spawn, rear): number of impassable downstream natural barriers
      * The upstream accessible length and per-lifecycle upstream/functional upstream/weighted
        upstream/functional weighted upstream length values above, read at the barrier's own
        snapped edge, taken as-is (unlike the barrier counts above, there is no subtraction of the
        barrier's own edge from the length figures)

    * Weighted length - computed based on the species, lifecycle and stream order as specified in the stream parameter file. Only computed for the spawn/rear lifecycles -- not computed for spawnrear.

      Formula: `weighted_length = length * weight[lifecycle][strahler_order]`, where
      `weight["spawn"][1]`/`weight["spawn"][2]` come from the species parameter file's
      `stream_order_1_spawning_weight`/`stream_order_2_spawning_weight`,
      `weight["rear"][1]`/`weight["rear"][2]` come from `stream_order_1_rearing_weight`/
      `stream_order_2_rearing_weight` (see docs/fish_species_parameter_file.md), and
      `weight[lifecycle][n] = 1.0` (no downweighting) for every `strahler_order >= 3`, since the
      parameter file documents no weight beyond order 2. There is no `weight["spawnrear"]` --
      "spawnrear" is a union of rear/spawn habitat rather than its own habitat purpose, and has no
      dedicated weight field, so weighted length is not computed for it.

      This per-edge weighted length is further degraded by the passability of the nearest ("first")
      barrier downstream of the edge, of any structure type (natural or anthropogenic -- not
      anthropogenic-only): `weighted_length = effective_length * weight[lifestage][strahler_order] *
      downstream_first_barrier_passability[species][lifestage]`, where
      `downstream_first_barrier_passability` is that barrier's raw (not threshold-based)
      `species_passability_value` for the given species/lifestage -- e.g. if the nearest downstream
      barrier has a passability of 0.25 for a species/lifestage, that edge's weighted length gets a
      0.25 multiplier, regardless of any barriers further downstream. If multiple barriers are
      snapped to that nearest location, their raw values combine by product for that location, same
      as step 5's per-location stacking convention. An edge with no downstream barrier at all gets a
      multiplier of 1.0 (no degradation). A missing species_lifestage key on a barrier is treated as
      0.0 (full barrier), consistent with step 5's "missing = full barrier" convention. This is a
      per-edge value (not masked to habitat edges, and not itself an upstream aggregate) -- it is
      stored per stream edge (see the streams Outputs entry above) as well as surfaced at each
      barrier's position (see the natural_barriers/anthropogenic_barriers Outputs entries), and also
      feeds the Weighted upstream <lifecycle> length / Functional weighted upstream <lifecycle>
      length aggregates above (habitat-masked).

     * Functional Values: A barrier ‘resets’ the upstream length calculation.

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
