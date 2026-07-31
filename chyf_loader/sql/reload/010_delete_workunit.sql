-- Remove all cached data for all workunit(s) before reloading.

TRUNCATE :target_flowpath_table;
TRUNCATE :target_shoreline_table;
TRUNCATE :target_aoi_table cascade;