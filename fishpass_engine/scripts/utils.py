"""Shared helpers that aren't database code: repo paths, shared constants, logging setup,
config/fishpass.yaml loaders, run details (who ran it, branch, commit), and GitHub Actions
job-summary output.

Imports here are kept light (yaml and model_plan are loaded inside the functions that need
them), so any script can import this module without pulling in extra dependencies or
creating a circular import.
"""

import getpass
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MODELS_DIR = REPO_ROOT / "config" / "models"

SPECIES_PARAMS_FILE = REPO_ROOT / "config" / "fish_species_parameters.yaml"

# Lifecycles. LIFESTAGES are the two that a barrier's passability and the species parameters are
# defined for; SPAWNREAR is their union (habitat and reporting only); LIFESTAGES_ALL is all three.
#
# These values are also used to build database column, view and JSON field names
# (e.g. f"{REAR}_habitat", f"upstream_anthro_{SPAWN}_count", f"total_{SPAWNREAR}_km"), so each one
# must be a valid unquoted PostgreSQL identifier part: lowercase letters, digits and underscores
# only (it must match IDENTIFIER_RE). Changing a value renames those columns in every output
# table, view and export. It also has to be matched by hand in the files that can't use these
# constants: init/database/fishpass_support_tables.sql, config/fish_species_parameters.yaml,
# the model plan files (reporting_values), the tests and the docs.
REAR = "rear"
SPAWN = "spawn"
SPAWNREAR = "spawnrear"
LIFESTAGES = (REAR, SPAWN)
LIFESTAGES_ALL = (*LIFESTAGES, SPAWNREAR)

# Identifiers that get interpolated directly into SQL (schema/table/column/role names can't be
# bound parameters in psycopg) are restricted to this safe charset.
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Plan codes and export file names: letters, digits and underscores (a leading digit is allowed).
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

# SRID of all_barriers.snapped_geometry / gradient_barriers.snapped_geometry (NAD83 lon/lat).
SNAPPED_GEOMETRY_SRID = 4617


def setup_logging():
	"""Log INFO and above to the console with timestamps -- shared by the command-line scripts."""
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(levelname)s %(name)s: %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)


# =================================================================================
#  SHARED CONFIG (config/fishpass.yaml)
# =================================================================================

DEFAULT_CONFIG_FILE = REPO_ROOT / "config" / "fishpass.yaml"

# Settings that a model plan may override; the fishpass.yaml 'wcrp' section supplies the
# default for each.
WCRP_SETTINGS = ("label_in_wcrp_rank_threshold", "min_avg_gain_km")


def load_config(config_path=DEFAULT_CONFIG_FILE):
	"""Load config/fishpass.yaml as a dict. yaml is imported lazily so modules that only
	need the other helpers don't require it."""
	import yaml

	if not Path(config_path).is_file():
		sys.exit(f"Config file not found: {config_path}")
	with open(config_path) as f:
		return yaml.safe_load(f) or {}


def config_section(name, config_path=DEFAULT_CONFIG_FILE):
	"""Return one top-level section of fishpass.yaml (exits if missing or not a mapping)."""
	section = load_config(config_path).get(name)
	if not isinstance(section, dict):
		sys.exit(f"Config file {config_path} is missing the '{name}' section")
	return section


def get_db_roles(config_path=DEFAULT_CONFIG_FILE):
	"""Return the database_roles section as {'owner': str, 'grant_all': tuple,
	'grant_select': tuple}. Role names are interpolated into GRANT/ALTER ... OWNER TO, so
	they are held to a safe identifier charset (and quoted by callers)."""
	section = config_section("database_roles", config_path)
	owner = section.get("owner")
	grant_all = section.get("grant_all") or []
	grant_select = section.get("grant_select") or []
	if not isinstance(owner, str) or not IDENTIFIER_RE.match(owner):
		sys.exit(f"database_roles.owner must be a valid role name, got {owner!r}")
	for key, roles in (("grant_all", grant_all), ("grant_select", grant_select)):
		if not isinstance(roles, list) or not all(isinstance(r, str) and IDENTIFIER_RE.match(r) for r in roles):
			sys.exit(f"database_roles.{key} must be a list of valid role names, got {roles!r}")
	return {"owner": owner, "grant_all": tuple(grant_all), "grant_select": tuple(grant_select)}


