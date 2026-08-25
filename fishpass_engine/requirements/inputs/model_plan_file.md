# Model Plan Parameter File

These YAML files describe all the parameters for a model run.

**Source:** GitHub

Any number of files can exist. These are stored in GitHub at `config/models/<plan_code>.yaml`.

| Field Name | Description |
| :---- | :---- |
| code | Required. Unique identifier code for the plan. Used to run the model, and to determine which structure updates, new structures, and habitat updates apply. MUST match the `update_scope`/`scope` value in those tables. |
| name | Optional. User-friendly name. Not used by the system. |
| output_schema | Required. The name of the output schema where all outputs are written to. |
| aoi | Required. See [AOI Options](#aoi-options) below. |
| target_species | Required. List of species codes of interest (must match a code in the fish species parameter file). Example: `as` |
| reporting_values | Required. List of species/lifecycle combinations required in outputs, encoded as `<species>_<lifecycle>`. Valid lifecycles: `rear`, `spawn`, `spawnrear`. `all` can be used in either position to mean all target species, or all lifecycles. Examples: `as_rear`, `as_spawn`, `as_spawnrear`, `all_rear`, `all_spawn`, `all_spawnrear`, `all_all` |
| structure_types | Required. List of structure types to include (`dams`, `waterfalls`, `stream_crossings`, `beaver_dams`, etc.). Pulled from the CABD API and the new_structures table. |
| structure_snap_edge_distance_m | Optional. Maximum distance used to find the closest stream edge to a structure point (CABD or new structure). Default: 100m. CABD points are expected to already be snapped; this provides a buffer in case they aren't, and is also used for new structures, which may not be snapped. |
| structure_snap_vertex_distance_m | Optional. When snapping a structure point, if an existing vertex is within this distance of the closest point along the edge, that vertex is used; otherwise a new vertex is inserted into the network. Default: 50m. |
| habitat_point_snap_edge_distance_m | Optional. Maximum distance used to find the closest stream edge to a habitat update point. (If edge ids are provided, the edge must also be within this distance of the point provided.) Default: 100m. |
| habitat_point_snap_vertex_distance_m | Optional. When snapping a habitat point, if an existing vertex is within this distance of the closest point along the edge, that vertex is used; otherwise a new vertex is inserted into the network. Default: 50m. |
| structure_update_table | Optional. Defaults to `support.structure_updates`. |
| structure_new_table | Optional. Defaults to `support.new_structures`. |
| habitat_update_table | Optional. Defaults to `support.habitat_updates`. |
| gradient_barriers_table | Optional. Defaults to `support.gradient_barriers`. |
| update_scope | Optional. Defaults to the plan's `code` value. Used to filter structure and habitat updates. |
| impassable_threshold | Optional. Default: `1.0`. A structure/barrier is treated as impassable for a given species/lifestage if its `passability_status_value` is less than this threshold. Default means anything short of fully passable (`1.0`) -- including a fractional partial-passability value like `0.25` -- counts as impassable. Used in Compute Statistics step 5. |
| natural_feature_types_override | Optional. List of `feature_type` values to classify as `natural` for this plan only. When present, it entirely replaces (not merges with) `config/fishpass.yaml`'s `structure_classification.natural_feature_types` list for this run; when omitted, that config file's list is used. **Any `feature_type` not in the effective list -- whether from this override or from `config/fishpass.yaml` -- is classified as `anthropogenic`.** This is a deliberate fail-safe default: an empty list (`[]`) or a typo'd/unrecognized feature type both fall back to `anthropogenic`. See Classify Structures in requirements.md and [config/fishpass.yaml](../../../config/fishpass.yaml). |

## AOI Options

The `aoi` field supports exactly one of the following (only one of `workunit`, `province`, or `upstream_of` per plan):

**workunit** — run on the specified work unit(s) and anything connected to them. Use `all` to run on all predefined 217 work units.

```yaml
aoi:
  workunit:
    - 03EBA001
    - 03EBA002
```

**province** — run on a specific province/territory.

```yaml
aoi:
  province:
    - ns
```

**upstream_of** — run on the network upstream of one or more CHyF stream edge ids. If a given edge id doesn't exist, the software will abort. *(Not yet supported — see requirements.md.)*

```yaml
aoi:
  upstream_of:
    - 12345678-1234-1234-1234-123456789012
```
