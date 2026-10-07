#!/usr/bin/env python3
"""Create a WCRP's tracking table (and its blank2null trigger) if it doesn't exist yet.

Runs automatically at the start of every model run: run_model.py calls
ensure_tracking_table() before the output schema is rebuilt. On a plan's first
run the table is created; on every later run creation is skipped (the existing
table and its hand-entered data are never touched) and the skip is reported in
the log and the GitHub Actions job summary.

The standalone entry point (python create_wcrp_tracking_table.py <plan_code>) is
kept for local/manual setup. It is stricter: it refuses, with an error, if the
table already exists.

IMPORTANT -- schema choice: the tracking table lives in its own persistent
<code>_wcrp schema, NOT in the model's output_schema. run_model.py rebuilds the
output_schema from scratch on every run (init_output_schema does DROP SCHEMA
... CASCADE), which would wipe hand-entered tracking data and cascade-drop any
constraints. The <code>_wcrp schema is never touched by a model run, so the
data survives. barrier_id is a plain uuid (matching all_barriers.feature_id in
type) but carries NO foreign key -- a cross-schema FK into the ephemeral
output_schema couldn't survive the rebuild. rank_barriers.py validates every
tracking barrier_id against the freshly-built all_barriers on each run instead.

Database-wide prerequisites: the support.tt_* enum types are defined in
config/fishpass.yaml (wcrp.tracking_table_enums) and synced into the database by
sync_wcrp_tracking_enums(), which run_model.py and the standalone entry point
both call first. The generic support.blank2null() trigger function is NOT
created here: it comes from init/database/wcrp_support.sql, run by hand once per
database. This script checks both exist and stops with a clear message if not.

Guarantees:
  * An existing tracking table is never dropped, replaced, or altered.
    ensure_tracking_table() skips creation when it exists; create_tracking_table()
    (the standalone path) aborts BEFORE any DDL runs. The CREATE TABLE itself has
    no IF NOT EXISTS, so it can't silently skip. (The containing <code>_wcrp
    schema is created idempotently -- create schema if not exists.)
  * The per-table blank2null trigger IS idempotent (drop-if-exists then create).

Column layout follows the enum-canonical cheticamp definition: support.tt_*
enum types, numeric money fields, text date fields, barrier_id (uuid) as the
primary key. Per-species enum columns are generated from plan['target_species'].

Owner/grant roles come from config/fishpass.yaml (database_roles). Database
connection details come from environment variables only, via the db module.

Usage:
    python create_wcrp_tracking_table.py <plan_code>
"""
import argparse
import logging
import sys

from db import (
    as_role,
    db_connect,
    function_exists,
    get_db_roles,
    quote_ident,
    require_env,
    table_columns,
    table_exists,
    wcrp_tracking_enums,
)
from model_plan import IDENTIFIER_RE, load_model_plan

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# The blank2null trigger function lives in the shared support schema and is created once
# by init/database/wcrp_support.sql. The enum types are synced from config/fishpass.yaml
# at the start of each run.
SUPPORT = "support"
BLANK2NULL_FUNCTION = "blank2null"
SUPPORT_SQL_SCRIPT = "init/database/wcrp_support.sql"


def sync_wcrp_tracking_enums(conn, cursor, config_path=None):
    """Ensure each support.tt_* enum matches the configured YAML values, appending '' last."""
    enum_defs = wcrp_tracking_enums(config_path) if config_path else wcrp_tracking_enums()
    cursor.execute("create schema if not exists support;")
    for type_name, values in enum_defs.items():
        if not values:
            continue
        type_qualified = f"{SUPPORT}.{quote_ident(type_name)}"
        escaped_values = [v.replace("'", "''") for v in values]
        value_sql = ", ".join(f"'{v}'" for v in escaped_values)

        cursor.execute(
            f"SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            f"WHERE n.nspname = %s AND t.typname = %s;",
            (SUPPORT, type_name),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                f"CREATE TYPE {type_qualified} AS ENUM ({value_sql}, '');"
            )
            logger.info("Created enum %s from config/fishpass.yaml", type_qualified)
            continue

        cursor.execute(f"ALTER TYPE {type_qualified} ADD VALUE IF NOT EXISTS '';")
        for value in escaped_values:
            cursor.execute(
                f"ALTER TYPE {type_qualified} ADD VALUE IF NOT EXISTS '{value}' BEFORE '';"
            )
    conn.commit()


