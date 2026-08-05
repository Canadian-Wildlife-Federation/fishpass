# FishPass - Gradient Barrier Generator
Using the 4d CHyF network and fish pass parameters files, this will compute gradient barriers per species and lifestage.

This will be a python script that can be run via a github action.

## Inputs
* Fishpass cached CHyF network with elevation and smoothed elevation.
   * This will run over the entire CHyF network loaded in the fishpass database. 
   * This network will be loaded from the chyf_raw.flowpath table.  These are all 4-d geometries, with the smoothed elevation being stored in the 4th dimension.

* Fish Species and parameters file from GitHub.
  * This file identifies the gradient threshold for each species and life stage.
  * Further details of this file are described in the inputs for the FishPass Process
  * documented in docs/fish_species_parameter_file.md

## Outputs

* Database table: support.gradient_barriers

  * Any existing table will be renamed support.gradient_barriers_archive_<yyyymmdd>_<seq>, to prevent losing manual updates.

* Gradient barriers will be computed at all locations where the gradient is greater than the maximum gradient defined in the fish species parameter files. 
  * We will use the accessibility_gradient_spawning_max and accessibility_gradient_rearing_max attributes.
  * Each barrier will be flagged with the species and lifecycles (spawn,rear) they are barriers for. A single point can be a barrier for any number of species/lifecycles.


### Table: support.gradient_barriers
| Column           | Type      | Description                                                                                                                                                                                                                                                                       | 
| ---------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| id               | uuid      | Unique system generated id                                                                                                                                                                                                                                                        |
| geometry         | point     | Location of barrier.                                                                                                                                                                                                                                                              |
| workunit         | varchar[] | The workunit associated with the barrier (if on the boundary of work units, record both).                                                                                                                                                                                         |
| gradient         | double    | Computed gradient value.                                                                                                                                                                                                                                                          |
| computed_species | string[]  | List of fish species/lifestage that this barrier applies to. These will be encoded as <species>_<lifestage> (ex. as_rear, as_spawn).                                                                                                                                              |
| actual_species   | string[]  | Used for modelling, this is a copy of the computed_species but allows users to overwrite.  Further details are in the FishPass Process input description. For the purposes of the barrier computation, this will always be populated with the same value as the computed_species. |
| comments         | varchar   | Placeholder for user comments.                                                                                                                                                                                                                                                    |

	
## Process
Gradients will be computed at each vertex along the stream network. The smoothed elevation at that point and a point on the stream network 100m upstream on the same mainstem will be used to determine gradient.

Stream vertices with no upstream point will not have a gradient.

* The smoothed elevation is the 4th dimenion value at the point.
* The mainsteam attribute is mainstem_id.
* The mainstem_seq attribute orders the edges along the mainstem with 1 representing the most downstream edge and values increasing as you walk up the mainstem.
* The workunit should record the short_name from the chyf_raw.aoi table (linked to the chyf_raw.flowpath table via the aoi_id )
* For the purposes of this script, the actual_species output field should be the same as the computed_species.

## Design Decisions

The following decisions resolve ambiguities in the requirements above and were confirmed with the
project owner during implementation planning:

* **Smoothed elevation ordinate**: the smoothed elevation is stored in the **M ordinate** of the
  4D flowpath geometries (XYZM/XYM).
* **Vertex definition**: a "vertex" is every coordinate vertex in each flowpath LineString
  geometry, not just edge endpoints.
* **Edge digitization direction**: every flowpath LineString is digitized consistently
  network-wide, starting at its **upstream** end and ending at its **downstream** end.
  `mainstem_seq = 1` is the most downstream edge on a mainstem, with values increasing upstream.
* **100m upstream walk**: if a vertex doesn't have 100m of upstream length remaining within its
  own edge, the walk continues into the next edge upstream on the same `mainstem_id` (ordered by
  ascending `mainstem_seq`). If the mainstem runs out (reaches its most-upstream edge) before
  accumulating 100m, that vertex has no gradient, same as a vertex with no upstream point at all.
* **Elevation at the 100m mark is linearly interpolated, not read from the nearest vertex**: the
  upstream reference elevation is computed by linearly interpolating between the two vertices whose
  segment contains the point exactly 100m upstream, rather than using the elevation of whichever
  vertex happens to be first at or beyond 100m. Using the nearest vertex instead of interpolating
  would average the gradient over whatever the actual distance to that vertex turns out to be
  (which, in a sparse-vertex reach, could be much more than 100m), diluting real local steepness.
  For example: vertex i at 0m/elevation 0, vertex A at 50m/elevation 5 (10% grade), vertex B at
  150m/elevation 5.5 (0.5% grade for the 100m beyond A) — using nearest-vertex B directly gives
  (5.5-0)/150 ≈ 3.7%, while interpolating the elevation at exactly 100m between A and B gives
  5 + (100-50)/(150-50)*(5.5-5) = 5.25, i.e. (5.25-0)/100 = 5.25%, which matches the actual slope of
  the A→B segment at the 100m mark instead of an average diluted by the extra distance to B. Since a
  LineString's vertices are already connected by straight segments, interpolating within the single
  segment containing the 100m mark isn't a new assumption — it uses the same piecewise-linear model
  the geometry already encodes, just evaluated at the right location instead of at the next
  available vertex.
* **Where computation runs**: the FishPass Postgres server is a shared/production resource, so the
  100m-walk and gradient computation is done in Python (not as large SQL window-function/LATERAL
  queries in Postgres) to avoid loading the database server. SQL is used only to extract raw edge
  geometries and to perform final table/workunit bookkeeping. Geometry parsing (including the M
  ordinate) uses `shapely>=2.1` with `GEOS>=3.12`, which is required for reliable M-ordinate
  support.
* **Workunit assignment**: computed as a post-processing spatial join step (point vs. `chyf_raw.aoi`
  polygons) after barrier points are inserted, rather than trusting a single flowpath edge's
  `aoi_id` — this correctly captures points that fall on/near a boundary between workunits.
* **Table lifecycle**: the script owns the full lifecycle of `support.gradient_barriers` each run —
  it creates the `support` schema if missing, archives any existing table to
  `support.gradient_barriers_archive_<yyyymmdd>_<seq>` (incrementing `seq` if today's archive name
  is already taken), then creates a fresh table. There is no separate one-time init script for this
  table.
* **Fish species parameter file location**: `config/fish_species_parameters.yaml` in this repo,
  read directly from the checked-out repo at runtime (no external fetch).
* **GitHub Action trigger**: `workflow_dispatch` only (manual), matching the `chyf_loader` reload
  action.
* **Database connection**: reuses the same `FISHPASS_HOST/PORT/DBNAME/USER/PASSWORD` environment
  variable / GitHub secrets convention as `chyf_loader` — connection details are never stored in
  config files and never logged.
