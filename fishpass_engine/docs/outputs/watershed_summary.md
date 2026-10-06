# Watershed Summary Outputs

### Views: `<output_schema>.watershed_summary_stats`

A single materialized view, with one row per reporting species, summarizing total habitat length and connectivity across the whole output schema. It is built directly from `<output_schema>.streams` in a single pass, laterally unnesting each edge's `species_stats` for the species found in the plan's `reporting_species_lifecycles`.

| Field Name | Type | Description |
| :---- | :---- | :---- |
| species | varchar | Species code. |
| total\_km | double | Sum of raw `effective_length` across all edges, in km (not weighted). |
| total\_spawn\_km | double | Sum of `spawn_weighted_length` across all edges, in km. |
| total\_rear\_km | double | Sum of `rear_weighted_length` across all edges, in km. |
| total\_spawnrear\_km | double | Sum, per edge, of the lesser of its spawn/rear weighted length, in km. |
| connected\_spawn\_km | double | Sum of `spawn_weighted_connected_length` across all edges, in km. |
| disconnected\_spawn\_km | double | Sum of `spawn_weighted_disconnected_length` across all edges, in km. |
| connected\_rear\_km | double | Sum of `rear_weighted_connected_length` across all edges, in km. |
| disconnected\_rear\_km | double | Sum of `rear_weighted_disconnected_length` across all edges, in km. |
| connected\_spawnrear\_km | double | Sum, per edge, of the lesser of its spawn/rear weighted connected length, in km. |
| disconnected\_spawnrear\_km | double | Sum, per edge, of the lesser of its spawn/rear weighted disconnected length, in km. |
| pct\_disconnected\_spawn | numeric | `disconnected_spawn_km / total_spawn_km`, rounded to 2 decimals. NULL if total\_spawn\_km is 0. |
| pct\_disconnected\_rear | numeric | `disconnected_rear_km / total_rear_km`, rounded to 2 decimals. NULL if total\_rear\_km is 0. |
| pct\_disconnected\_spawnrear | numeric | `disconnected_spawnrear_km / total_spawnrear_km`, rounded to 2 decimals. NULL if total\_spawnrear\_km is 0. |

A species with no entries in `reporting_species_lifecycles` produces no row. If no species are configured at all, the view is skipped entirely (a warning is logged).
