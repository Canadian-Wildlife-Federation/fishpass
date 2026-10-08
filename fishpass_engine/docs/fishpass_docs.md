# FishPass Modelling Engine

This engine will be implemented in python.

## Running

This process will be initiated via a GitHub action. A plan file will control parameters for the run. This plan file will be selected by the user when launching the action. Each run will clear all existing data out of the output schema and generate new output data.

Every model run also produces the WCRP outputs: barrier rankings and the combined output view (see [WCRP Outputs](#wcrp-outputs)). These rely on a per-plan tracking table that holds hand-entered data. The tracking table and the WCRP outputs live in a separate `<code>_wcrp` schema, which is not cleared between runs. The tracking table is created on a plan's first run and never modified by the scripts after that; the rankings and the combined view are rebuilt on every run.

### GitHub Limitation

Every job has a 6-hour max execution time on GitHub-hosted runners, regardless of tier. Standard `ubuntu-latest` runners provide 4 vCPU / 16 GB RAM, are free/unlimited on public repos, and billed per-minute on private repos. GitHub-hosted runners are available with Ubuntu Linux, Windows, or macOS; machine maintenance and upgrades are handled by GitHub.

If these limitations prevent us from using a GitHub job, we can containerize the process and run it in the Azure environment.


## FishPass Database

The database connection uses the `FISHPASS_HOST/PORT/DBNAME/USER/PASSWORD` environment variable / GitHub secrets.

The database must be configured before the first model run. See [Database Setup](../README.md#database-setup) in the README for the scripts to run.

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
| Watershed Summary Stats | [outputs/watershed_summary.md](./outputs/watershed_summary.md) |
| Barriers | [outputs/barriers.md](./outputs/barriers.md) |
| CABD Features | [outputs/cabd_features.md](./outputs/cabd_features.md) |
| Gradient Barriers | [outputs/gradient_barriers.md](./outputs/gradient_barriers.md) |
| Habitat Updates | `<output_schema>.habitat_updates`  A copy of the habitat updates table that only includes the rows used for this model. |

### WCRP Outputs

WCRP outputs live in a separate, persistent per-plan schema, `<code>_wcrp` (where `<code>` is the plan's `code`), which a model run **never drops**. The tracking table in it holds hand-entered data and is created once and kept forever; the ranked tables and the combined view are rebuilt on every model run.

| Dataset | Details |
| :---- | :---- |
| WCRP Tracking Table | [outputs/tracking_table.md](./outputs/tracking_table.md) |
| Ranked Barriers | [outputs/ranked_barriers.md](./outputs/ranked_barriers.md) |
| Combined Output View | [outputs/combined_output_view.md](./outputs/combined_output_view.md) |

Settings used by these outputs live in [config/fishpass.yaml](../../config/fishpass.yaml): `database_roles` (owner and grant roles for every WCRP object) and `wcrp` (`label_in_wcrp_rank_threshold`, `min_avg_gain_km`). Both `wcrp` values can be overridden per plan with a field of the same name in the model plan file.


## Computations

### Accessibility

* IF (downstream natural impassable barrier for species/lifecycle count = 0) THEN 
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
  * Otherwise the effective length * (<species> <lifestage> strahler order weighting)
  * Note: Only need spawning and rearing as lifestage, NOT combined
* <species> <lifestage> Weighted Connected Length
  * If the edge is not habit for <species> <lifestage> then 0
  * If the edge is not naturally accessible for <species> <lifestage> then 0
  * Otherwise the effective length * (<species> <lifestage> strahler order weighting) * (<species> <lifestage> passability status of the first not-passable anthropogenic downstream barrier)
  * Note: Only need spawning and rearing as lifestage, NOT combined
* <species> <lifestage> Weighted Disconnected Length
  * If the edge is not habit for <species> <lifestage> then 0
  * If the edge is not naturally accessible for <species> <lifestage> then 0
  * Otherwise the effective length * (<species> <lifestage> strahler order weighting) * (1 - <species> <lifestage> passability status of the first not-passable anthropogenic downstream barrier)
  * Note: Only need spawning and rearing as lifestage, NOT combined

### 'Functional' Upstream Length
 * A barrier ‘resets’ the upstream length calculation when the barrier is a non-passable anthropogenic barrier.

### Barrier Weighted Upstream Length

For each barrier and <species> <lifestage>, the base weighted lengths (see above, not the
connected/disconnected variants) of edges upstream of the barrier are summed, then split by that
barrier's own passability status for <species> <lifestage>:
* <species> <lifestage> Weighted Connected Upstream Length = (sum of base weighted lengths in the total upstream area) * (passability of the barrier)
* <species> <lifestage> Weighted Disconnected Upstream Length = (sum of base weighted lengths in the total upstream area) * (1 - passability of the barrier)
* <species> <lifestage> Weighted Functional Connected Upstream Length = (sum of base weighted lengths in the upstream area, stopping at the first non-passable anthropogenic barrier) * (passability of the barrier)
* <species> <lifestage> Weighted Functional Disconnected Upstream Length = (sum of base weighted lengths in the upstream area, stopping at the first non-passable anthropogenic barrier) * (1 - passability of the barrier)

The definition of 'functional' (stopping at the first non-passable anthropogenic barrier) is
unchanged from above. For spawnrear, the passability of the barrier is the minimum of its spawn and
rear passability (matching the combined "impassable if either lifestage fails" rule used elsewhere).
 


## Processing

The model run is a single sequence of phases against one database connection/transaction scope,
in this order. If any phase raises an error the whole run is rolled back.

### Sync WCRP Enums and Set Up Tracking Table

This is the first phase of a model run. It runs before the output schema is dropped, so if it
fails, the previous run's outputs are still in place.

The run first syncs the `support.tt_*` enum types from the `wcrp.tracking_table_enums` section of
`config/fishpass.yaml`. A type that doesn't exist is created with the listed values plus a blank
(`''`) value last. For a type that exists, any listed value it doesn't have is added just before
the blank. Existing values and data are never changed, so if nothing has changed in the YAML,
nothing changes in the database. Values removed or renamed in the YAML are not applied (see
[tracking_table.md](./outputs/tracking_table.md)).

It then checks for the plan's `<code>_wcrp.tracking_table_<code>`. If the table doesn't exist, it
is created (along with the `<code>_wcrp` schema, if needed). If it exists, creation is skipped and
the table and its data are left unchanged. Either outcome is reported in the log and the job
summary. The run stops here if `support.blank2null()` is missing (see
[Database Setup](../README.md#database-setup)).

Finally, it checks the prerequisites of the WCRP phases that run last (Rank Barriers, Create
Combined View), so a problem stops the run now and not after the whole model has been computed.
The tracking table must have every column the run reads, including the per-species columns for
each reporting species. An existing tracking table is never altered, so a species added to the
plan later needs its columns added by hand. The `cabd_fdw` foreign tables the combined view joins
must also exist and have the attributes it surfaces (see
`init/database/fishpass_cabd_raw_init.sql`).

### Initialize

Drop the existing output schema (if it exists) and create a new one using the output_schema name from the model parameters file.

### Load Stream Network

Create a working copy of the stream network, copying relevant records the flowpath and aoi tables into the output schema. This working copy is `<output_schema>.streams` — the same table named in
Outputs above, created here directly rather than as a separate `<output_schema>.flowpath` table
that gets copied again later. It accumulates the statistics columns from Compute Statistics in
place, and is split into more rows in place during that phase's network-breaking step.

Inputs: `chyf_raw.flowpath`, `chyf_raw.aoi`

Outputs:  `<output_schema>.streams`, `<output_schema>.aoi`


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
Compute Statistics' habitat assignment (`strahler_order >= min_<lc>_strahler_order`
test) and was missing from this field list.

`effective_length` and `segment_gradient` are populated by Compute Statistics.

`downstream_route_measure` and `upstream_route_measure` are populated by Compute Statistics'
per-component processing, alongside `species_stats`. They are a per-`mainstem_id` linear-referencing measure, in `length`
units (not `effective_length`): 0 at the mouth of the edge's `mainstem_id` chain (the edge with no
successor, i.e. a network outlet, or whose successor belongs to a *different* `mainstem_id`, i.e.
it joins a larger mainstem), increasing upstream to the chain's headwater.
`downstream_route_measure` is the distance from the chain's mouth to this edge's downstream end;
`upstream_route_measure` is that plus this edge's own `length` (its distance to the edge's upstream
end). Each mainstem's measure resets to 0 at its own mouth -- it is not a single measure running
continuously from the network outlet. Edges with `mainstem_id IS NULL` are left NULL for both
columns (there is no chain to measure them along).

`species_stats` holds Compute Statistics' per-species output values (see the Outputs section) as a JSON
object keyed by species code, rather than one SQL column per species -- species codes are
plan-defined (from `target_species`), not a fixed schema, so a dynamic column set isn't practical.
Same pattern as `all_barriers.species_passability_value`/`species_stats`.

**Copy Filters**

Model Parameter File: aoi_filter

* aoi = 'workunit'
  * all - copy all data (not filter required)
  * individual units - create an aoi_id filter on the shortname, by joining to the chyf_raw.aoi table
* aoi = 'province'
  * create a filter using the province_territory_code attribute from the chyf_raw.aoi table
* aoi = 'upstream_of'
  * Resolve the given edge ids' `graph_id`(s) (failing the run if any edge id doesn't exist), then
    filter on every `chyf_raw.aoi` id that has any edge on those `graph_id`(s) -- the same
    aoi_id-based copy as above, so the full `graph_id` component(s) get copied in.
  * Immediately afterward, delete every copied edge that isn't upstream of (or one of) the given
    edges -- computed per `graph_id` as the union of each given edge's own upstream closure (edge +
    all ancestors), which also strips out any unrelated `graph_id` pulled in incidentally by
    sharing an aoi with a target `graph_id`.

### Load Structures

Builds `<output_schema>.all_barriers` (see Outputs) and its `cabd_<feature_type>` cache tables,
in this order:

1. For each `structure_types` entry (excluding `gradients`), fetch that feature type from the
   CABD API for the plan's AOI and load it straight into its own `cabd_<feature_type>` cache
   table, one feature type at a time, committing before moving to the next -- so only one feature
   type's data is held in memory at once. Features with passability type code 5 (no structure) or
   `use_analysis = false` are excluded at this point.
2. `all_barriers` is populated by reading back from those cache tables (source `cabd`, or
   `gradient_barriers` for gradient rows).
3. New structures (`support.new_structures`, or the plan's override table) are added, filtered by
   `structure_types` and `update_scope`.
4. Structure updates (`support.structure_updates`, or the plan's override table) are applied on
   top, oldest-first within `update_type = authoritative`, then `update_type = local_override`
   -- each update only touches the species it has an entry for, leaving any other species'
   existing passability value as-is.
5. If `gradients` is in `structure_types`, gradient barriers (`support.gradient_barriers`, or the
   plan's override table) are added as further `all_barriers` rows.
6. Every row is classified `natural`/`anthropogenic` from `config/fishpass.yaml`'s (or the
   plan's `natural_feature_types_override`) natural-feature-types list -- anthropogenic is the
   fallback for anything not explicitly listed as natural.

### Snap Structures

Snaps every `all_barriers` point onto the stream network, writing `snapped_geometry` and
`downstream_edge_id` (Compute Statistics' network-breaking step later derives `upstream_edge_id`
for the other side of the split). For each structure: find the nearest streams edge within
`structure_snap_edge_distance_m` (default 100m); if an existing vertex on that edge is within
`structure_snap_vertex_distance_m` (default 50m), snap to it; otherwise insert a new vertex at
the snapped point, interpolating its elevation.

### Process Habitat

Loads and snaps habitat update points the same way structures are snapped, so the resulting
`<output_schema>.habitat_updates` table (see Outputs) has its upstream/downstream point(s) tied
to specific stream network vertices for use in Compute Statistics:

1. Load rows from `support.habitat_updates` (or the plan's override table) that fall within
   `habitat_point_snap_edge_distance_m` of the stream network.
2. Snap each row's point(s) (upstream, downstream, or both for `location_type = between`) onto
   the network: if a `chyf_upstream_edge_id`/`chyf_downstream_edge_id` is given, snap to that
   specific edge within `habitat_point_snap_edge_distance_m` or fail the run; otherwise snap to
   the nearest edge within that tolerance, or silently skip the point if none is in range. Once
   an edge is chosen, snap to an existing vertex within `habitat_point_snap_vertex_distance_m`,
   or insert a new one, interpolating elevation. Points on the same edge share a single working
   copy of that edge so their inserted vertices are visible to each other.

### Compute Statistics

The core numerical phase. It runs once at network scale (breaking, effective length, gradient),
then partitions the rest of the work by `graph_id` (see Load Stream Network) so each connected
group of edges is processed independently -- this bounds memory/compute to one river system at a
time instead of the whole network. Components are packed largest-first into edge-count-bounded
bundles for bulk fetch/write, and any `graph_id` group made up of `is_isolated = true` edges is
skipped entirely.

1. **Break the network** at every vertex a structure or habitat point snapped onto (structure/
   habitat snapping already ensured a real vertex exists there): each affected edge is split into
   segments at those vertices, and the corresponding `all_barriers`/`habitat_updates` edge-id
   references are corrected to point at the resulting segments. Edges with no snapped point are
   left untouched.
2. **Compute `effective_length` and `segment_gradient`** for every edge in one pass: per
   `ecatchment_id`, only the longest mainstem's edges keep their geometric length as
   `effective_length` (others get 0); `segment_gradient` is the elevation drop between an edge's
   endpoints divided by its length, or NULL if the length is 0 or either endpoint's smoothed
   elevation is missing.
3. **Per connected component**, compute every species/lifecycle's statistics together in a
   handful of linear passes over the component's edges in topological order (rather than walking
   the graph separately from every edge, which wouldn't finish at network scale), in this order:
   * upstream/downstream impassable-barrier counts per species and lifecycle (natural vs.
     anthropogenic; see Computations for the impassability rule);
   * accessibility, from the downstream natural-barrier counts;
   * habitat assignment, gating on accessibility plus each species/lifecycle's gradient and
     Strahler-order thresholds;
   * habitat-access overrides from `habitat_updates`, applied along the relevant `mainstem_id`
     chain only (not the full branching network), followed by deriving the combined `spawnrear`
     habitat/accessibility values from the spawn and rear results;
   * each edge's weighted length per species/lifecycle (see Computations), using the passability
     of the nearest qualifying downstream anthropogenic barrier as a multiplier, with functional
     upstream length resetting at non-passable anthropogenic barriers.
4. **Write the results back**: per-edge stats into `streams.species_stats`, per-barrier stats
   (including each barrier's upstream length figures) into `all_barriers.species_stats`. If the
   plan includes gradient barriers, `<output_schema>.gradient_barriers` (see Outputs) is
   populated at this point by copying the relevant, now-snapped rows back out of `all_barriers`.

### Create Barrier Views

Once statistics are populated, create the reporting views over `all_barriers`/`streams`:
`natural_barriers`, `anthropogenic_barriers`, and `unsnapped_barriers`, plus a per-species
`natural_barriers_<species>`/`anthropogenic_barriers_<species>`/`streams_<species>` view for each
`target_species` in the plan, with that species' `species_stats` fields exploded to columns.

### Summary Statistics

A single `watershed_summary_stats` materialized view is created: one row per reporting
species with total/connected/disconnected habitat length (km) and percent-disconnected figures,
computed directly from `streams.species_stats` in one pass over the network (see
[outputs/watershed_summary.md](./outputs/watershed_summary.md)).

### Rank Barriers

`rank_barriers.py`. For each (species, lifecycle) pair in the plan's expanded `reporting_values`,
build `<code>_wcrp.ranked_barriers_<species>_<lifecycle>_<code>` (see
[outputs/ranked_barriers.md](./outputs/ranked_barriers.md)):

- First, check every tracking-table `feature_id` against the freshly built `all_barriers.feature_id`
  (this replaces a foreign key, which couldn't survive the output schema rebuild). Ids with no match
  are logged as a warning; those rows can't affect ranking until corrected.
- Select the barriers from `anthropogenic_barriers_<species>` that are not fully passable for the
  ranked lifestage (spawn, rear, or either for spawnrear), plus any marked `Rehabilitated barrier`
  in the tracking table, that have upstream habitat for the lifecycle.
- Convert every length column on the working table from metres to km (`_km` suffix).
- Group barriers by mainstem, then repeatedly split each group at the barrier that maximizes the
  running average weighted functional gain.
- Compute per-group habitat gains, downstream group ids, and three ranks: immediate gain
  (`rank_w_avg_gain_tiered`, with groups below `min_avg_gain_km` moved to the bottom), potential
  gain (`rank_w_total_upstr_<lifecycle>_hab`), and their combination (`rank_combined`).
- Write a slim output table containing only `feature_id` and the ranking fields.

### Create Combined View

`create_combined_view.py`. Rebuild `<code>_wcrp.combined_output_table_vw` (see
[outputs/combined_output_view.md](./outputs/combined_output_view.md)): one row per actionable
barrier (all `all_barriers` rows except natural feature types), joining CABD attributes (via the
`cabd_fdw` foreign tables), per-species passability and habitat lengths (in km), every ranked table,
and the tracking table.


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
