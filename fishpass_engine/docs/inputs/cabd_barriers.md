# CABD Barriers

Barriers are loaded for the purposes of fishpass modelling from the CABD API.  All barrier features identified in the `feature_type` model parameter are loaded into tables in the `output_schema` model parameter.

CABD Features with passability_type_code = 5 or a use_analysis value of False are EXCLUDED from the analysis and not loaded.  (A use_analysis value of null or true are included)

### CABD API

Base URL: `https://cabd-web.azurewebsites.net/cabd-api/`

Feature Type Endpoint: `https://cabd-web.azurewebsites.net/cabd-api/features/<feature_type>/`

Returns a GeoJSON feature collection for the requested `<feature_type>` (e.g. `dams`, `waterfalls`, `stream_crossings`).


### Filtering

The API supports filtering with one or more `filter=<field>:in:<value1>;<value2>` query parameters, combined with `&`. 

Example:

```text
https://cabd-web.azurewebsites.net/cabd-api/features/waterfalls?filter=nhn_watershed_id:in:02PH002;02PH001&filter=passability_type_code:neq:5
```
Each feature type is loaded using a separate call to the API. We use two filters to limit results: an aoi filter and passability type code filters: `filter=nhn_watershed_id:in:shortname1,shortname2&filter=passability_type_code:neq:5`. 
Use analysis is not passed as a filter (due to null values), but are filted by the code.



 ### CABD Passability Status Code to FishPass Passability Status Value 

 The following table identifies how CABD passability status codes are converted to the passability status value used by the FishPass modelling.

| CABD Passability Status Code | FishPass Passability Status Value |
| :---- | :---- |
| 1 (Barrier) | 0 |
| 2 (Partial Barrier) | 0 |
| 3 (Passable) | 1 |
| 4 (Unknown) | 0 |
| 6 (NA - Decommissioned / Removed) | 1 |
| 5 (NA - No Structure) | N/A (not loaded) |


### Mappings

This table identifies how the CABD outputs are mapped to the `output_schema.all_barriers` table.

| CABD Field | All Barrier Table Field |
| :---- | :---- |
| cabd_id | cabd_id |
| lat/lon | geometry |
| passability_status_code | species_passability_value* |

*the species_passability_value is populated with one key for each species in the `target_species` (from the model parameter file), for each `life stage` (rear/spawn). The values are computed using the mapping above.


### Result Cap
A single query is capped at 50,000 features. Structure loading calls the API once per feature type by default, which normally keeps each call under the cap. If a single feature type's query for the requested AOI(s) still exceeds 50,000 features, that request must be further split by work-unit subgroup. A response of exactly 50,000 features should be treated as a signal the result was truncated, not assumed to be a complete result.