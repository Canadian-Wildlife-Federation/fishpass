# Cached Gradient Barriers

Cached version of gradient barriers containing only barriers used in this analysis, with their snapped location. Unlike `cabd_<feature_type>`, this is not an immutable pre-processing copy of the source table -- it is created and populated during processing to include the `snapped_geometry` and the species_passability_value`. Only created when the plan's `structure_types` list contains `gradients`.

Table Structure:

| Field | Type | Comment |
| :---- | :---- | :---- |
| id | uuid | system generated primary key, copied from `all_barriers.id` |
| feature_id | uuid | id of the source row in the gradient barriers table |
| species_passability_value | jsonb | populated from the `actual_species` value from the source table for each lifecycle |
| geometry | point | original location of barrier |
| snapped_geometry | point (4617) | point snapped to the chyf stream network |
