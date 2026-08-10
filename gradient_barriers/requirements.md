# FishPass - Gradient Barrier Generator
Using the 4d CHyF network and fish pass parameters files, this will compute gradient barriers per species and lifestage.

This will be a python script.

## Running

This is run via a GitHub action, triggered by `workflow_dispatch` (manual), matching the `chyf_loader` reload action.

## Inputs
* Fishpass cached CHyF network with elevation and smoothed elevation.
   * This will run over the entire CHyF network loaded in the fishpass database. 
   * This network will be loaded from the chyf_raw.flowpath table.  These are all 4-d geometries, with the smoothed elevation being stored in the 4th dimension, the **M ordinate** (XYZM/XYM).
   * Every flowpath LineString is digitized consistently network-wide, starting at its **upstream** end and ending at its **downstream** end. The mainstem_seq attribute orders the edges along the mainstem with 1 representing the most downstream edge and values increasing as you walk up the mainstem.

* Fish species parameter file from GitHub.
  * This file identifies the gradient threshold for each species and life stage.
  * Complete details of this file are documented in docs/fish_species_parameter_file.md
  * Located at `config/fish_species_parameters.yaml` in this repo, read directly from the checked-out repo at runtime (no external fetch).

* Database Connection
    * The database connection reuses the same `FISHPASS_HOST/PORT/DBNAME/USER/PASSWORD` environment variable / GitHub secrets convention as `chyf_loader` — connection details are never stored in config files and never logged.

## Outputs

* Database table: support.gradient_barriers

  * Any existing table will be renamed support.gradient_barriers_archive_<yyyymmdd>_<seq>, to prevent losing manual updates. The `seq` is incremented if today's archive name is already taken.
  * The script owns the full lifecycle of this table each run: it creates the `support` schema if missing, archives any existing table as above, then creates a fresh table. There is no separate one-time init script for this table.

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
Gradients will be computed at each vertex along the stream network — every coordinate vertex in each flowpath LineString geometry, not just edge endpoints. The smoothed elevation at that point and a point on the stream network 100m upstream on the same mainstem will be used to determine gradient.

If a vertex doesn't have 100m of upstream length remaining within its own edge, the walk continues into the next edge upstream on the same `mainstem_id` (ordered by ascending `mainstem_seq`). If the mainstem runs out (reaches its most-upstream edge) before accumulating 100m, that vertex has no gradient, same as a vertex with no upstream point at all.

The upstream reference elevation at the 100m mark is linearly interpolated between the two vertices whose segment contains the point exactly 100m upstream, rather than using the elevation of whichever vertex happens to be first at or beyond 100m — using the nearest vertex instead would average the gradient over whatever the actual distance to that vertex turns out to be, diluting real local steepness in sparse-vertex reaches.

Stream vertices with no upstream point will not have a gradient.

For the purposes of this script, the actual_species output field should be the same as the computed_species.

The workunit should record the short_name from the chyf_raw.aoi table (linked to the chyf_raw.flowpath table via the aoi_id), assigned as a post-processing spatial join (point vs. `chyf_raw.aoi` polygons) after barrier points are inserted, rather than trusting a single flowpath edge's `aoi_id` — this correctly captures points that fall on/near a boundary between workunits.

## Architectural Decisions

* Since the FishPass Postgres server is a shared/production resource, the 100m-walk and gradient computation is done in Python (not as large SQL window-function/LATERAL queries in Postgres) to avoid loading the database server. SQL is used only to extract raw edge geometries and to perform final table/workunit bookkeeping. Geometry parsing (including the M ordinate) uses `shapely>=2.1` with `GEOS>=3.12`, which is required for reliable M-ordinate support.

## Design Decision


### Volume of Gradient Barriers
In steeper terrain this process will generate a gradient barrier at every qualifying vertex along the stream network, which can produce a large number of barriers. This was discussed with the team and, for now, this behavior is required: if local or other knowledge identifies that a small section of stream is not actually steep, or that fish have another way to pass a given point, we need to know the next upstream barrier. If this volume of barriers turns out to be inefficient for the process, we discussed potentially thinning the output to flag barriers only every ~500m as a mitigation. More sophisticated approaches (e.g., identifying actual changes in slope while walking upstream, to only flag the start of a steep section) are out of scope for this work but may be investigated in the future.  The other suggestions of one barrier per mainstem or per connected group were rejected.

