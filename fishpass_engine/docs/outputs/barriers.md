# Barrier Outputs

### `<output_schema>.all_barriers`


All structures used in the analysis, including those loaded from the CABD database, the gardient barriers table, and the new structures table. This table will contain all the updates applied to the structures and used in the modelling.  


| Field Name | Values | Description |
| :---- | :---- | :---- |
| id | uuid | Unique system defined identifier. |
| feature\_id | uuid | Identifier of the source feature (cabd\_id, gradient barrier id or new structure id) |
| feature\_type | varchar | Barrier feature type. |
| source | varchar | Source of barrier (cabd, gradient, new structure) |
| structure\_type | varchar | Natural vs anthropogenic |
| geometry | Point | Raw geometry |
| snapped\_geometry | Point | Geometry snapped to stream network |
| upstream\_edge\_id | uuid | The upstream edge id from the streams table. |
| downstream\_edge\_id | uuid  | The downstream edge id from the stream table. |
| species\_passability\_value  | jsonb | Passability values for each species, lifestage of this barriers.  This should be populated for each species,lifestage the user is reporting on. |
| species\_statistics** | jsonb | This is a jsonb column containing all species statistics. We will use views to convert these into user-friendly columns. See table below for complete description. |

### **species_statistics
This column contains the following fields for each species identifies in the model parameters.

| Field Name | Type | Description |
| :---- | :---- | :---- |
| upstream\_anthro\_spawn\_count  | int | Number of spawning impassable anthropogenic barriers upstream of the stream segment edge |
| upstream\_anthro\_rear\_count  | int | Number of rearing impassable anthropogenic barriers upstream of the stream segment edge |
| upstream\_anthro\_spawnrear\_count  |  |  |
| downstream\_anthro\_spawn\_count  | int | Number of spawning impassable anthropogenic barriers downstream of the stream segment edge |
| downstream\_anthro\_rear\_count  | int | Number of rearing impassable anthropogenic barriers downstream  of the stream segment edge |
| downstream\_anthro\_spawnrear\_count  |  |  |
| upstream\_natural\_spawn\_count  | int | Number of spawning impassable natural barriers upstream of the stream segment edge |
| upstream\_natural\_rear\_count  | int | Number of rearing impassable natural barriers upstream of the stream segment edge |
| upstream\_natural\_spawnrear\_count  | int |  |
| downstream\_natural\_spawn\_count  | int | Number of spawning impassable natural barriers downstream of the stream segment edge |
| downstream\_natural\_rear\_count  | int | Number of rearing impassable natural barriers downstream  of the stream segment edge |
| downstream\_natural\_spawnrear\_count  | int |  |
| upstream\_anthro\_spawn\_ids  | uuid\[\] | The barrier ids of anthropogenic structures that are upstream and spawn impassable for the given species. |
| upstream\_anthro\_rear\_ids  | uuid\[\] | The barrier ids of anthropogenic structures that are upstream and rear impassable for the given species. |
| downstream\_anthro\_spawn\_ids  | uuid\[\] | The barrier ids of anthropogenic structures that are downstream and spawn impassable for the given species. |
| downstream\_anthro\_rear\_ids  | uuid\[\] | The barrier ids of anthropogenic structures that are downstream and rear impassable for the given species. |
| downstream\_natural\_spawn\_ids  | uuid\[\] | The barrier ids of natural structures that are downstream and spawn impassable for the given species. |
| downstream\_natural\_rear\_ids  | uuid\[\] | The barrier ids of natural structures that are downstream and rear impassable for the given species. |
| spawn\_upstream\_accessible\_length  | double | The sum of the effective length of any edges upstream flagged as naturally accessible for spawning |
| rear\_upstream\_accessible\_length  | double | The sum of the effective length of any edges upstream flagged as naturally accessible for spawning. |
| spawn\_upstream\_length  | double | The sum of the effective length of any upstream edges flagged as spawning habitat. |
| rear\_upstream\_length  | double | The sum of the effective length of any upstream edges flagged as rearing habitat. |
 |spawnrear_upstream_length | double | The sum of the effective length of any upstream edges flagged as spawning or rearing habitat. Each edge is only counted once. |
| spawn\_functional\_upstream\_length  | double | The sum of all the effective length of all upstream edges flagged as spawning habitat  stopping at the first impassible anthropogenic barrier. |
| rear\_functional\_upstream\_length  | double | The sum of all the effective length of all upstream edges flagged as rearing habitat stopping at the first impassible anthropogenic barrier. |
| spawnrear\_functional\_upstream\_length  | double | The sum of the maximum (for each edge) of the rear functional length or spawn functional length for all upstream edges stopping at the first impassible anthropogenic barrier. |
| spawn\_weighted\_upstream\_length  | double | The sum of the spawn weighted length for all upstream edges. |
| rear\_weighted\_upstream\_length  | double | The sum of the rear weighted length for all upstream edges. |
| spawnrear\_weighted\_upstream\_length  | double | The sum of the maximum (for each edge) of the rear weighted length or spawn weighted length for all upstream edges |
| spawn\_functional\_weighted\_upstream\_length  | double | The sum of all the  spawn weighted length of all upstream edges stopping at the first impassible anthropogenic barrier. |
| rear\_functional\_weighted\_upstream\_length  | double | The sum of all the  rear weighted length of all upstream edges stopping at the first impassible anthropogenic barrier. |
| spawnrear\_functional\_weighted\_upstream\_length  | double | The sum of the maximum (for each edge) of the rear weighted functional length or spawn weighted functional length for all upstream edges stopped at the first impassible anthropogenic barrier. |



### Views: `<output_schema>.anthropogenic_barriers_<species>` and `<output_schema>.natural_barriers_<species>`

Each output species there will be an anthropogenic_barriers and natural_barriers view that includes each of the statistics fields computed available as a column.


### Views: `<output_schema>.unsnapped_barriers`

This table contains a list of all the barriers that could not be snapped to the stream network.
