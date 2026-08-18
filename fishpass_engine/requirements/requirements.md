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

This is the single working/output table for the stream network — there is no separate
`<output_schema>.flowpath` copy. It is created directly from `chyf_raw.flowpath`/`chyf_raw.aoi`
during Load Stream Network (see below), has vertices inserted into its geometries in place during
structure/habitat snapping, is split into more rows in place during the network-breaking step in
Compute Statistics, and finally has the statistics columns below populated on it. It is never
duplicated into a second table.

All chyf stream network fields, plus `graph_id` and `isolated` (see Load Stream Network)

For each species/lifecycle identified in the reporting_values:

* supports_species - true/false if species is applicable for this stream segment (based on fish species model aoi)
* number of impassable anthropogenic barriers upstream of the stream edge
* number of impassable anthropogenic barriers downstream of the stream edge
* number of impassable natural barriers upstream of the stream edge
* number of impassable natural barriers downstream of the stream edge
* the ids of the impassable anthropogenic barriers upstream of the stream edge
* the ids of the impassable anthropogenic barriers downstream of the stream edge
* accessibility - naturally accessible, naturally inaccessible
* \<lifecycle\>_habitat - true/false if habitat for given species
* upstream accessible length - sum of all upstream edge lengths that are accessible or potentially accessible habitat
* upstream \<lifecycle\> length - sum of all upstream edge lengths that are \<lifecycle\> habitat
* functional upstream \<lifecycle\> length - sum of the upstream edge lengths between barriers that are \<lifecycle\> habitat
* weighted upstream \<lifecycle\> length - sum of all upstream edge weighted lengths that are \<lifecycle\> habitat
* functional weighted upstream \<lifecycle\> length - sum of the upstream weighted edge lengths between barriers that are \<lifecycle\> habitat

For each lifecycle (rear, spawn, general):

* upstream \<lifecycle\> length - sum of all upstream edge lengths that are \<lifecycle\> habitat for at least one species
* functional upstream \<lifecycle\> length - sum of the upstream edge lengths between barriers that are \<lifecycle\> habitat for at least one species
* weighted upstream \<lifecycle\> length - sum of all upstream edge weighted lengths that are \<lifecycle\> habitat for at least one species

`general` is not a lifecycle tracked by the fish species parameter file or the habitat_updates
dataset (neither has a `general` value). `general` habitat/length values are computed as the
union of `rear` and `spawn`: an edge is `general` habitat (for a species, or for "at least one
species" in the per-lifecycle rollups above) if it is `rear` habitat or `spawn` habitat. A
habitat_updates row that changes `rear` or `spawn` habitat therefore implicitly changes `general`
too, through this union — there is no separate `general` update path.

**View:** <output_schema>.natural_barriers

A view over `<output_schema>.all_structures` (`WHERE structure_type = 'natural' AND species_stats
IS NOT NULL` -- the latter restricts it to structures that snapped onto a processed edge, the same
row set the table version held), exposing:

* Passability status per species
* Number of impassable upstream anthropogenic barriers
* Number of impassable upstream natural barriers
* Number of impassable downstream anthropogenic barriers
* Number of impassable downstream natural barriers
* The ids of the impassable downstream anthropogenic barriers
* The ids of the impassable downstream natural barriers

**View:** <output_schema>.anthropogenic_barriers

Same as `natural_barriers` above, but `WHERE structure_type = 'anthropogenic' AND species_stats IS
NOT NULL`:

* Passability status per species
* Number of impassable upstream anthropogenic barriers
* Number of impassable upstream natural barriers
* Number of impassable downstream anthropogenic barriers
* Number of impassable downstream natural barriers
* The ids of the impassable downstream anthropogenic barriers
* The ids of the impassable downstream natural barriers

**Table:** <output_schema>.cabd_<feature_type>

Cached values of the barrier imported from CABD and used in the analysis with all modelling updates applied. For each <feature_type> included in the plan.

**Table:** <output_schema>.gradient_barriers

Cached version of gradient barriers used in analysis.

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
| species_stats | jsonb |
| lifecycle_stats | jsonb |
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
same source as `graph_id`) -- it isn't produced by anything in this tool, but is required by
Compute Statistics step 7 (habitat assignment's `strahler_order >= min_<lc>_strahler_order`
test) and was missing from this field list.

`effective_length` and `segment_gradient` are populated by Compute Statistics steps 3-4.
`species_stats` and `lifecycle_stats` hold steps 5-9's per-species and per-lifecycle output
values (see the Outputs section) as JSON objects keyed by species code / lifecycle, rather than
one SQL column per species/lifecycle combination -- species codes are plan-defined (from
`target_species`), not a fixed schema, so a dynamic column set isn't practical. Same pattern as
`all_structures.species_passability_value`.