# Non-species columns, in the canonical cheticamp order. Each entry is
# (column_name, type_sql). Enum types are qualified to the support schema. The
# per-species blocks are spliced in at the right positions in _build_columns().
# barrier_id is a uuid (same type as all_barriers.feature_id) and is the primary
# key -- see _build_create_table_sql(). There is intentionally no foreign key
# (see the module docstring); rank_barriers.py validates ids per run instead.
_LEADING_COLUMNS = [
    ("internal_name", "character varying"),
    ("barrier_id", "uuid NOT NULL"),
    ("watercourse_name", "character varying"),
    ("road_name", "character varying"),
    ("structure_type", f"{SUPPORT}.tt_structure_type"),
    ("structure_owner", "character varying"),
    ("private_owner_details", "character varying"),
]

# Comes after the per-species structure_list_status_<sp> block.
_MIDDLE_COLUMNS = [
    ("passability_assessment_type", f"{SUPPORT}.tt_passability_asmt_type"),
    ("assessment_step_completed", f"{SUPPORT}.tt_assessment_step_type"),
    ("reason_for_exclusion", f"{SUPPORT}.tt_excl_reason_type"),
    ("method_of_exclusion", f"{SUPPORT}.tt_excl_method_type"),
]

# Comes after the per-species partial_passability block.
_TRAILING_COLUMNS = [
    ("upstream_habitat_quality", f"{SUPPORT}.tt_upstr_hab_quality_type"),
    ("constructability", f"{SUPPORT}.tt_constructability_type"),
    ("estimated_cost_$", "numeric"),
    ("priority", f"{SUPPORT}.tt_priority_type"),
    ("type_of_rehabilitation", f"{SUPPORT}.tt_rehab_type"),
    ("rehabilitated_by", "character varying"),
    ("rehabilitated_date", "text"),
    ("estimated_rehabilitation_cost_$", "numeric"),
    ("actual_project_cost_$", "numeric"),
    ("next_steps", f"{SUPPORT}.tt_next_steps_type"),
    ("timeline_for_next_steps", "text"),
    ("lead_for_next_steps", "character varying"),
    ("others_involved_in_next_steps", "character varying"),
    ("reason", "character varying"),
    ("notes", "character varying"),
    ("supporting_links", "character varying"),
]

def _tracking_table_name(plan):
    return f"tracking_table_{plan['code']}"


def _wcrp_schema(plan):
    """The persistent per-WCRP schema, e.g. 'ns' -> 'ns_wcrp'. plan['code'] is
    already validated by model_plan.PLAN_CODE_RE, and the _wcrp suffix keeps it
    within the safe identifier charset. RAW name -- quote_ident() it before
    interpolating into SQL (a code may start with a digit)."""
    return f"{plan['code']}_wcrp"


def _validate_species(species_list):
    """target_species feed column-name suffixes interpolated into DDL, so hold
    them to the same safe charset model_plan uses for other identifiers."""
    for sp in species_list:
        if not isinstance(sp, str) or not IDENTIFIER_RE.match(sp):
            sys.exit(f"Invalid species code (unsafe for a column name): {sp!r}")


def _build_columns(species_list):
    """Return the ordered (name, type_sql) column list, splicing per-species
    enum columns into their canonical positions."""
    structure_list_status = [
        (f"structure_list_status_{sp}", f"{SUPPORT}.tt_structure_list_status_type")
        for sp in species_list
    ]
    partial_passability = []
    for sp in species_list:
        partial_passability.append(
            (f"partial_passability_{sp}", f"{SUPPORT}.tt_partial_passability_type")
        )
        partial_passability.append(
            (
                f"partial_passability_notes_{sp}",
                f"{SUPPORT}.tt_partial_passability_notes_type",
            )
        )
    return (
        _LEADING_COLUMNS
        + structure_list_status
        + _MIDDLE_COLUMNS
        + partial_passability
        + _TRAILING_COLUMNS
    )


