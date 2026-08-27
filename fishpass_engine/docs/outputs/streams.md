# Stream Network Outputs

### `<output_schema>.streams`


| Field Name | Values | Description |
| :---- | :---- | :---- |
| id | uuid | Unique identifier. We will re-use chyf identifiers in some places, but these may be split and new identifiers created when necessary |
| aoi\_id | uuid |  |
| ef\_type | int | Flowpath type |
| ef\_subtype | int | Flowpath subtype |
| rank | int | Rank (primary/secondary) |
| length | double | Raw 2d length of flowpath |
| from\_nexus\_id | uuid |  |
| to\_nexus\_id | uuid |  |
| ecatchment\_id | uuid | waterbody/catchment edge is contained within |
| mainstem\_id | uuid | Mainstem identifier |
| graph\_id | int | Unique id that identifies connected components |
| strahler\_order | int |  |
| downstream\_route\_measure | double precision |  |
| upstream\_route\_measure | double precision |  |
| segment\_gradient | double precision |  |
| effective\_length | double | The raw length for edges, except for the non-core mainstem in flowpaths. This removes the length of skeleton connectors from the analysis. |
| species\_statistics** | jsonb | This is a jsonb column containing all species statistics. We will use views to convert these into user-friendly columns.  Each field is described in the next table. |
| geometry | LineStringZM |  |

### **species_statistics
This column contains the following fields for each species identifies in the model parameters.


| Field Name | Type | Description |
| :---- | :---- | :---- |
| spawn\_weighted\_length  | double | See "Compute Weighted Length for Stream Edges" section below for how this is computed.  For accessible, habitat this is the effective length \* the stream order weighting \* spawn passability status of the first non-passable downstream anthropogenic barrier. |
| rear\_weighted\_length  | double | Same as spawn weighted length but using rear attributes. |
| upstream\_anthro\_spawn\_count  | int | Number of spawning impassable anthropogenic barriers upstream of the stream segment edge |
| upstream\_anthro\_rear\_count  | int | Number of rearing impassable anthropogenic barriers upstream of the stream segment edge |
| upstream\_anthro\_spawnrear\_count  | int | The number of impassable rearing or spawning anthrogopenic barriers upstream of the stream segment. Barriers are counted only once (no double counting) |
| downstream\_anthro\_spawn\_count  | int | Number of spawning impassable anthropogenic barriers downstream of the stream segment edge |
| downstream\_anthro\_rear\_count  | int | Number of rearing impassable anthropogenic barriers downstream  of the stream segment edge |
| downstream\_anthro\_spawnrear\_count  | int | The number of impassable rearing or spawning anthrogopenic barriers downstream of the stream segment. Barriers are counted only once (no double counting) | |
| upstream\_natural\_spawn\_count  | int | Number of spawning impassable natural barriers upstream of the stream segment edge |
| upstream\_natural\_rear\_count  | int | Number of rearing impassable natural barriers upstream of the stream segment edge |
| upstream\_natural\_spawnrear\_count  | int  | The number of impassable rearing or spawning nature barriers upstream of the stream segment. Barriers are counted only once (no double counting) | 
| downstream\_natural\_spawn\_count  | int | Number of spawning impassable natural barriers downstream of the stream segment edge |
| downstream\_natural\_rear\_count  | int | Number of rearing impassable natural barriers downstream  of the stream segment edge |
| downstream\_natural\_spawnrear\_count  | int | The number of impassable rearing or spawning natural barriers downstream of the stream segment. Barriers are counted only once (no double counting) |
| downstream\_anthro\_spawn\_ids  | uuid\[\] | The barrier ids of anthropogenic structures that are downstream and spawn impassable for the given species. |
| downstream\_anthro\_rear\_ids  | uuid\[\] | The barrier ids of anthropogenic structures that are downstream and rear impassable for the given species. |
| upstream\_anthro\_spawn\_ids  | uuid\[\] | The barrier ids of anthropogenic structures that are upstream and spawn impassable for the given species. |
| upstream\_anthro\_rear\_ids  | uuid\[\] | The barrier ids of anthropogenic structures that are upstream and rear impassable for the given species. |
| spawn\_accessibility | naturally\_accessible or naturally\_inaccessible | Indicates if the stream edge is naturally accessible or naturally inaccessible to target species and spawning lifestage. |
| rear\_accessibility | naturally\_accessible or naturally\_inaccessible | Indicates if the stream edge is naturally accessible or naturally inaccessible to target species and rearing lifestage. |
| spawn\_habitat  | boolean | Indicates if the stream edge is suitable spawning habitat for target species. |
| rear\_habitat  | boolean | Indicates if the stream edge is suitable rearing habitat for target species. |
| spawnrear\_habitat  | boolean | True if one of spawn or rear is true. |
| | |


### Views: `<output_schema>.streams_<species>`

Each output species will have a separate view with each of the statistics fields computed available as a column in the view.