Copy Filters:

Model Parameter File: aoi_filter

* aoi = 'working'
  * all - copy all data (not filter required)
  * individual units - create an aoi_id filter on the shortname, by joining to the chyf_raw.aoi table
* aoi = 'province'
  * create a filter using the province_territory_code attribute from the chyf_raw.aoi table
* aoi = 'upstream_of'
  * Fail saying not yet supported. We will implement this later.

### Load Structures (CABD, New, Updates)

#### Step 1: Create structures table

Create a table to hold all structures used in the processing and associated passability status information.

Rows are duplicated per species, so feature_id is not unique on its own.

Table Name: The name will be generated using the model parameter file output_schema. `<output_schema>.all_structures`

| Field | Type | Comment |
| :---- | :---- | :---- |
| id | uuid | system generated primary key |
| feature_id | uuid | cabd_id for feature from cabd, new_structure_id for other features |
| feature_type | string | |
| species_passability_value | jsonb | a json string whose key is the species_lifestage and value is the passability status |
| source | enum (cabd, new_structure) | where the structure/barrier originated |
| structure_type | enum (natural, anthropogenic) | the classification of the structure based on feature_type |
| geometry | point (4617) | original location of barrier |
| snapped_geometry | point (4617) | point snapped to the chyf stream network |
| snapped_edge_id | uuid | id of the `streams` edge this structure snapped to. Not in the original spec -- added because Compute Statistics step 2 (breaking the network) needs to know exactly which edge/vertex to split at. |
| network_vertex_x, network_vertex_y | double precision | the same snapped location as `snapped_geometry`, but in the `streams` table's native SRID rather than 4617, so Compute Statistics can match it against `streams` vertices exactly rather than through a lossy reprojection round-trip. Not in the original spec, added for the same reason as `snapped_edge_id`. |
| species_stats | jsonb | per-species upstream/downstream barrier passability stats (see the natural_barriers/anthropogenic_barriers Outputs entries), written by Compute Statistics step 9. NULL until then, and stays NULL for structures that never snapped onto a processed edge -- the `natural_barriers`/`anthropogenic_barriers` views key off `species_stats IS NOT NULL` to reproduce that row set. Not in the original spec, added so those two outputs can be views over `all_structures` instead of separate tables. |

#### Step 2. Populate the structures table from CABD API

Read appropriate filters from the model parameters file, access the CABD api to download required features and populate the table.

**Model Parameter File: structure_types**

If provided create an api feature filter=feature_type:in:<type1>,<type2>

**aoi_filter**

Filter based on aois in the `<output_schema>.aoi` table. 
  * filter=nhn_watershed_id:in:shortname1,shortname2

**Field Mapping**

* one record per feature, with the species array populated with multiple values

| CABD Field | Structures Table Field |
| :---- | :---- |
| cabd_id | feature_id |
| feature_type | feature_type |
| lat/lon | geometry |
| passability_status_code | species_passability_value* |
| | source = 'cabd' |
| | species[] - one value for each species in the target_species field for each life stage (rear/spawn) |

*the species_passability_value is populated with one key for each species in the target_species (from the model parameter file), for each life stage (rear/spawn), the value computed by mapping the passability_status_code as follows:

  * 4 (Unknown) = 0
  * 1 (Barrier) = 0
  * 2 (Partial Barrier) = 0
  * 3 (Passable) = 1
  * 5 (NA - No Structure) = 1
  * 6 (NA - Decommissioned / Removed) = 1

#### Step 3. Load new structures

Add to the table, the new structures from the input dataset, filtering on the fields provided in the model configuration file.

**Source Table**

In the model configuration file, the structure_new_table parameter identifies the name of the new structures table to use. If not provided default to support.new_structures.

**Model Parameter File: structure_types**

Only include feature types that match.

**Model Parameter File: update_scope**

Only include structures where update_scope = 'all' or update_scope contains the value of the update_scope parameter from the model parameter file.

**Field Mapping**

Create one record for each species in the new structure table passability_status json field.

| Structures Table | New Structure Table |
| :---- | :---- |
| feature_id | new_structure_id |
| feature_type | feature_type |
| geometry | point |
| species_passability_value | passability_status key/value pairs. If lifestage is not provided in the key explode into multiple values, one for each life stage |
| source | fixed - 'new_structure' |

