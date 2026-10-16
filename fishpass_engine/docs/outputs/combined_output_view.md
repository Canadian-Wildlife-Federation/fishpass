# Combined Output View

**View:** `<code>_wcrp.combined_output_table_vw`

The single WCRP reporting view, with one row per actionable barrier. Rebuilt by `create_combined_view.py` at the end of every model run, using `DROP VIEW` then `CREATE`, so the column set can change between runs (e.g. after adding a species). It lives in the persistent `<code>_wcrp` schema, but anything that depends on it is dropped (`CASCADE`) when it's rebuilt. The view isn't rebuilt outside a model run, so changes such as new CABD attributes or a new label threshold appear after the next model run.

**Row set:** every row of `<output_schema>.all_barriers` whose `feature_type` is NOT a natural feature type (`natural_feature_types_override` from the plan, otherwise `structure_classification.natural_feature_types` from `config/fishpass.yaml` -- e.g. waterfalls, gradients). This keeps dams, stream crossings, and new structures such as beaver dams. It also includes barriers that could not be snapped to the stream network; their passability, length, and ranking columns are NULL.

**Sources and join keys:**

| Source | Joined on |
| :---- | :---- |
| `<output_schema>.all_barriers` (base) | -- |
| `cabd_fdw.dams_view_en` | `cabd_id = all_barriers.feature_id` (NULL for non-dams / non-CABD structures) |
| `cabd_fdw.stream_crossings_sites_structures_view_en` | `cabd_id = all_barriers.feature_id` (NULL for non-crossings / non-CABD structures) |
| `<code>_wcrp.tracking_table_<code>` | `feature_id = all_barriers.feature_id` |
| `<output_schema>.anthropogenic_barriers_<sp>` (per species) | `feature_id = all_barriers.feature_id` |
| `<code>_wcrp.ranked_barriers_<sp>_<lc>_<code>` (per species/lifecycle) | `feature_id = all_barriers.feature_id` |

All joins are LEFT JOINs, so a missing match gives NULLs and never drops the barrier.

**Units:** every length is in **km**. Lengths read from `anthropogenic_barriers_<sp>` (metres) are divided by 1000 and suffixed `_km`, to match the ranked tables.

**Settings:** `label_in_wcrp_rank_threshold` comes from the plan if set, otherwise from `config/fishpass.yaml` (`wcrp.label_in_wcrp_rank_threshold`, default `30`). Owner and grant roles come from `config/fishpass.yaml` (`database_roles`).

View Structure, in column order. `<sp>` = each reporting species and `<lc>` = each lifecycle reported for it (`spawn`, `rear`, `spawnrear`), both from the plan's expanded `reporting_values`.

| Field | Type | Comment |
| :---- | :---- | :---- |
| feature_id | uuid | `all_barriers.feature_id` (CABD `cabd_id`, gradient barrier id, or new structure id); the same value stored in the tracking table |
| feature_type | varchar | from `all_barriers` |
| snapped_geometry | point | from `all_barriers` |
| dam_name_en, dam_use, owner, ownership_type, structure_type, construction_material, up_passage_type, down_passage_route | (CABD) | CABD dam attributes; NULL unless the barrier is a CABD dam. Set by `DAM_ATTRIBUTES` in `create_combined_view.py`. |
| crossing_type_name, crossing_condition_name, land_ownership_context, inlet_shape, outlet_shape, road_name, addressed_status_name, assessment_type_name, date_assessed | (CABD) | CABD stream crossing attributes; NULL unless the barrier is a CABD stream crossing. Set by `STREAM_CROSSING_ATTRIBUTES`. |
| \<sp\>_spawn_passability | double | the barrier's spawn passability for the species |
| \<sp\>_rear_passability | double | the barrier's rear passability for the species |
| \<sp\>_spawn_upstream_accessible_length_km | double | km version of `spawn_upstream_accessible_length` (see [barriers.md](./barriers.md)) |
| \<sp\>_rear_upstream_accessible_length_km | double | km version of `rear_upstream_accessible_length` |
| \<sp\>_\<lc\>_upstream_length_km | double | km version of `<lc>_upstream_length` |
| \<sp\>_\<lc\>_functional_upstream_length_km | double | km version of `<lc>_functional_upstream_length` |
| \<sp\>_\<lc\>_weighted_connected_upstream_length_km | double | km version of `<lc>_weighted_connected_upstream_length` |
| \<sp\>_\<lc\>_weighted_disconnected_upstream_length_km | double | km version of `<lc>_weighted_disconnected_upstream_length` |
| \<sp\>_\<lc\>_functional_weighted_connected_upstream_length_km | double | km version of `<lc>_functional_weighted_connected_upstream_length` |
| \<sp\>_\<lc\>_functional_weighted_disconnected_upstream_length_km | double | km version of `<lc>_functional_weighted_disconnected_upstream_length` |
| group_id_\<sp\>_\<lc\> | numeric | from the ranked table (see [ranked_barriers.md](./ranked_barriers.md)) |
| num_barriers_group_\<sp\>_\<lc\> | integer | from the ranked table |
| total_hab_gain_group_km_\<sp\>_\<lc\> | numeric | ranked table `total_<lc>_hab_gain_group_km` |
| w_total_hab_gain_group_km_\<sp\>_\<lc\> | numeric | from the ranked table |
| avg_gain_per_barrier_km_\<sp\>_\<lc\> | numeric | from the ranked table |
| w_avg_gain_per_barrier_km_\<sp\>_\<lc\> | numeric | from the ranked table |
| downstr_group_ids_\<sp\>_\<lc\> | varchar[] | from the ranked table |
| rank_w_avg_gain_tiered_\<sp\>_\<lc\> | numeric | immediate gain rank, from the ranked table |
| rank_w_total_upstr_hab_\<sp\>_\<lc\> | numeric | potential gain rank (ranked table `rank_w_total_upstr_<lc>_hab`) |
| rank_combined_\<sp\>_\<lc\> | numeric | combined rank, from the ranked table |
| label_in_wcrp_\<sp\>_\<lc\> | text | `yes` when `rank_combined <= label_in_wcrp_rank_threshold`, otherwise `no` (including barriers that weren't ranked) |
| internal_name ... supporting_links | (tracking) | every non-species tracking table column, in tracking-table order (see [tracking_table.md](./tracking_table.md)), except `feature_id`. `road_name` and `structure_type` are renamed `tracking_road_name` and `tracking_structure_type` so they don't clash with the CABD columns of the same name. Set by `TRACKING_NON_SPECIES_COLUMNS`. |
| structure_list_status_\<sp\>, partial_passability_\<sp\>, partial_passability_notes_\<sp\> | (tracking) | per-species tracking columns, for each reporting species |

The per-species blocks are ordered by species code, and the per-(species, lifecycle) ranking blocks by (species, lifecycle).
