"""Client for the CABD Barrier API (fishpass/requirements/requirements.md's "CABD Barrier API"
section).
"""

import sys

import requests

CABD_BASE_URL = "https://cabd-web.azurewebsites.net/cabd-api/"
RESULT_CAP = 50_000  # confirmed API limit -- see requirements.md
DEFAULT_CHUNK_SIZE = 25  # work units per request, kept comfortably under the 50,000 cap
REQUEST_TIMEOUT_S = 120

# requirements.md Load Structures step 2: passability_status_code -> 0/1 passability value.
PASSABILITY_CODE_MAP = {
	1: 0,  # Barrier
	2: 0,  # Partial Barrier
	3: 1,  # Passable
	4: 0,  # Unknown
	5: 1,  # NA - No Structure (this shouldn't exist as they should be excluded from the analysis)
	6: 1,  # NA - Decommissioned / Removed
}


def map_passability(status_code):
	"""Map a CABD passability_status_code to a 0/1 passability value. A missing/unrecognized
	code is treated the same as 4 (Unknown) -> 0 (impassable), the conservative default."""
	if status_code is None:
		return 0
	return PASSABILITY_CODE_MAP.get(status_code, 0)


def _build_url(feature_type, short_names, base_url):
	filter_value = ";".join(short_names)
	return f"{base_url}features/{feature_type}?filter=passability_status_code:neq:5&filter=nhn_watershed_id:in:{filter_value}"


def fetch_feature_type(feature_type, short_names, base_url=CABD_BASE_URL, chunk_size=DEFAULT_CHUNK_SIZE, session=None):
	"""Fetch every CABD feature of `feature_type` within the given chyf_raw.aoi.short_name
	work units, chunking requests by work-unit subgroup to stay under the API's 50,000-feature
	cap (requirements.md's "CABD Barrier API" section). A chunk response that hits the cap is
	treated as a signal of truncation, not a complete result, and aborts the run rather than
	silently returning partial data -- see requirements.md.

	Yields GeoJSON feature dicts one at a time, chunked by work-unit subgroup so each chunk's
	response can be freed as soon as its features are consumed rather than holding every chunk's
	features in one combined list. Features with properties.use_analysis == False are skipped;
	features where it's True or missing/null are yielded.
	"""
	http = session or requests
	for i in range(0, len(short_names), chunk_size):
		chunk = short_names[i:i + chunk_size]
		url = _build_url(feature_type, chunk, base_url)
		try:
			resp = http.get(url, timeout=REQUEST_TIMEOUT_S)
			resp.raise_for_status()
		except requests.exceptions.HTTPError as e:
			if e.response.status_code == 404:
				sys.exit(f"404 response from CABD API at {url}")
			raise
		chunk_features = resp.json().get("features", [])
		if len(chunk_features) >= RESULT_CAP:
			sys.exit(
				f"CABD API returned {len(chunk_features)} feature(s) for feature_type="
				f"{feature_type!r}, work units {chunk} -- this may be truncated at the "
				f"{RESULT_CAP}-feature cap. Reduce chunk_size and retry."
			)
		for feature in chunk_features:
			if feature.get("properties", {}).get("use_analysis") is False:
				continue
			yield feature