* species with the same passability status value should be merged into a single record; however if they have a different passability status value then multiple records are required.

#### Step 4. Apply structure updates

Apply the structure updates to the structure tables.

**Source Table**

In the model configuration file, the structure_update_table parameter identifies the name of the structure updates table to use. If not provided default to support.structure_updates.

**Model Parameter File: update_scope**

Only include structures where update_scope = 'all' or update_scope contains the value of the update_scope parameter from the model parameter file.

**Ordering**

Apply the updates in the order:

1. update_type = authoritative by update_date asc
2. update_type = local_override by update_date asc

**Field Mapping**

Create one record for each species in the structure updates table passability_status json field.

| Structures Table | Structure Updates Table |
| :---- | :---- |
| feature_id | barrier_id |
| feature_type | feature_type |
| species_passability_value | passability_status key/value pairs. If lifestage is not provided in the key explode into multiple values, one for each life stage |

For json_key if lifestage is not provided assume it applies to both rear and spawn.
If the update changes the passability status value for one species/lifestage but not another in the same record, then a new record needs to be added to the all_structures table

#### Step 5. Snap to the CHyF Network

Snap the structures in the all_structures table to the stream network.

Source Geometry: `<output_schema>.all_structures.geometry`

Target (snapped) Geometry: `<output_schema>.all_structures.snapped_geometry`

Stream Network Geometry: `<output_schema>.streams.geometry`

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

#### Step 6. Add Gradient Barriers

Only run this step if the plan's `structure_types` list contains `gradient_barriers`; otherwise
skip it and exclude gradient barriers from this run entirely.

Copy the gradient barriers from the `support.gradient_barriers` table into the `output_schema.all_structures` table.

Explode the actual_species array so that each entry becomes a key in species_passability_value:

| Structures Table | Gradient Barriers Table |
| :---- | :---- |
| feature_id | id |
| feature_type | 'gradient' |
| geometry | geometry |
| species_passability_value | actual_species - one key for each entry (already encoded as `<species>_<lifestage>`), value 0 (not passable) |
| source | fixed - 'gradient_barriers' |

#### Step 7. Classify Structures

Populate the `output_schema.all_structures` table structure_type based on the following mappings:

| feature type | structure_type |
| :---- | :---- |
| waterfalls | natural |
| gradient | natural |
| _all others_ | anthropogenic |

### Process Habitat

### 1. Load Habitat Updates

Create a working copy of the habitat points, copying relevant records and manipulated as required.

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
| species\_lifestage | string\[\] | Array of species/lifecycle combinations with habitat in the specified area, each encoded as `[not_]<species>[_<spawn|rear>]`. Omitting the `_spawn`/`_rear` suffix means both lifecycles; a `not_` prefix explicitly excludes (clears) that species/lifecycle combination instead of setting it. |
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

Processing is partitioned by `graph_id` (see Load Stream Network): each connected group of edges
is processed independently, which bounds memory/compute to one river system at a time instead of
the whole network, and makes runs resumable/parallelizable per component. Any `graph_id` group
with `is_isolated = true` edges is skipped entirely -- no statistics are computed for it. Steps 5-9 below (the upstream/downstream aggregate and barrier-count statistics) must be
computed as linear passes over each component's edges in topological order (upstream-to-downstream
for upstream aggregates, downstream-to-upstream for downstream barrier counts) rather than by
independently walking the graph from every edge -- with 10M+ edges network-wide, a per-edge walk
does not finish in a reasonable time. Each edge's full vector of per-species/lifecycle values
should be computed together in the same pass, rather than repeating the traversal once per
species/lifecycle combination in `reporting_values`.

1. Load the stream network from the output_schema.streams table
2. Break the network at all barriers points and habitat points, updating the length attribute and ensure it is computed in meters
3. Compute effective_length for each edge. For each waterbody(ecatchment_id), the longest mainstem will be determined, and any edges in that waterbody that are not a part of that mainsteam are given an effective_length of 0, all other edges are given their geometric length in meters.
4. For each stream segments compute the segments gradient (upstream_elevation - downstream elevation) / segment_length
5. Compute upstream/downstream barriers per species; Add the following statistics to each stream network edge:
    * For each species, number of impassable anthropogenic barriers upstream of the stream edge
    * For each species, number of impassable anthropogenic barriers downstream of the stream edge
    * For each species, number of impassable natural barriers upstream of the stream edge
    * For each species, number of impassable natural barriers downstream of the stream edge
    * For each species, the id’s of the impassable anthropogenic barriers upstream of the stream edge
    * For each species, the id’s of the impassable anthropogenic barriers downstream of the stream edge