def natural_feature_types(plan, config_path=DEFAULT_CONFIG_FILE):
	"""The feature_type values classified as 'natural' for this plan -- every other feature_type
	is 'anthropogenic'. The plan's natural_feature_types_override wins if set (an empty list is
	allowed); otherwise structure_classification.natural_feature_types from fishpass.yaml.
	Values end up in SQL, so they are held to the safe identifier charset (exits otherwise)."""
	types = plan.get("natural_feature_types_override")
	if types is None:
		types = (load_config(config_path).get("structure_classification") or {}).get("natural_feature_types") or []
	for t in types:
		if not isinstance(t, str) or not IDENTIFIER_RE.match(t):
			sys.exit(f"Invalid natural feature type (unsafe for SQL): {t!r}")
	return list(types)


def wcrp_setting(plan, key, config_path=DEFAULT_CONFIG_FILE):
	"""Resolve a WCRP setting: the plan's own value wins when set (model_plan has already
	validated it); otherwise the default from fishpass.yaml's 'wcrp' section."""
	if key not in WCRP_SETTINGS:
		raise KeyError(f"Unknown WCRP setting: {key!r}")
	value = plan.get(key)
	if value is not None:
		return value
	from model_plan import wcrp_value_error  # lazy: model_plan imports this module and yaml

	value = config_section("wcrp", config_path).get(key)
	error = wcrp_value_error(key, value)
	if error:
		sys.exit(f"Invalid wcrp setting in {config_path}: {error}")
	return value


def wcrp_tracking_enums(config_path=DEFAULT_CONFIG_FILE):
	"""Return the tracking-table enum definitions from fishpass.yaml as an ordered dict."""
	section = config_section("wcrp", config_path)
	enums = section.get("tracking_table_enums")
	if not isinstance(enums, dict):
		sys.exit(f"Config file {config_path} is missing the 'wcrp.tracking_table_enums' section")
	for name, values in enums.items():
		if not isinstance(name, str) or not name.startswith("tt_"):
			sys.exit(f"Invalid WCRP enum name in {config_path}: {name!r}")
		if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
			sys.exit(f"WCRP enum values for {name!r} must be a list of strings in {config_path}")
	return enums


# =================================================================================
#  RUN DETAILS
# =================================================================================


def _git(*args):
	try:
		result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
	except (OSError, subprocess.CalledProcessError):
		return None
	return result.stdout.strip() or None


def run_details():
	"""(github_user, github_branch, commit_sha) -- from the GitHub Actions environment, or from
	git and the local user name for a local run."""
	user = os.environ.get("GITHUB_ACTOR") or f"local:{getpass.getuser()}"
	branch = os.environ.get("GITHUB_REF_NAME") or _git("rev-parse", "--abbrev-ref", "HEAD")
	sha = os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD")
	return user, branch, sha


def format_runtime(delta):
	"""hh:mm:ss (hours can go past 24)."""
	if delta is None:
		return None
	total = int(delta.total_seconds())
	hours, rest = divmod(total, 3600)
	minutes, seconds = divmod(rest, 60)
	return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# =================================================================================
#  GITHUB ACTIONS
# =================================================================================


def write_job_summary(markdown):
	"""Append markdown to the GitHub Actions job summary (shown on the run's summary page).
	GITHUB_STEP_SUMMARY is only set inside GitHub Actions, so this is a no-op when run locally."""
	summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
	if not summary_path:
		return
	with open(summary_path, "a", encoding="utf-8") as f:
		f.write(markdown.rstrip() + "\n\n")
