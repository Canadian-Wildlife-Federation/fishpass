# Ranked Barriers

**Table:** `<code>_wcrp.ranked_barriers_<species>_<lifecycle>_<code>`

One table per (species, lifecycle) pair in the plan's expanded `reporting_values`, produced by `rank_barriers.py` on every model run. Each holds ONLY the barrier id plus the ranking fields generated during ranking -- nothing is copied from the source view, because the barriers, tracking, and ranked tables are joined into [combined_output_table_vw](./combined_output_view.md) for reporting.

The table lives in the persistent `<code>_wcrp` schema (alongside the [tracking table](./tracking_table.md)) but is dropped and rebuilt on every model run -- don't hand-edit it.

**Row set:** barriers from `<output_schema>.anthropogenic_barriers_<species>` that

* are not fully passable (passability `<> 1`) for the ranked lifestage -- spawn for `spawn`, rear for `rear`, either spawn or rear for `spawnrear` -- **or** are marked `Rehabilitated barrier` in the tracking table's `structure_list_status_<species>` column, **and**
* have upstream habitat for the lifecycle (`<lifecycle>_upstream_length != 0`).

**Units:** every length and gain is in **km**. All length columns are converted from metres to km before any gains or ranks are computed.

**Settings:** `min_avg_gain_km` comes from the plan if set, otherwise from `config/fishpass.yaml` (`wcrp.min_avg_gain_km`, default `0.5`). Owner and grant roles come from `config/fishpass.yaml` (`database_roles`).

Table Structure:

| Field | Type | Comment |
| :---- | :---- | :---- |
| feature_id | uuid | primary key; `all_barriers.feature_id` of the barrier (CABD `cabd_id`, gradient barrier id, or new structure id); matches the combined output view and tracking table |
| group_id | numeric | Barrier group. Barriers start grouped by `mainstem_id`; each group is then repeatedly split at the barrier that maximizes the running average of `<lifecycle>_functional_weighted_disconnected_upstream_length_km`, working from the barrier with the most upstream barriers to the one with the fewest. NULL if the barrier couldn't be placed on a mainstem. |
| num_barriers_group | integer | number of barriers in the group (1 if `group_id` is NULL) |
| total_\<lifecycle\>_hab_gain_group_km | numeric | sum of `<lifecycle>_functional_upstream_length_km` over the group |
| w_total_hab_gain_group_km | numeric | sum of `<lifecycle>_functional_weighted_disconnected_upstream_length_km` over the group |
| avg_gain_per_barrier_km | numeric | `total_<lifecycle>_hab_gain_group_km / num_barriers_group` |
| w_avg_gain_per_barrier_km | numeric | `w_total_hab_gain_group_km / num_barriers_group` |
| downstr_group_ids | varchar[] | group_ids of OTHER groups that contain barriers downstream of this barrier (from the union of `downstream_anthro_spawn_ids` and `downstream_anthro_rear_ids`); NULL if none |
| rank_w_avg_gain_tiered | numeric | **Immediate gain rank.** Barriers are ordered by number of downstream anthropogenic barriers for the lifecycle (fewest first), then by `w_avg_gain_per_barrier_km` (highest first). Groups whose `w_avg_gain_per_barrier_km` is below `min_avg_gain_km` are moved below every group at or above it. Every barrier in a group gets the rank of the group's barrier with the fewest downstream barriers. NULL when `group_id` is NULL or `w_avg_gain_per_barrier_km` is 0. |
| rank_w_total_upstr_\<lifecycle\>_hab | numeric | **Potential gain rank.** Barriers are ordered by `<lifecycle>_weighted_disconnected_upstream_length_km` (highest first); each group takes the best rank among its barriers, then ranks are densified. NULL when `group_id` is NULL, or when both the group's weighted gain and the barrier's weighted upstream length are 0. |
| rank_combined | numeric | **Combined rank.** Dense rank of `rank_w_avg_gain_tiered + rank_w_total_upstr_<lifecycle>_hab` (ties broken by `group_id`). NULL under the same conditions as the two component ranks. Used for `label_in_wcrp_<species>_<lifecycle>` in the combined view. |
| \<species\>_spawn_passability | numeric | the barrier's spawn passability for the species (from `passability_status_spawn`) |
| \<species\>_rear_passability | numeric | the barrier's rear passability for the species (from `passability_status_rear`) |

**Tracking-table check:** before ranking, every tracking-table `feature_id` is checked against `<output_schema>.all_barriers.feature_id` (this replaces a foreign key, which couldn't survive the output schema rebuild). Any ids with no match are logged as a warning and those rows can't affect ranking (e.g. a rehabilitated barrier won't be added back in) until they're corrected.
