# Barrier Outputs

## `<output_schema>.all_barriers`

All structures used in the analysis, including those loaded from the CABD database, the gardient barriers table, and the new structures table. This table will contain all the updates applied to the structures and used in the modelling.  

| Field Name | Values | Description |
| :---- | :---- | :---- |
| id | uuid | Internal system identifier, generated when `all_barriers` is rebuilt; not stable between model runs. |
| feature\_id | uuid | Source identifier - CABD `cabd_id`, gradient barrier id, or persistent new structure id. This is the barrier key used in exports and tracking. This is stable across model runs for CABD and new structures. For gradient barriers it will change whenever the gradient table is recomputed. |
| feature\_type | varchar | Barrier feature type. |
| source | varchar | Source of barrier (cabd, gradient, new structure) |
| structure\_type | varchar | Natural vs anthropogenic |
| geometry | Point | Raw geometry |
| snapped\_geometry | Point | Geometry snapped to stream network |
| downstream\_edge\_id | uuid | The id of the streams edge that starts at the barrier. It is the same whichever edge the barrier was snapped to: a barrier on the point where two edges meet is always on the edge that starts there. A barrier on the last vertex of an outlet edge (nothing downstream) is on that outlet's zero-length sink edge (`streams.is_sink_barrier_edge`). |
| species\_passability\_value | jsonb | Passability values for each species, lifestage of this barriers.  This should be populated for each species,lifestage the user is reporting on. |
| upstream\_stream\_length | double | Total length (m) of every stream edge upstream of the barrier, all the way to the headwaters. There is no habitat, accessibility, species or barrier filter, so it is one value per barrier. Sums the raw stream `length` (not `effective_length`). Has a value at a confluence (all inflowing edges are added) and is 0 at a headwater. NULL for barriers that did not snap to a processed edge. |
| species\_statistics** | jsonb | This is a jsonb column containing all species statistics. We will use views to convert these into user-friendly columns. See table below for complete description. |

### **species_statistics

This column contains the following fields for each species identifies in the model parameters.

Every upstream length field below is the sum over all the stream edges that flow into the barrier's point (the start of its `downstream_edge_id`). At a confluence it adds every branch, and at a headwater it is 0.

