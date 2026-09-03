"""Load and validate a model plan YAML file, as documented in
docs/inputs/model_plan_file.md.
"""

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS_DIR = REPO_ROOT / "config" / "models"

# Identifiers that get interpolated directly into SQL (schema/table names can't be bound
# parameters in psycopg) are restricted to this safe charset.
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLAN_CODE_RE = re.compile(r"^[A-Za-z0-9_]+$")

LIFECYCLES = ("rear", "spawn", "spawnrear")

DEFAULTS = {
	"structure_snap_edge_distance_m": 100,
	"structure_snap_vertex_distance_m": 50,
	"habitat_point_snap_edge_distance_m": 100,
	"habitat_point_snap_vertex_distance_m": 50,
	"structure_update_table": "support.structure_updates",
	"structure_new_table": "support.new_structures",
	"habitat_update_table": "support.habitat_updates",
	"gradient_barriers_table": "support.gradient_barriers",
	"impassable_threshold": 1.0,
	"natural_feature_types_override": None,
}

REQUIRED_FIELDS = (
	"code",
	"output_schema",
	"aoi",
	"target_species",
	"reporting_values",
	"structure_types",
)


def _fail(plan_path, message):
	sys.exit(f"Invalid model plan {plan_path}: {message}")


def _validate_aoi(aoi, plan_path):
	"""Return (aoi_kind, aoi_value) -- aoi_kind is one of 'workunit', 'province',
	'upstream_of'. Exits if not exactly one is given."""

	if not isinstance(aoi, dict):
		_fail(plan_path, "'aoi' must be a mapping with exactly one of workunit/province/upstream_of")

	present = [k for k in ("workunit", "province", "upstream_of") if k in aoi]
	if len(present) != 1:
		_fail(
			plan_path,
			f"'aoi' must specify exactly one of workunit/province/upstream_of, got: {present or 'none'}",
		)

	kind = present[0]

	value = aoi[kind]
	if kind == "workunit" and value == "all":
		return kind, "all"
	if not isinstance(value, list) or not value:
		_fail(plan_path, f"aoi.{kind} must be a non-empty list" + (" or 'all'" if kind == "workunit" else ""))
	return kind, value


def expand_reporting_values(reporting_values, target_species, plan_path):
	"""Return a sorted list of (species_code, lifecycle) tuples, expanding 'all' in either
	position of each '<species>_<lifecycle>' entry.

	'spawnrear' is the union of 'rear' and 'spawn' everywhere it's computed (there is no
	species-parameter or habitat_updates support for a distinct 'spawnrear' value) -- see
	the Outputs section.
	"""

	result = set()
	for rv in reporting_values:
		sp_part, sep, lc_part = rv.partition("_")
		if not sep:
			_fail(plan_path, f"Invalid reporting_values entry (expected <species>_<lifecycle>): {rv!r}")

		species_list = target_species if sp_part == "all" else [sp_part]
		lifecycle_list = LIFECYCLES if lc_part == "all" else [lc_part]

		for lc in lifecycle_list:
			if lc not in LIFECYCLES:
				_fail(plan_path, f"Invalid lifecycle in reporting_values entry {rv!r}: {lc!r}")
		for sp in species_list:
			if sp not in target_species:
				_fail(
					plan_path,
					f"reporting_values entry {rv!r} references species {sp!r} not in target_species",
				)

		for sp in species_list:
			for lc in lifecycle_list:
				result.add((sp, lc))

	return sorted(result)


def load_model_plan(plan_code, models_dir=DEFAULT_MODELS_DIR):
	"""Load, validate, and apply defaults to config/models/<plan_code>.yaml.

	Returns a dict with all fields from model_plan_file.md, plus:
	  * aoi_kind / aoi_value -- parsed form of the 'aoi' field
	  * reporting_species_lifecycles -- expanded (species, lifecycle) tuples
	"""

	if not PLAN_CODE_RE.match(plan_code):
		sys.exit(f"Invalid plan code: {plan_code!r}")

	plan_path = Path(models_dir) / f"{plan_code}.yaml"
	if not plan_path.is_file():
		sys.exit(f"Model plan file not found: {plan_path}")

	with open(plan_path) as f:
		data = yaml.safe_load(f) or {}

	missing = [f for f in REQUIRED_FIELDS if f not in data]
	if missing:
		_fail(plan_path, f"missing required field(s): {', '.join(missing)}")

	if data["code"] != plan_code:
		_fail(plan_path, f"'code' field ({data['code']!r}) must match the plan code ({plan_code!r})")

	if not IDENTIFIER_RE.match(data["output_schema"]):
		_fail(plan_path, f"invalid output_schema: {data['output_schema']!r}")

	aoi_kind, aoi_value = _validate_aoi(data["aoi"], plan_path)

	if not isinstance(data["target_species"], list) or not data["target_species"]:
		_fail(plan_path, "target_species must be a non-empty list")

	if not isinstance(data["structure_types"], list) or not data["structure_types"]:
		_fail(plan_path, "structure_types must be a non-empty list")

	override = data.get("natural_feature_types_override")
	if override is not None and (
		not isinstance(override, list) or not all(isinstance(v, str) for v in override)
	):
		_fail(plan_path, "natural_feature_types_override must be a list of feature_type strings")

	reporting_species_lifecycles = expand_reporting_values(
		data["reporting_values"], data["target_species"], plan_path
	)

	plan = {**DEFAULTS, **data}
	plan["aoi_kind"] = aoi_kind
	plan["aoi_value"] = aoi_value
	plan["reporting_species_lifecycles"] = reporting_species_lifecycles
	plan.setdefault("update_scope", plan["code"])
	plan["include_gradient_barriers"] = "gradients" in plan["structure_types"]

	threshold = plan["impassable_threshold"]
	if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not (0 <= threshold <= 1):
		_fail(plan_path, f"impassable_threshold must be a number between 0 and 1, got {threshold!r}")

	return plan
