# CABD Feature Tables

**Table:** `<output_schema>.cabd_<feature_type>`

Cached raw values of the barrier as imported from CABD, before any structure_updates, new_structures, gradient, classification, or snapping logic is applied -- an immutable record of the source data, distinct from `all_barriers` which holds the working modelled values used throughout the rest of the pipeline. For each <feature_type> included in the plan (excluding `gradients`).

Table Structure:
| Field | Type | Comment |
| :---- | :---- | :---- |
| cabd_id | uuid | cabd_id for the feature; primary key |
| species_passability_value | jsonb | as computed in Load Structures step 2, before any later updates |
| passability_status_code | integer | raw passability_status_code as returned by CABD, unmapped |
| geometry | point | original location of barrier, as returned by CABD |

No `source` column here -- every row in this table came from CABD by construction. The table is  scoped to a single feature type via its `<feature_type>`-suffixed name.