def _build_create_table_sql(schema, table, species_list):
    """Render the CREATE TABLE statement (no IF NOT EXISTS -- creation is
    guarded separately and must fail rather than silently skip).

    barrier_id is the primary key. There is deliberately no foreign key onto
    all_barriers: that table lives in the ephemeral output_schema and is
    rebuilt every model run, so a cross-schema FK couldn't survive. Referential
    integrity is enforced per run by rank_barriers.py instead.
    """
    columns = _build_columns(species_list)
    col_defs = [f"    {quote_ident(name)} {type_sql}" for name, type_sql in columns]
    col_defs.append(
        f"    CONSTRAINT {quote_ident(table + '_pkey')} PRIMARY KEY ({quote_ident('barrier_id')})"
    )
    qualified = f"{quote_ident(schema)}.{quote_ident(table)}"
    return f"CREATE TABLE {qualified} (\n" + ",\n".join(col_defs) + "\n);"


def _required_enum_types(species_list):
    """The support.tt_* enum type names (RAW, unqualified) the table needs."""
    prefix = f"{SUPPORT}."
    return sorted(
        {t[len(prefix):] for _, t in _build_columns(species_list) if t.startswith(prefix)}
    )


def _check_support_objects(cursor, species_list):
    """Stop with a clear message if a database-wide support object is missing:
    an enum type (synced from config/fishpass.yaml) or blank2null() (created by
    wcrp_support.sql)."""
    missing_types = [
        f"{SUPPORT}.{t}"
        for t in _required_enum_types(species_list)
        if not _type_exists(cursor, SUPPORT, t)
    ]
    problems = []
    if missing_types:
        problems.append(
            f"Missing WCRP enum type(s): {', '.join(missing_types)}. These are synced "
            f"from the wcrp.tracking_table_enums section of config/fishpass.yaml at the "
            f"start of each run -- check that each type is listed there with at least "
            f"one value."
        )
    if not function_exists(cursor, SUPPORT, BLANK2NULL_FUNCTION):
        problems.append(
            f"Missing {SUPPORT}.{BLANK2NULL_FUNCTION}(). Run {SUPPORT_SQL_SCRIPT} "
            f"against this database first."
        )
    if problems:
        sys.exit(" ".join(problems))


def _type_exists(cursor, schema, type_name):
    cursor.execute(
        """
        select 1
        from pg_type t
        join pg_namespace n on n.oid = t.typnamespace
        where n.nspname = %s and t.typname = %s;
        """,
        (schema, type_name),
    )
    return cursor.fetchone() is not None


def _apply_ownership_and_grants(cursor, qualified, roles):
    """Set the table owner and role grants (config/fishpass.yaml database_roles).
    Per-WCRP biologist access is applied separately later."""
    cursor.execute(f"alter table {qualified} owner to {quote_ident(roles['owner'])};")
    for role in roles["grant_all"]:
        cursor.execute(f"grant all on table {qualified} to {quote_ident(role)};")
    for role in roles["grant_select"]:
        cursor.execute(f"grant select on table {qualified} to {quote_ident(role)};")


def ensure_tracking_table(conn, cursor, plan):
    """Create the plan's tracking table if it doesn't exist; otherwise skip.

    Called by run_model.py at the start of every model run, BEFORE the output
    schema is rebuilt, so a missing support object fails in seconds rather than
    after a full run. Returns True if the table was created, False if it already
    existed and creation was skipped.

    Still exits (failing the run) if the database-wide support objects from
    wcrp_support.sql are missing -- every tracking table depends on them.
    """
    schema = _wcrp_schema(plan)
    table = _tracking_table_name(plan)

    if table_exists(cursor, schema, table):
        # The table's trigger calls blank2null(), so it must still exist.
        if not function_exists(cursor, SUPPORT, BLANK2NULL_FUNCTION):
            sys.exit(
                f"{SUPPORT}.{BLANK2NULL_FUNCTION}() is missing. Run "
                f"{SUPPORT_SQL_SCRIPT} against this database."
            )
        logger.info(
            "Tracking table %s.%s already exists -- skipping creation.", schema, table
        )
        return False

    logger.info("Tracking table %s.%s not found -- creating it.", schema, table)
    create_tracking_table(conn, cursor, plan)
    return True