"impassable" = passability_status_value < impassable_threshold (for either lifestage), where
impassable_threshold is a model plan parameter (see model_plan_file.md), default 1.0 -- i.e. by
default anything short of fully passable (0, or a fractional partial-passability value such as
0.25) counts as impassable. 

6. compute accessibility; Adds the following values to each stream network edge: For each species, the accessibility based on:
    * If (the species if valid for that edge) AND  downstream natural barrier count = 0 and downstream anthropogenic  barrier count = 0 then CONNECTED NATURALLY ACCESSIBLE WATERBODIES 
    * If (the species if valid for that edge) AND  the downstream natural barrier count = 0 and downstream anthropogenic barrier count > 0 then DISCONNECTED NATURALLY ACCESSIBLE WATERBODIES Else NATURALLY INACCESSIBLE WATERBODIES 

7. Assign Habitat - This script computes habitat possibility for each stream network edge for each species/lifestage:
   * (species accessibility = ‘CONNECTED NATURALLY ACCESSIBLE WATERBODIES’ or ‘DISCONNECTED NATURALLY ACCESSIBLE WATERBODIES’)
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
    * For each stream edge for reporting_values compute:
      * Upstream accessible length - sum of all upstream edge lengths that are accessible or potentially accessible habitat
      * Upstream <lifecycle> length - sum of all upstream edge lengths that are <lifecycle> habitat
      * Functional upstream <lifecycle> length - sum of the upstream edge lengths between barriers that are <lifecycle> habitat
      * Weighted upstream <lifecycle> length - sum of all upstream edge weighted lengths that are <lifecycle> habitat
      * Functional weighted upstream <lifecycle> length - sum of the upstream weighted edge lengths between barriers that are <lifecycle> habitat

    * For each stream edge compute, for each lifecycle (spawn, rear, general):
      * Upstream <lifecycle>  length - sum of all upstream edge lengths that are lifecycle habitat for at least one species
      * Functional upstream <lifecycle> length - sum of the upstream edge lengths between barriers that are <lifecycle> habitat for at least one species
      * Weighted upstream <lifecycle> length - sum of all upstream edge weighted lengths that are <lifecycle> habitat

    * For each barrier and each species compute:
      * Number of impassable upstream anthropogenic barriers
      * Number of impassable upstream natural barriers
      * Number of impassable downstream anthropogenic barriers
      * Number of impassable downstream natural barriers
      * The id’s of the impassable downstream natural barriers
      * The id’s of the impassable downstream anthropogenic barriers

    * Weighted length - computed based on the species, lifecycle and stream order as specified in the stream parameter file.

      Formula: `weighted_length = length * weight[strahler_order]`, where `weight[1]` and
      `weight[2]` come from the species parameter file's `stream_order_1_weight` /
      `stream_order_2_weight` (see docs/fish_species_parameter_file.md), and `weight[n] = 1.0`
      (no downweighting) for every `strahler_order >= 3`, since the parameter file documents no
      weight beyond order 2.

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

**Current implementation:** `supports_species` is always `true` for every species/segment. The
engine's accessibility computation (`graph_stats.compute_accessibility`) accepts a
`supports_species_fn(species, edge_id) -> bool` callback for this purpose, defaulting to
"always true" -- so a real species-range data source can be plugged in later without changing
the surrounding algorithm, once one exists.

### Per-lifecycle rollup "functional reset" and "weighted length" (step 9, second bullet group)

Step 9's per-lifecycle (rear/spawn/general) rollup outputs are aggregated across "habitat for at
least one reporting species" -- i.e. not anchored to a single species. Two of that group's
values don't have an unambiguous species-independent definition in this doc:

* **Functional reset**: for a single species, "a barrier resets the upstream length
  calculation" is unambiguous (that species' own barrier). For the multi-species rollup, does an
  edge that's impassable for *some but not all* reporting species reset the accumulation?
  **Implemented as:** it only resets if the edge is impassable for *every* reporting species
  that wanted that lifecycle -- if even one species can still pass, the reach beyond it still
  counts toward "habitat for at least one species".
* **Weighted length**: the weighting formula (step 9's own note, see above) is per-species
  (`stream_order_1_weight`/`stream_order_2_weight` come from each species' own parameter file
  entry), so a species-independent weighted length has no single value to use.
  **Implemented as:** the average of each contributing reporting species' own weight for that
  edge's `strahler_order`.

Both are reasonable defaults, not confirmed requirements -- flagging in case a different
aggregation was intended.

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
