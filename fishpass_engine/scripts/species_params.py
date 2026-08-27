"""Load the fish species parameter file (docs/fish_species_parameter_file.md).
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPECIES_PARAMS_FILE = REPO_ROOT / "config" / "fish_species_parameters.yaml"

FIELDS = (
	"accessibility_gradient_spawning_max",
	"accessibility_gradient_rearing_max",
	"spawn_gradient_min",
	"spawn_gradient_max",
	"rear_gradient_min",
	"rear_gradient_max",
	"strahler_order_spawning_min",
	"strahler_order_spawning_max",
	"strahler_order_rearing_min",
	"strahler_order_rearing_max",
	"stream_order_1_spawning_weight",
	"stream_order_1_rearing_weight",
	"stream_order_2_spawning_weight",
	"stream_order_2_rearing_weight",
)


def load_species_params(params_path=DEFAULT_SPECIES_PARAMS_FILE):
	"""Return {species_code: {field: value, ...}} for every species in the parameter file.
	Fields not present in the file are None. Missing min/max fields mean "no restriction" is
	not assumed -- see habitat_gradient_ok/habitat_strahler_ok below for how a blank max/min
	pair (max < min, or either missing) is treated as "no habitat for this species/lifecycle",
	per docs/fish_species_parameter_file.md's strahler_order fields' documented convention,
	applied consistently to the gradient fields too."""

	if not Path(params_path).is_file():
		sys.exit(f"Species parameter file not found: {params_path}")

	with open(params_path) as f:
		data = yaml.safe_load(f)

	species = {}
	for entry in data.get("species", []):
		code = entry["code"]
		species[code] = {field: entry.get(field) for field in FIELDS}
	if not species:
		sys.exit(f"No species entries found in {params_path}")
	return species


def habitat_gradient_ok(params, lifecycle, segment_gradient):
	"""Used in compute statistics: min_<lc>_gradient <= segment_gradient <
	max_<lc>_gradient. False if segment_gradient is None, or either bound is missing/max<min
	"""

	if segment_gradient is None:
		return False
	min_v = params.get(f"{lifecycle}_gradient_min")
	max_v = params.get(f"{lifecycle}_gradient_max")
	if min_v is None or max_v is None or max_v < min_v:
		return False
	return min_v <= segment_gradient < max_v


LIFECYCLE_STRAHLER_FIELD = {"rear": "rearing", "spawn": "spawning"}


def habitat_strahler_ok(params, lifecycle, strahler_order):
	"""Unsed in compute statistics: min_<lc>_strahler_order <= strahler_order <
	max_<lc>_strahler_order. False if strahler_order is None, or either bound is missing/max<min."""

	if strahler_order is None:
		return False
	field_lifecycle = LIFECYCLE_STRAHLER_FIELD[lifecycle]
	min_v = params.get(f"strahler_order_{field_lifecycle}_min")
	max_v = params.get(f"strahler_order_{field_lifecycle}_max")
	if min_v is None or max_v is None or max_v < min_v:
		return False
	return min_v <= strahler_order < max_v


def stream_order_weight(params, lifecycle, strahler_order):
	"""Weighted-length multiplier for strahler_order (used in compute statistics): 
	stream_order_{1,2}_{spawning,rearing}_weight from the species
	parameter file for orders 1 and 2, 1.0 (no downweighting) for every order >= 3. lifecycle
	must be "rear" or "spawn" -- weighted length is not computed for "spawnrear"."""

	if strahler_order not in (1, 2):
		return 1.0
	field_lifecycle = LIFECYCLE_STRAHLER_FIELD[lifecycle]
	weight = params.get(f"stream_order_{strahler_order}_{field_lifecycle}_weight")
	return 1.0 if weight is None else weight