def check_tracking_table_columns(cursor, plan):
    """Stop with a clear message if the plan's tracking table lacks a column this
    run will read.

    Called by run_model.py right after ensure_tracking_table(), BEFORE the output
    schema is rebuilt. An existing tracking table is never altered, so a species
    added to the plan after the table was created has no structure_list_status_<sp>
    / partial_passability_<sp> columns. Without this check that only surfaces as
    "column does not exist" in rank_barriers.py / create_combined_view.py, at the
    very end of a full model run.

    Checks the columns for the plan's REPORTING species (the ones ranking and the
    combined view read), plus every non-species column.
    """
    schema = _wcrp_schema(plan)
    table = _tracking_table_name(plan)
    species_list = sorted({sp for sp, _lc in plan["reporting_species_lifecycles"]})
    _validate_species(species_list)

    existing = table_columns(cursor, schema, table)
    missing = [name for name, _type in _build_columns(species_list) if name not in existing]
    if missing:
        sys.exit(
            f"Tracking table {schema}.{table} is missing column(s) this model run "
            f"needs: {', '.join(missing)}. An existing tracking table is never altered "
            f"automatically (e.g. when a species is added to the plan) -- add the "
            f"column(s) by hand, then re-run."
        )
    logger.info("Tracking table %s.%s has every column this run needs.", schema, table)


def create_tracking_table(conn, cursor, plan):
    """Create the WCRP's tracking table (in <code>_wcrp) and attach the
    blank2null trigger.

    Aborts (sys.exit) without touching the table if it already exists, or if
    the database-wide support objects from wcrp_support.sql are missing. A model
    run goes through ensure_tracking_table() instead, which checks for the
    table first and skips rather than aborting.
    """
    schema = _wcrp_schema(plan)
    table = _tracking_table_name(plan)
    species_list = plan["target_species"]
    _validate_species(species_list)
    roles = get_db_roles()

    _check_support_objects(cursor, species_list)

    # Act as the owner role for the whole setup. The connecting user is expected
    # to be a granted member of this role (no password needed for SET ROLE), but
    # may not BE the role. Doing this first means the schema, table, and every
    # object are created and owned by the owner role directly -- otherwise the
    # schema would be owned by the connecting user and ALTER TABLE ... OWNER TO
    # would fail because the owner lacks CREATE on a schema it doesn't own. When
    # run as the owner directly (e.g. the GitHub Action), SET ROLE is a no-op.
    # as_role() rolls back + RESETs on the way out, so on any failure (including
    # the "already exists" exit below) nothing is left half-created.
    with as_role(conn, cursor, roles["owner"]):
        # The persistent WCRP schema is created idempotently -- it is never
        # dropped by a model run, so this is a no-op after the first setup.
        cursor.execute(f"create schema if not exists {quote_ident(schema)};")

        if table_exists(cursor, schema, table):
            # Only reachable via the standalone entry point (or if the table
            # appears between ensure_tracking_table's check and here).
            sys.exit(
                f"Tracking table {schema}.{table} already exists -- refusing to "
                f"recreate it. This table is created once and maintained forever; "
                f"delete it manually first if you truly intend to rebuild it."
            )

        qualified = f"{quote_ident(schema)}.{quote_ident(table)}"

        logger.info("Creating tracking table %s", qualified)
        cursor.execute(_build_create_table_sql(schema, table, species_list))

        logger.info("Setting ownership and grants on %s", qualified)
        _apply_ownership_and_grants(cursor, qualified, roles)

        logger.info("Attaching %s.%s trigger to %s", SUPPORT, BLANK2NULL_FUNCTION, qualified)
        cursor.execute(f"drop trigger if exists blank2null_trg on {qualified};")
        cursor.execute(
            f"create trigger blank2null_trg"
            f" before insert or update on {qualified}"
            f" for each row execute function {SUPPORT}.{BLANK2NULL_FUNCTION}();"
        )

        conn.commit()
        logger.info("Tracking table setup complete for %s", qualified)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "plan_code", help="Plan code -- loads config/models/<plan_code>.yaml"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    require_env()
    plan = load_model_plan(args.plan_code)
    logger.info(
        "Setting up tracking table for plan %r (schema %r)",
        plan["code"],
        _wcrp_schema(plan),
    )
    conn = db_connect()
    try:
        with conn.cursor() as cursor:
            sync_wcrp_tracking_enums(conn, cursor)
            create_tracking_table(conn, cursor, plan)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