| Field Name | Type | Description |
| :---- | :---- | :---- |
| upstream\_anthro\_spawn\_count | int | Number of spawning impassable anthropogenic barriers upstream of the stream segment edge |
| upstream\_anthro\_rear\_count | int | Number of rearing impassable anthropogenic barriers upstream of the stream segment edge |
| upstream\_anthro\_spawnrear\_count | | |
| downstream\_anthro\_spawn\_count | int | Number of spawning impassable anthropogenic barriers downstream of the stream segment edge |
| downstream\_anthro\_rear\_count | int | Number of rearing impassable anthropogenic barriers downstream  of the stream segment edge |
| downstream\_anthro\_spawnrear\_count | | |
| upstream\_natural\_spawn\_count | int | Number of spawning impassable natural barriers upstream of the stream segment edge |
| upstream\_natural\_rear\_count | int | Number of rearing impassable natural barriers upstream of the stream segment edge |
| upstream\_natural\_spawnrear\_count | int | |
| downstream\_natural\_spawn\_count | int | Number of spawning impassable natural barriers downstream of the stream segment edge |
| downstream\_natural\_rear\_count | int | Number of rearing impassable natural barriers downstream  of the stream segment edge |
| downstream\_natural\_spawnrear\_count | int | |
| upstream\_anthro\_spawn\_ids | uuid\[\] | The barrier ids of anthropogenic structures that are upstream and spawn impassable for the given species. |
| upstream\_anthro\_rear\_ids | uuid\[\] | The barrier ids of anthropogenic structures that are upstream and rear impassable for the given species. |
| downstream\_anthro\_spawn\_ids | uuid\[\] | The barrier ids of anthropogenic structures that are downstream and spawn impassable for the given species. |
| downstream\_anthro\_rear\_ids | uuid\[\] | The barrier ids of anthropogenic structures that are downstream and rear impassable for the given species. |
| downstream\_natural\_spawn\_ids | uuid\[\] | The barrier ids of natural structures that are downstream and spawn impassable for the given species. |
| downstream\_natural\_rear\_ids | uuid\[\] | The barrier ids of natural structures that are downstream and rear impassable for the given species. |
| spawn\_upstream\_accessible\_length | double | The sum of the effective length of any edges upstream flagged as naturally accessible for spawning |
| rear\_upstream\_accessible\_length | double | The sum of the effective length of any edges upstream flagged as naturally accessible for spawning. |
| spawn\_upstream\_below\_gradient\_threshold\_length | double | The sum of the raw stream `length` (not `effective_length`) of every upstream edge whose `segment_gradient` is less than or equal to the species' `accessibility_gradient_spawning_max` (fish species parameter file), all the way to the headwaters. No barrier stops the sum. Edges with no gradient are not counted; a blank threshold means no limit. Has a value at a confluence and is 0 at a headwater. Computed for every species, whether or not spawn is reported. |
| rear\_upstream\_below\_gradient\_threshold\_length | double | Same as spawn upstream below gradient threshold length, but using `accessibility_gradient_rearing_max`. |
| spawn\_upstream\_length | double | The sum of the effective length of any upstream edges flagged as spawning habitat. |
| rear\_upstream\_length | double | The sum of the effective length of any upstream edges flagged as rearing habitat. |
| spawnrear_upstream_length | double | The sum of the effective length of any upstream edges flagged as spawning or rearing habitat. Each edge is only counted once. |
| spawn\_functional\_upstream\_length | double | The sum of all the effective length of all upstream edges flagged as spawning habitat  stopping at the first impassible anthropogenic barrier. |
| rear\_functional\_upstream\_length | double | The sum of all the effective length of all upstream edges flagged as rearing habitat stopping at the first impassible anthropogenic barrier. |
| spawnrear\_functional\_upstream\_length | double | The sum of the maximum (for each edge) of the rear functional length or spawn functional length for all upstream edges stopping at the first impassible anthropogenic barrier. |
| spawn\_weighted\_connected\_upstream\_length | double | The sum of the spawn weighted length (base, undegraded) for all upstream edges, times this barrier's own spawn passability. |
| rear\_weighted\_connected\_upstream\_length | double | Same as spawn weighted connected upstream length but using rear attributes. |
| spawnrear\_weighted\_connected\_upstream\_length | double | The sum of the maximum (for each edge) of the rear weighted length or spawn weighted length (base, undegraded) for all upstream edges, times this barrier's own min(spawn, rear) passability. |
| spawn\_weighted\_disconnected\_upstream\_length | double | The sum of the spawn weighted length (base, undegraded) for all upstream edges, times (1 \- this barrier's own spawn passability). |
| rear\_weighted\_disconnected\_upstream\_length | double | Same as spawn weighted disconnected upstream length but using rear attributes. |
| spawnrear\_weighted\_disconnected\_upstream\_length | double | The sum of the maximum (for each edge) of the rear weighted length or spawn weighted length (base, undegraded) for all upstream edges, times (1 \- this barrier's own min(spawn, rear) passability). |
| spawn\_functional\_weighted\_connected\_upstream\_length | double | The sum of the spawn weighted length (base, undegraded) of all upstream edges stopping at the first impassible anthropogenic barrier, times this barrier's own spawn passability. |
| rear\_functional\_weighted\_connected\_upstream\_length | double | Same as spawn functional weighted connected upstream length but using rear attributes. |
| spawnrear\_functional\_weighted\_connected\_upstream\_length | double | The sum of the maximum (for each edge) of the rear weighted length or spawn weighted length (base, undegraded) for all upstream edges stopped at the first impassible anthropogenic barrier, times this barrier's own min(spawn, rear) passability. |
| spawn\_functional\_weighted\_disconnected\_upstream\_length | double | The sum of the spawn weighted length (base, undegraded) of all upstream edges stopping at the first impassible anthropogenic barrier, times (1 \- this barrier's own spawn passability). |
| rear\_functional\_weighted\_disconnected\_upstream\_length | double | Same as spawn functional weighted disconnected upstream length but using rear attributes. |
| spawnrear\_functional\_weighted\_disconnected\_upstream\_length | double | The sum of the maximum (for each edge) of the rear weighted length or spawn weighted length (base, undegraded) for all upstream edges stopped at the first impassible anthropogenic barrier, times (1 \- this barrier's own min(spawn, rear) passability). |

## Views: `<output_schema>.natural_barriers` and `<output_schema>.anthropogenic_barriers`

The `all_barriers` rows for each structure type that snapped onto a processed stream edge (`species_stats` is populated).

| Field Name | Values | Description |
| :---- | :---- | :---- |
| feature\_id | uuid | `all_barriers.feature_id`. |
| feature\_type | varchar | Barrier feature type. |
| stream\_name\_en | text | English name of the stream edge that starts at the barrier (`downstream_edge_id`), from `streams.stream_name_en`. NULL when that edge has no CHyF name. |
| downstream\_edge\_id | uuid | The id of the streams edge that starts at the barrier. |
| upstream\_stream\_length | double | `natural_barriers` only. `all_barriers.upstream_stream_length`. |
| species\_passability\_value | jsonb | Passability values for each species, lifestage. |
| geometry | Point | Raw geometry |
| snapped\_geometry | Point | Geometry snapped to stream network |
| species\_stats | jsonb | Species statistics (see the species_statistics table above). |
| fall\_name\_en | varchar | `natural_barriers` only. Waterfall English name from `cabd_waterfalls`; NULL for other natural barriers, or when the plan does not load waterfalls. |
| fall\_height\_m | real | `natural_barriers` only. Waterfall height (m) from `cabd_waterfalls`; NULL for other natural barriers, or when the plan does not load waterfalls. |

The waterfall columns come from the `waterfalls` entry in `CUSTOM_FEATURE_TYPE_FIELDS` (`fishpass_engine/scripts/cabd_client.py`).

## Views: `<output_schema>.anthropogenic_barriers_<species>` and `<output_schema>.natural_barriers_<species>`

Each reporting species has anthropogenic and natural views with the computed statistics available as columns. Both expose one identifier, `feature_id`, whose value is `all_barriers.feature_id`. They omit the per-run `all_barriers.id`, which is used internally by the ranking work table.

Besides the statistics columns, each view has `feature_type`, `stream_name_en`, `downstream_edge_id`, `geometry` and `snapped_geometry` (as described for `natural_barriers` / `anthropogenic_barriers` above), and that species' `passability_status_spawn` and `passability_status_rear` (double) from `species_passability_value`.

The `natural_barriers_<species>` views also have `upstream_stream_length` and the `spawn_upstream_below_gradient_threshold_length` and `rear_upstream_below_gradient_threshold_length` columns (both lifestages, whatever is reported). The `anthropogenic_barriers_<species>` views do not have these columns.

## Views: `<output_schema>.unsnapped_barriers`

This view contains barriers that could not be snapped to the stream network. It exposes one identifier, `feature_id`, and omits the per-run `all_barriers.id`.

The `*_ids` columns in barrier and stream statistics contain `all_barriers.feature_id` values, not the per-run `all_barriers.id`.
