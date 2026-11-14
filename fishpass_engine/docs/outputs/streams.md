# Stream Network Outputs

## `<output_schema>.streams`

| Field Name | Values | Description |
| :---- | :---- | :---- |
| id | uuid | Unique identifier. We will re-use chyf identifiers in some places, but these may be split and new identifiers created when necessary |
| aoi\_id | uuid | |
| ef\_type | int | Flowpath type |
| ef\_subtype | int | Flowpath subtype |
| rank | int | Rank (primary/secondary) |
| length | double | Length (m) of the edge, computed with PostGIS `ST_Length(geometry::geography)` from its final geometry after network breaking. Every edge is recomputed this way, split or not, so this replaces the CHyF flowpath length. |
| rivernameid1 | uuid | CHyF primary river name identifier (`chyf_raw.flowpath.rivernameid1`). |
| stream\_name\_en | text | English name for `rivernameid1`, from `chyf_raw.names`. NULL when the edge has no name. Segments created by network breaking keep the name of the edge they were split from. |
| from\_nexus\_id | uuid | |
| to\_nexus\_id | uuid | |
| ecatchment\_id | uuid | waterbody/catchment edge is contained within |
| mainstem\_id | uuid | Mainstem identifier |
| graph\_id | int | Unique id that identifies connected components |
| strahler\_order | int | |
| downstream\_route\_measure | double precision | |
| upstream\_route\_measure | double precision | |
| segment\_gradient | double precision | |
| effective\_length | double | The raw length for edges, except for the non-core mainstem in flowpaths. This removes the length of skeleton connectors from the analysis. |
| is\_sink\_barrier\_edge | boolean | True for a zero-length "sink edge" that network breaking adds below an outlet edge when a barrier sits on that outlet edge's very last vertex. The barrier is placed at the start of the sink edge, like every other barrier, so the outlet edge counts as upstream of it. Sink edges are only in this table: the `streams_<species>` views, the stream exports and `watershed_summary_stats` leave them out. False for every other edge. |
| species\_statistics** | jsonb | This is a jsonb column containing all species statistics. We will use views to convert these into user-friendly columns.  Each field is described in the next table. |
| geometry | LineStringZM | |

## **species_statistics

This column contains the following fields for each species identifies in the model parameters.

The `*_ids` fields below contain barrier `feature_id` values, not the per-run `all_barriers.id`.

| Field Name | Type | Description |
| :---- | :---- | :---- |
| spawn\_weighted\_length | double | For accessible, spawning habitat this is the effective length \* the spawn stream order weighting. See "Stream Length Attributes" in fishpass_docs.md for more details. |
| rear\_weighted\_length | double | Same as spawn weighted length but using rear attributes. |
| spawn\_weighted\_connected\_length | double | spawn\_weighted\_length \* the spawn passability status of the first non-passable downstream anthropogenic barrier. |
| rear\_weighted\_connected\_length | double | Same as spawn weighted connected length but using rear attributes. |
| spawn\_weighted\_disconnected\_length | double | spawn\_weighted\_length \* (1 \- the spawn passability status of the first non-passable downstream anthropogenic barrier). |
| rear\_weighted\_disconnected\_length | double | Same as spawn weighted disconnected length but using rear attributes. |
| upstream\_anthro\_spawn\_count | int | Number of spawning impassable anthropogenic barriers upstream of the stream segment edge |
| upstream\_anthro\_rear\_count | int | Number of rearing impassable anthropogenic barriers upstream of the stream segment edge |
| upstream\_anthro\_spawnrear\_count | int | The number of impassable rearing or spawning anthrogopenic barriers upstream of the stream segment. Barriers are counted only once (no double counting) |
| downstream\_anthro\_spawn\_count | int | Number of spawning impassable anthropogenic barriers downstream of the stream segment edge |
| downstream\_anthro\_rear\_count | int | Number of rearing impassable anthropogenic barriers downstream  of the stream segment edge |
| downstream\_anthro\_spawnrear\_count | int | The number of impassable rearing or spawning anthrogopenic barriers downstream of the stream segment. Barriers are counted only once (no double counting) |
| upstream\_natural\_spawn\_count | int | Number of spawning impassable natural barriers upstream of the stream segment edge |
| upstream\_natural\_rear\_count | int | Number of rearing impassable natural barriers upstream of the stream segment edge |
| upstream\_natural\_spawnrear\_count | int | The number of impassable rearing or spawning nature barriers upstream of the stream segment. Barriers are counted only once (no double counting) |
| downstream\_natural\_spawn\_count | int | Number of spawning impassable natural barriers downstream of the stream segment edge |
| downstream\_natural\_rear\_count | int | Number of rearing impassable natural barriers downstream  of the stream segment edge |
| downstream\_natural\_spawnrear\_count | int | The number of impassable rearing or spawning natural barriers downstream of the stream segment. Barriers are counted only once (no double counting) |
| downstream\_anthro\_spawn\_ids | uuid\[\] | The barrier ids of anthropogenic structures that are downstream and spawn impassable for the given species. |
| downstream\_anthro\_rear\_ids | uuid\[\] | The barrier ids of anthropogenic structures that are downstream and rear impassable for the given species. |
| upstream\_anthro\_spawn\_ids | uuid\[\] | The barrier ids of anthropogenic structures that are upstream and spawn impassable for the given species. |
| upstream\_anthro\_rear\_ids | uuid\[\] | The barrier ids of anthropogenic structures that are upstream and rear impassable for the given species. |
| spawn\_accessibility | naturally\_accessible or naturally\_inaccessible | Indicates if the stream edge is naturally accessible or naturally inaccessible to target species and spawning lifestage. |
| rear\_accessibility | naturally\_accessible or naturally\_inaccessible | Indicates if the stream edge is naturally accessible or naturally inaccessible to target species and rearing lifestage. |
| spawn\_habitat | boolean | Indicates if the stream edge is suitable spawning habitat for target species. |
| rear\_habitat | boolean | Indicates if the stream edge is suitable rearing habitat for target species. |
| spawnrear\_habitat | boolean | True if one of spawn or rear is true. |
| | | |

### Views: `<output_schema>.streams_<species>`

Each output species will have a separate view with each of the statistics fields computed available as a column in the view, alongside `id`, `geometry`, `length`, `strahler_order`, `effective_length`, `segment_gradient` and `stream_name_en` from `streams`.
