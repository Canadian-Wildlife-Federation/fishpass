# New Structures Dataset

The purpose of this table is to support new structure types not in the CABD (e.g.: barrier beaches, beaver dams). These are generally only included in WCRP reporting.

Updates to these structures occur in the `support.structure_updates` table (and can be different for different plans).

**Source:** FishPass Database

**Table:** support.new_structures

| Column | Type | Description |
| :---- | :---- | :---- |
| new_structure_id | uuid | Unique system identifier |
| feature_type | varchar | Feature type as free-form text. It is up to users to ensure there are no typos or other errors. |
| update_scope | array[varchar] | Values: 'all', or any specific plan code (e.g.: cheticamp_wcrp). Determines which model runs include this structure: the run will include new structures where `update_scope` = 'all' or `update_scope` contains the plan code. Only structures which can be attached to the stream network for the AOI will be used in the analysis. |
| passability_status_rear | jsonb | Rearing passability status per species as a JSON string: `{"es": 0.25, "wl": 1}`. If this is null, or the specific species doesn't exist in the JSON string, the structure is assumed to be a full barrier (impassable) for that species. |
| passability_status_spawn | jsonb | Spawning passability status per species as a JSON string, same format and fallback rule as `passability_status_rear`. |
| point | geometry (point) | Represents the location (lat/long) of the new structure. |
| source | varchar | Source of the structure — e.g., a particular organization, workshop, assessment, or data source. |
| notes | varchar | Field for adding notes about this structure. |
