# Fish Species Parameter File

This file describes the parameters for individual fish species. Historically this has been a csv file, for this tool this will be a yaml file.

| Field | Description |
| :---- | :---- |
| code | A two-letter code used to refer to the species  (e.g., as \= Atlantic Salmon; ae=American Eel; chn=Chinook Salmon) |
| name | Common name of the species |
| accessibility\_gradient\_spawning\_max | This is used for computing gradient barriers. If left blank, spawning gradient barriers are never flagged for this species. |
| accessibility\_gradient\_rearing\_max | This is used for computing gradient barriers. If left blank, rearing gradient barriers are never flagged for this species. |
| fall\_height\_threshold | The height of natural barriers (waterfalls) in meters at which a given species can't move upstream (i.e. becomes impassible) |
| spawn\_gradient\_min | Minimum stream gradient for suitable spawning habitat for the species.  |
| spawn\_gradient\_max | Maximum stream gradient for suitable spawning habitat for the species.  |
| rear\_gradient\_min | Minimum stream gradient for suitable rearing habitat for the species. |
| rear\_gradient\_max | Maximum stream gradient for suitable rearing habitat for the species. |
| spawn\_discharge\_min | Minimum stream discharge for suitable spawning habitat for the species. Only populated for where discharge data is available for streams |
| spawn\_discharge\_max | Maximum stream discharge for suitable spawning habitat for the species. Only populated for where discharge data is available for streams |
| rear\_discharge\_min | Minimum stream discharge for suitable rearing habitat for the species. Only populated for where discharge data is available for streams |
| rear\_discharge\_max | Maximum stream discharge for suitable rearing habitat for the species. Only populated for where discharge data is available for streams |
| spawn\_channel\_confinement\_min | Minimum channel confinement for suitable spawning habitat for the species. Only populated for where channel confinement data is available for streams  |
| spawn\_channel\_confinement\_max | Maximum channel confinement for suitable spawning habitat for the species. Only populated for where channel confinement data is available for streams  |
| rear\_channel\_confinement\_min | Minimum channel confinement for suitable rearing habitat for the species. Only populated for where channel confinement data is available for streams |
| rear\_channel\_confinement\_max | Maximum channel confinement for suitable rearing habitat for the species. Only populated for where channel confinement data is available for streams  |
| strahler\_order\_spawning\_min | Used when assigning habitat. If there is no habitat for this species/lifecycle then set the max \< min. |
| strahler\_order\_spawning\_max | Used when assigning habitat. If there is no habitat for this species/lifecycle then set the max \< min. |
| strahler\_order\_rearing\_min | Used when assigning habitat. If there is no habitat for this species/lifecycle then set the max \< min. |
| strahler\_order\_rearing\_max | Used when assigning habitat. If there is no habitat for this species/lifecycle then set the max \< min. |
| stream\_order\_1\_weight | Downweighting value applied to habitat length calculations on first order streams. |
| stream\_order\_2\_weight | Downweighting value applied to habitat length calculations on second order streams. |

**Example:**

```
# Example habitat suitability parameters
species:
  - code: chn
    name: Chinook Salmon
    accessibility_gradient_spawning_max: 4.0
    accessibility_gradient_rearing_max: 6.0
    fall_height_threshold: 1.5
    spawn_gradient_min: 0.0
    spawn_gradient_max: 3.0
    rear_gradient_min: 0.0
    rear_gradient_max: 5.0
    spawn_discharge_min: 10.0
    spawn_discharge_max: 500.0
    rear_discharge_min: 5.0
    rear_discharge_max: 1000.0
    spawn_channel_confinement_min: 1.0
    spawn_channel_confinement_max: 3.0
    rear_channel_confinement_min: 1.0
    rear_channel_confinement_max: 4.0
    strahler_order_spawning_min: 2
    strahler_order_spawning_max: 5
    strahler_order_rearing_min: 1
    strahler_order_rearing_max: 6
    stream_order_1_weight: 0.2
    stream_order_2_weight: 0.5

  - code: sth
    name: Steelhead Trout
    accessibility_gradient_spawning_max: 6.0
    accessibility_gradient_rearing_max: 8.0
    fall_height_threshold: 2.0
    spawn_gradient_min: 0.0
    spawn_gradient_max: 4.0
    rear_gradient_min: 0.0
    rear_gradient_max: 6.0
    spawn_discharge_min: 5.0
    spawn_discharge_max: 300.0
    rear_discharge_min: 2.0
    rear_discharge_max: 800.0
    spawn_channel_confinement_min: 1.0
    spawn_channel_confinement_max: 4.0
    rear_channel_confinement_min: 1.0
    rear_channel_confinement_max: 5.0
    strahler_order_spawning_min: 1
    strahler_order_spawning_max: 4
    strahler_order_rearing_min: 1
    strahler_order_rearing_max: 5
    stream_order_1_weight: 0.3
    stream_order_2_weight: 0.6
```

x