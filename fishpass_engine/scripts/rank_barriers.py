#!/usr/bin/env python3
"""Generate and run the automated barrier-ranking query for a model plan.

Builds a per-(species, lifecycle) ranked-barriers table from the plan's
<output_schema>.anthropogenic_barriers_<species> view, folding in rehabilitated
structures recorded in the WCRP tracking table, and writes a slim output table
containing ONLY the barrier id plus the ranking fields generated here (group
membership, per-group gains, and the three rank columns). The barriers,
tracking, and ranked tables are intended to be joined into a single export view
later, so nothing from the source view is duplicated into the output.

Runs as part of run_model.py (call run_ranking after create_barrier_views), and
can also be run standalone for one-off reruns.

Usage:
    python rank_barriers.py <plan_code>              # run against the database
    python rank_barriers.py <plan_code> --dry-run    # print SQL, no connection
    python rank_barriers.py <plan_code> --species as  # one species' pairs only
"""
import argparse
import logging
import sys

from db import db_connect, quote_ident, require_env
from model_plan import IDENTIFIER_RE, load_model_plan

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# =================================================================================
#  CONFIGURATION
# =================================================================================
# Ranking is driven by the plan's reporting_species_lifecycles -- the cached
# result of model_plan.expand_reporting_values(). One ranked table is produced
# per (species, lifecycle) pair (see resolve_ranking_pairs).

# Minimum weighted average gain per barrier (km). Groups below this are pushed
# to the bottom of the immediate-gain ranking.
MIN_AVG_GAIN_KM = 0.5

# Ownership / grants on the final ranked table -- aligned with the tracking-table
# convention (create_tracking_table.py). OWNER_ROLE also owns the <code>_wcrp
# schema, so the ranked/_work tables are created there without extra privileges.
OWNER_ROLE = "fishpass"
GRANT_ALL_ROLES = ("cwf_analyst", "cwf_tech")
GRANT_SELECT_ROLES = ("cwf_user",)

# Rehabilitated-structure match. The tracking table's status column is a
# controlled enum (support.tt_structure_list_status_type), so this is a strict
# equality match.
REHABILITATED_STATUS = "Rehabilitated barrier"

# all_barriers table (in the output schema) that tracking barrier_ids are
# validated against each run -- this is the FK replacement.
ALL_BARRIERS_TABLE = "all_barriers"
ALL_BARRIERS_KEY = "feature_id"

# Geometry / mainstem derivation (spatial stream_id_up lookup).
BARRIER_GEOM_COL = "snapped_geometry"
STREAM_GEOM_COL = "geometry"

# Every *_length field in the barriers view is in METRES; the km-conversion stage
# divides each by 1000 and renames it with a _km suffix.
LENGTH_FIELDS = [
    "spawn_upstream_accessible_length",
    "rear_upstream_accessible_length",
    "rear_upstream_length",
    "rear_functional_upstream_length",
    "rear_weighted_connected_upstream_length",
    "rear_weighted_disconnected_upstream_length",
    "rear_functional_weighted_connected_upstream_length",
    "rear_functional_weighted_disconnected_upstream_length",
    "spawn_upstream_length",
    "spawn_functional_upstream_length",
    "spawn_weighted_connected_upstream_length",
    "spawn_weighted_disconnected_upstream_length",
    "spawn_functional_weighted_connected_upstream_length",
    "spawn_functional_weighted_disconnected_upstream_length",
    "spawnrear_upstream_length",
    "spawnrear_functional_upstream_length",
    "spawnrear_weighted_connected_upstream_length",
    "spawnrear_weighted_disconnected_upstream_length",
    "spawnrear_functional_weighted_connected_upstream_length",
    "spawnrear_functional_weighted_disconnected_upstream_length",
]


# =================================================================================
#  PLAN-DERIVED VALUES + IDENTIFIERS
# =================================================================================
def resolve_ranking_pairs(plan, species_filter=None):
    """Return the (species, lifecycle) tuples this run will rank.

    This is simply the cached result of model_plan.expand_reporting_values() --
    plan['reporting_species_lifecycles'] -- optionally narrowed to one species by
    the --species flag. The expansion of reporting_values ('<species>_<lifestage>'
    parsing, 'all' handling, lifecycle validation, species-in-target_species
    checks) is NOT re-done here; it lives entirely in model_plan.py. One ranked
    table is built per pair.

    The only extra check is a SQL-safety guard on the species code (it is
    interpolated into table/column identifiers, which can't be bound params);
    this is an injection defense, not a re-validation of the expansion.
    """
    pairs = [tuple(pair) for pair in plan["reporting_species_lifecycles"]]
    if species_filter is not None:
        pairs = [(sp, lc) for sp, lc in pairs if sp == species_filter]
        if not pairs:
            sys.exit(
                f"--species {species_filter!r} has no (species, lifecycle) pairs in "
                f"the plan's reporting_values."
            )
    for sp, _lc in pairs:
        if not IDENTIFIER_RE.match(sp):
            sys.exit(f"Invalid species code (unsafe for a column name): {sp!r}")
    return pairs


class RankingConfig:
    """All the fully-resolved names/values a single ranking run needs."""

    def __init__(self, plan, species_code, lifecycle):
        self.schema = plan["output_schema"]          # e.g. ns_test  (READ from)
        self.watershed = plan["code"]                # e.g. ns
        self.species = species_code                  # e.g. as
        self.lifecycle = lifecycle                   # from reporting_species_lifecycles

        # Whether to fold in the rehabilitated value from the tracking table. Defaults True (dry-run
        # shows the full SQL); set False at runtime when the persistent tracking
        # table doesn't exist yet (first run, before the WCRP is set up).
        self.include_rehab = True

        # Persistent per-WCRP schema (owned by OWNER_ROLE) -- holds BOTH the
        # tracking table and the ranked output/_work tables.
        self.wcrp_schema = f"{self.watershed}_wcrp"

        # Sources are READ from the ephemeral output_schema. All name parts are
        # validated (output_schema via IDENTIFIER_RE, code via PLAN_CODE_RE,
        # species via resolve_ranking_pairs; lifecycle via model_plan expansion),
        # so they are safe to interpolate directly.
        self.barriers_view = f"{self.schema}.anthropogenic_barriers_{self.species}"
        self.streams = f"{self.schema}.streams"
        self.all_barriers = f"{self.schema}.{ALL_BARRIERS_TABLE}"

        # Outputs are WRITTEN to the persistent WCRP schema. The lifecycle is part
        # of the name so a plan reporting multiple lifecycles for a species yields
        # one distinct ranked table per (species, lifecycle) pair.
        table_stem = f"ranked_barriers_{self.species}_{self.lifecycle}_{self.watershed}"
        self.ranked = f"{self.wcrp_schema}.{table_stem}"
        self.work = f"{self.wcrp_schema}.{table_stem}_work"

        # Tracking table (create_tracking_table.py). Status column is
        # species-suffixed (lifecycle-agnostic).
        self.tracking_schema = self.wcrp_schema
        self.tracking_table_name = f"tracking_table_{self.watershed}"
        self.tracking_table = f"{self.tracking_schema}.{self.tracking_table_name}"
        self.col_tracking_status = f"structure_list_status_{self.species}"

        lc = self.lifecycle
        # Counts (unitless).
        self.col_upstr_count = f"upstream_anthro_{lc}_count"
        self.col_downstr_count = f"downstream_anthro_{lc}_count"
        # Source-view length columns (still metres) -- read pre-conversion.
        self.src_func_upstr = f"{lc}_functional_upstream_length"
        self.src_hab_exists = f"{lc}_upstream_length"
        # Ranked-table length columns after km conversion.
        self.col_func_upstr = f"{lc}_functional_upstream_length_km"
        self.w_func_upstr = f"{lc}_functional_weighted_disconnected_upstream_length_km"
        self.w_total_upstr = f"{lc}_weighted_disconnected_upstream_length_km"
        # Derived per-group gain columns (km).
        self.gain_total = f"total_{lc}_hab_gain_group_km"
        self.gain_w_total = "w_total_hab_gain_group_km"
        self.gain_avg = "avg_gain_per_barrier_km"
        self.gain_w_avg = "w_avg_gain_per_barrier_km"
        # Downstream id arrays (native uuid[]).
        self.col_downstr_ids_spawn = "downstream_anthro_spawn_ids"
        self.col_downstr_ids_rear = "downstream_anthro_rear_ids"
        # Passability gate columns + surfaced decimal outputs.
        self.col_passability_spawn = "passability_status_spawn"
        self.col_passability_rear = "passability_status_rear"
        self.out_passability_spawn = f"{self.species}_spawn_passability"
        self.out_passability_rear = f"{self.species}_rear_passability"


# =================================================================================
#  SQL BUILDERS  (one function per logical stage; each returns a SQL string)
# =================================================================================
def _passability_predicate(c):
    # Keep a barrier only if it blocks the ranked lifestage: spawn keeps
    # spawn-impassable, rear keeps rear-impassable, and spawnrear keeps a barrier
    # impassable for either spawn or rear. A barrier fully passable ('1') for the
    # ranked lifestage(s) is dropped (unless folded back in as rehabilitated).
    if c.lifecycle == "spawn":
        return f"b.{c.col_passability_spawn}::text <> '1'"
    if c.lifecycle == "rear":
        return f"b.{c.col_passability_rear}::text <> '1'"
    return (
        f"(b.{c.col_passability_spawn}::text <> '1' "
        f"OR b.{c.col_passability_rear}::text <> '1')"
    )


def sql_create_working_table(c):
    """DROP + rebuild the working table from the barriers view.

    Rehabilitated structures are folded in via the tracking-table LEFT JOIN
    (strict enum match), unless c.include_rehab is False (the persistent
    tracking table doesn't exist yet). Length fields are still in METRES here;
    the next stage converts them to km. anthropogenic_barriers_<species> is a
    view, but SELECT ... INTO materialises the needed columns into a real table,
    so all later ALTER/UPDATE stages work normally. The passability gate is
    lifecycle-specific (see _passability_predicate): a barrier is kept only if it
    blocks the ranked lifestage(s), with rehabilitated barriers always kept.
    """
    passability_predicate = _passability_predicate(c)
    if c.include_rehab:
        tracking_join = (
            f"    LEFT JOIN {c.tracking_table} tt\n"
            f"        ON tt.barrier_id = b.id"
        )
        rehab_clause = (
            f"\n             OR tt.{c.col_tracking_status} = '{REHABILITATED_STATUS}'"
        )
    else:
        tracking_join = (
            "    -- tracking table not present yet; rehab fold-in skipped this run"
        )
        rehab_clause = ""
    return f"""
    DROP TABLE IF EXISTS {c.work} CASCADE;
    SELECT b.*
        -- Only ADDED column(s): passability surfaced as decimal for readability.
        ,b.{c.col_passability_spawn}::decimal AS {c.out_passability_spawn}
        ,b.{c.col_passability_rear}::decimal  AS {c.out_passability_rear}
        INTO {c.work}
    FROM {c.barriers_view} b
{tracking_join}
    WHERE (
                {passability_predicate}{rehab_clause}
          )
        AND b.{c.src_hab_exists} != 0
    ORDER BY b.{c.src_func_upstr} DESC;
    ALTER TABLE IF EXISTS {c.work} ALTER COLUMN id SET NOT NULL;
    ALTER TABLE IF EXISTS {c.work} ADD COLUMN group_id numeric;
    ALTER TABLE IF EXISTS {c.work} ADD PRIMARY KEY (id);
    """


def sql_convert_lengths_to_km(c):
    """Convert every length column from metres to km and rename with a _km suffix."""
    lines = [
        "    ------------- CONVERT LENGTH FIELDS FROM METRES TO KILOMETRES -------------",
        "    -- Divide each *_length column by 1000 and rename with a _km suffix so the",
        "    -- unit is explicit for all downstream stages (gains, ranks, min_avg_gain_km).",
    ]
    for f in LENGTH_FIELDS:
        lines.append(
            f"    ALTER TABLE {c.work} "
            f"ALTER COLUMN {f} TYPE double precision USING ({f}::double precision / 1000.0);"
        )
        lines.append(f"    ALTER TABLE {c.work} RENAME COLUMN {f} TO {f}_km;")
    return "\n".join(lines) + "\n"


def sql_derive_stream_id_up(c):
    """Derive stream_id_up spatially: a barrier belongs to the stream edge whose
    downstream end (st_endpoint) it sits on, within a 0.01 tolerance."""
    return f"""
    ALTER TABLE {c.work} DROP COLUMN IF EXISTS stream_id;
    ALTER TABLE {c.work} ADD COLUMN IF NOT EXISTS stream_id_up uuid;
    UPDATE {c.work} SET stream_id_up = NULL;
    WITH ids AS (
        SELECT a.id AS stream_id, b.id AS barrier_id
        FROM {c.streams} a, {c.work} b
        WHERE st_dwithin(a.{STREAM_GEOM_COL}, b.{BARRIER_GEOM_COL}, 0.01)
          AND st_dwithin(st_endpoint(a.{STREAM_GEOM_COL}), b.{BARRIER_GEOM_COL}, 0.01)
    )
    UPDATE {c.work}
        SET stream_id_up = a.stream_id
        FROM ids a
        WHERE a.barrier_id = {c.work}.id;
    """


def sql_assign_mainstem_and_initial_groups(c):
    """Add mainstem_id from the stream network and seed one group per mainstem."""
    base = c.work.split(".")[-1]
    return f"""
    --TO FIX: some group_ids need to get combined - e.g., multiple branches of river
    ALTER TABLE {c.work} ADD COLUMN IF NOT EXISTS mainstem_id uuid;
    UPDATE {c.work} SET mainstem_id = t.mainstem_id
        FROM {c.streams} t WHERE t.id = stream_id_up;
    CREATE INDEX IF NOT EXISTS {base}_idx_mainstem ON {c.work} (mainstem_id);
    CREATE INDEX IF NOT EXISTS {base}_idx_group_id ON {c.work} (group_id);
    CREATE INDEX IF NOT EXISTS {base}_idx_id       ON {c.work} (id);
    WITH mainstems AS (
        SELECT DISTINCT mainstem_id, row_number() OVER () AS group_id
        FROM {c.work}
    )
    -- Start by assigning all barriers on the same mainstem_id to a group
    UPDATE {c.work} a SET group_id = m.group_id
        FROM mainstems m WHERE m.mainstem_id = a.mainstem_id;
    """


def sql_group_loop(c):
    """Iteratively split each mainstem into gain-maximizing groups: one set-based
    UPDATE per iteration using a MATERIALIZED CTE + running average, terminating
    when GET DIAGNOSTICS ROW_COUNT = 0."""
    return f"""
    -- Iterate over barriers on a mainstem, from mouth upstream, and cut each group
    -- at the barrier that maximizes the running average gain per barrier.
    DO $$
    DECLARE
        v_iteration  BIGINT := 0;
        v_grp_offset BIGINT;
        v_updated    BIGINT;
    BEGIN
        SELECT COUNT(*)::BIGINT * 10 INTO v_grp_offset FROM {c.work};
        LOOP
            v_iteration := v_iteration + 1;
            WITH ranked AS MATERIALIZED (
                SELECT
                    id,
                    group_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY group_id
                        ORDER BY {c.col_upstr_count} DESC, id
                    ) AS row_num,
                    AVG({c.w_func_upstr}) OVER (
                        PARTITION BY group_id
                        ORDER BY {c.col_upstr_count} DESC, id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS running_average
                FROM {c.work}
                WHERE group_id < v_grp_offset
            ),
            cutoff AS (
                SELECT DISTINCT ON (group_id)
                    group_id,
                    row_num AS cutoff_row
                FROM ranked
                ORDER BY group_id, running_average DESC NULLS LAST, row_num ASC
            )
            UPDATE {c.work} AS target
            SET group_id = (target.group_id::BIGINT * v_grp_offset) + v_iteration
            FROM ranked
            JOIN cutoff ON cutoff.group_id = ranked.group_id
            WHERE target.id = ranked.id
              AND ranked.row_num <= cutoff.cutoff_row;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            EXIT WHEN v_updated = 0;
        END LOOP;
    END
    $$;
    """


def sql_group_gains(c):
    """Aggregate per-group habitat gains and per-barrier averages (all in km)."""
    return f"""
    ----------------- CALCULATE GROUP GAINS (km) -------------------------
    alter table {c.work} add column {c.gain_total} numeric;
    alter table {c.work} add column {c.gain_w_total} numeric;
    alter table {c.work} add column num_barriers_group integer;
    alter table {c.work} add column {c.gain_avg} numeric;
    alter table {c.work} add column {c.gain_w_avg} numeric;
    with temp as (
        SELECT sum({c.w_func_upstr}) AS w_sum
             , sum({c.col_func_upstr}) AS sum
             , group_id
        from {c.work}
        group by group_id
    )
    update {c.work} a
    SET {c.gain_total} = t.sum
      , {c.gain_w_total} = t.w_sum
    FROM temp t WHERE t.group_id = a.group_id;
    update {c.work} SET {c.gain_w_total} = {c.w_func_upstr}   WHERE group_id IS NULL;
    update {c.work} SET {c.gain_total}   = {c.col_func_upstr} WHERE group_id IS NULL;
    with temp as (
        SELECT count(*) AS cnt, group_id FROM {c.work} group by group_id
    )
    update {c.work} a SET num_barriers_group = t.cnt FROM temp t WHERE t.group_id = a.group_id;
    update {c.work} SET num_barriers_group = 1 WHERE group_id IS NULL;
    update {c.work} SET {c.gain_avg}   = {c.gain_total}   / num_barriers_group;
    update {c.work} SET {c.gain_w_avg} = {c.gain_w_total} / num_barriers_group;
    """


def sql_downstream_group_ids(c):
    """Record, for each barrier, the group_ids of the barriers immediately
    downstream -- the deduplicated UNION of the spawn and rear downstream id
    arrays (native uuid[], concatenated with || and expanded with unnest())."""
    return f"""
    ---------------GET DOWNSTREAM GROUP IDs----------------------------
    ALTER TABLE {c.work} ADD downstr_group_ids varchar[];
    WITH downstr_barriers AS (
        SELECT rb.id, rb.group_id, dse.downstr_id
        FROM {c.work} rb
        CROSS JOIN LATERAL (
            SELECT DISTINCT elem AS downstr_id
            FROM unnest(
                     COALESCE(rb.{c.col_downstr_ids_spawn}, '{{}}'::uuid[])
                  || COALESCE(rb.{c.col_downstr_ids_rear},  '{{}}'::uuid[])
                 ) AS elem
        ) dse
    ),
    downstr_group AS (
        SELECT db_.id, db_.group_id AS current_group, db_.downstr_id, rb.group_id
        FROM downstr_barriers AS db_
        JOIN {c.work} rb ON rb.id = db_.downstr_id
        WHERE db_.group_id != rb.group_id
    ),
    dg_arrays AS (
        SELECT dg.id, ARRAY_AGG(DISTINCT dg.group_id)::varchar[] AS downstr_group_ids
        FROM downstr_group dg
        GROUP BY dg.id
    )
    UPDATE {c.work}
    SET downstr_group_ids = dg_arrays.downstr_group_ids
    FROM dg_arrays
    WHERE {c.work}.id = dg_arrays.id;
    """


def sql_assign_ranks(c):
    """Immediate, potential, and combined ranks (thresholds in km). Unranked-able
    barriers (no group, or zero weighted gain/habitat) are deliberately left NULL."""
    return f"""
    ----------------- ASSIGN RANK ID  -------------------------
    -- 1) Immediate gain: tier by number of downstream barriers, then weighted avg gain
    ALTER TABLE {c.work} ADD rank_w_avg_gain_tiered numeric;
    WITH sorted AS (
        SELECT id, group_id, {c.col_upstr_count}, {c.col_downstr_count},
               {c.gain_w_total}, {c.gain_w_avg},
               {c.out_passability_spawn},
               ROW_NUMBER() OVER(ORDER BY COALESCE({c.col_downstr_count}, 0),
                                 {c.gain_w_avg} DESC) AS row_num
        FROM {c.work}
        WHERE {c.gain_w_avg} >= {MIN_AVG_GAIN_KM}
        UNION ALL
        -- groups blocking < min_avg_gain_km of habitat are moved to the bottom
        SELECT id, group_id, {c.col_upstr_count}, {c.col_downstr_count},
               {c.gain_w_total}, {c.gain_w_avg},
               {c.out_passability_spawn},
               (SELECT MAX(row_num) FROM (
                   SELECT ROW_NUMBER() OVER(ORDER BY COALESCE({c.col_downstr_count}, 0),
                                            {c.gain_w_avg} DESC) AS row_num
                   FROM {c.work}
                   WHERE {c.gain_w_avg} >= {MIN_AVG_GAIN_KM}
               ) AS subquery)
               + ROW_NUMBER() OVER(ORDER BY COALESCE({c.col_downstr_count}, 0),
                                   {c.gain_w_avg} DESC) AS row_num
        FROM {c.work}
        WHERE {c.gain_w_avg} < {MIN_AVG_GAIN_KM}
    ),
    ranks AS (
        SELECT id
             , FIRST_VALUE(row_num) OVER(PARTITION BY group_id
                    ORDER BY COALESCE({c.col_downstr_count}, 0)) AS ranks
        FROM sorted
        ORDER BY group_id, COALESCE({c.col_downstr_count}, 0), {c.gain_w_avg} DESC
    )
    UPDATE {c.work} SET rank_w_avg_gain_tiered = ranks.ranks
    FROM ranks
    WHERE {c.work}.id = ranks.id
      -- guardrail: leave zero-gain / null-group barriers unranked
      AND {c.work}.group_id IS NOT NULL
      AND {c.work}.{c.gain_w_avg} != 0;
    -- 2) Potential gain: total weighted upstream habitat (km)
    ALTER TABLE {c.work} ADD rank_w_total_upstr_{c.lifecycle}_hab numeric;
    WITH sorted AS (
        SELECT id, group_id, {c.col_upstr_count},
               COALESCE({c.col_downstr_count}, 0) AS {c.col_downstr_count},
               {c.w_total_upstr}, {c.gain_w_total}, {c.gain_w_avg},
               ROW_NUMBER() OVER(ORDER BY {c.w_total_upstr} DESC) AS row_num
        FROM {c.work}
    ),
    ranks AS (
        SELECT id, group_id,
               FIRST_VALUE(row_num) OVER(PARTITION BY group_id ORDER BY row_num) AS relative_rank
        FROM sorted
    ),
    densify AS (
        SELECT id, DENSE_RANK() OVER(ORDER BY relative_rank) AS ranks FROM ranks
    )
    UPDATE {c.work} SET rank_w_total_upstr_{c.lifecycle}_hab = densify.ranks
    FROM densify
    WHERE {c.work}.id = densify.id
      -- guardrail: leave null-group barriers, and barriers with no weighted gain and
      -- no weighted upstream habitat, unranked
      AND {c.work}.group_id IS NOT NULL
      AND ({c.work}.{c.gain_w_total} != 0 OR {c.work}.{c.w_total_upstr} != 0);
    -- 3) Composite rank: immediate + potential
    ALTER TABLE {c.work} ADD rank_combined numeric;
    WITH ranks AS (
        SELECT id
             , DENSE_RANK() OVER(ORDER BY rank_w_avg_gain_tiered
                                 + rank_w_total_upstr_{c.lifecycle}_hab, group_id ASC) AS rank_composite
        FROM {c.work}
    )
    UPDATE {c.work} SET rank_combined = ranks.rank_composite
    FROM ranks
    WHERE {c.work}.id = ranks.id
      -- guardrail: leave null-group / zero-contribution barriers unranked
      AND {c.work}.group_id IS NOT NULL
      AND (({c.work}.{c.gain_w_total} != 0 AND {c.work}.{c.gain_w_avg} != 0)
           OR {c.work}.{c.w_total_upstr} != 0);
    """


def sql_finalize_output_table(c):
    """Write the slim output table: barrier_id + ONLY the ranking fields generated
    by this script, then drop the working table. Ownership/grants match the
    tracking-table convention. The barriers, tracking, and ranked tables are
    joined into a single export view downstream, so no source columns are copied.
    """
    all_roles = ", ".join(GRANT_ALL_ROLES)
    select_roles = ", ".join(GRANT_SELECT_ROLES)
    return f"""
    ----------------- BUILD SLIM RANKING OUTPUT TABLE -------------------------
    DROP TABLE IF EXISTS {c.ranked} CASCADE;
    CREATE TABLE {c.ranked} AS
    SELECT
        id AS barrier_id,
        group_id,
        num_barriers_group,
        {c.gain_total},
        {c.gain_w_total},
        {c.gain_avg},
        {c.gain_w_avg},
        downstr_group_ids,
        rank_w_avg_gain_tiered,
        rank_w_total_upstr_{c.lifecycle}_hab,
        rank_combined,
        {c.out_passability_spawn},
        {c.out_passability_rear}
    FROM {c.work};
    ALTER TABLE {c.ranked} ALTER COLUMN barrier_id SET NOT NULL;
    ALTER TABLE {c.ranked} ADD PRIMARY KEY (barrier_id);
    ALTER TABLE {c.ranked} OWNER TO {OWNER_ROLE};
    GRANT ALL ON TABLE {c.ranked} TO {all_roles};
    GRANT SELECT ON TABLE {c.ranked} TO {select_roles};
    DROP TABLE IF EXISTS {c.work} CASCADE;
    """


# Stages run in order; each is committed independently so a failure is easy to locate.
STAGES = [
    ("create working table",           sql_create_working_table),
    ("convert lengths m -> km",        sql_convert_lengths_to_km),
    ("derive stream_id_up",            sql_derive_stream_id_up),
    ("assign mainstem + seed groups",  sql_assign_mainstem_and_initial_groups),
    ("group loop (performant)",        sql_group_loop),
    ("calculate group gains",          sql_group_gains),
    ("downstream group ids",           sql_downstream_group_ids),
    ("assign ranks",                   sql_assign_ranks),
    ("finalize slim output table",     sql_finalize_output_table),
]


def build_full_sql(c):
    """Concatenate every stage into one script (used for --dry-run)."""
    parts = []
    for label, fn in STAGES:
        parts.append(f"-- ===== {label} =====")
        parts.append(fn(c).strip())
        parts.append("")
    return "\n".join(parts)


# =================================================================================
#  TRACKING-TABLE PRESENCE + VALIDATION (FK replacement)
# =================================================================================
def _tracking_table_exists(cursor, c):
    cursor.execute(
        """
        select 1
        from information_schema.tables
        where table_schema = %s
          and table_name = %s;
        """,
        (c.tracking_schema, c.tracking_table_name),
    )
    return cursor.fetchone() is not None


def validate_tracking_barrier_ids(cursor, c):
    """Per-run replacement for the dropped foreign key: flag any tracking
    barrier_id that has no matching feature_id in the freshly-built all_barriers.
    Such rows silently fail to join during ranking, so surfacing them here is how
    a mistyped id gets caught. Returns the list of offending ids (empty if OK)."""
    cursor.execute(
        f"""
        SELECT tt.barrier_id
        FROM {c.tracking_table} tt
        LEFT JOIN {c.all_barriers} ab ON ab.{ALL_BARRIERS_KEY} = tt.barrier_id
        WHERE ab.{ALL_BARRIERS_KEY} IS NULL;
        """
    )
    bad_ids = [row[0] for row in cursor.fetchall()]
    if bad_ids:
        logger.warning(
            "%s has %d barrier_id(s) with no match in %s: %s. These rows will "
            "not participate in ranking until the ids are corrected.",
            c.tracking_table,
            len(bad_ids),
            c.all_barriers,
            ", ".join(str(b) for b in bad_ids),
        )
    else:
        logger.info("Tracking barrier_id validation passed for %s", c.tracking_table)
    return bad_ids


# =================================================================================
#  EXECUTION
# =================================================================================
def run_ranking(conn, cursor, plan, species_filter=None):
    """Rank every (species, lifecycle) pair in the plan's reporting_values,
    committing per stage. Intended to be called from run_model.py after
    create_barrier_views (reusing the run's conn/cursor), and also used by this
    module's standalone main(). Optionally restrict to one species via
    species_filter (the --species flag).

    Detects the persistent tracking table once: if present, validates its
    barrier_ids against all_barriers and folds rehab structures into the ranking;
    if absent (first run, before WCRP setup), ranks without the rehab join.
    """
    pairs = resolve_ranking_pairs(plan, species_filter)

    # Act as the OWNER_ROLE so the ranked/_work tables (created in the OWNER_ROLE-
    # owned <code>_wcrp schema) and the finalize-stage OWNER TO succeed even when
    # the connecting user is only a granted member of the role (no password needed
    # for SET ROLE). RESET afterwards because run_model.py shares this connection
    # across phases, so the role change must not leak past ranking.
    cursor.execute(f"set role {quote_ident(OWNER_ROLE)};")
    conn.commit()
    try:
        # Tracking table is per-watershed (shared across all species/lifecycles),
        # so probe once using the first pair.
        probe_species, probe_lifecycle = pairs[0]
        probe = RankingConfig(plan, probe_species, probe_lifecycle)
        tracking_exists = _tracking_table_exists(cursor, probe)
        if tracking_exists:
            validate_tracking_barrier_ids(cursor, probe)
        else:
            logger.info(
                "Tracking table %s not present yet; ranking without rehab fold-in.",
                probe.tracking_table,
            )

        for species, lifecycle in pairs:
            c = RankingConfig(plan, species, lifecycle)
            c.include_rehab = tracking_exists
            logger.info(
                "Ranking barriers for %r / %r -> %s", species, lifecycle, c.ranked
            )
            for label, fn in STAGES:
                logger.info("  -> %s", label)
                cursor.execute(fn(c))
                conn.commit()
    finally:
        # Restore the connecting user's role for any later phases / reuse. Runs
        # even on error, after the caller's rollback, so the session isn't left
        # stuck as OWNER_ROLE.
        cursor.execute("reset role;")
        conn.commit()


# =================================================================================
#  ENTRY POINT (standalone reruns)
# =================================================================================
def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "plan_code", help="Plan code -- loads config/models/<plan_code>.yaml"
    )
    parser.add_argument(
        "--species",
        default=None,
        help="Only rank the (species, lifecycle) pairs for this species code "
        "(default: every species in the plan's reporting_values).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated SQL and exit without connecting to a database.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    plan = load_model_plan(args.plan_code)
    pairs = resolve_ranking_pairs(plan, args.species)

    logger.info(
        "Plan %r -> schema %r; ranking (species, lifecycle) pairs: %s",
        plan["code"],
        plan["output_schema"],
        pairs,
    )

    if args.dry_run:
        for species, lifecycle in pairs:
            c = RankingConfig(plan, species, lifecycle)  # include_rehab True -> full SQL
            print(
                f"\n{'=' * 80}\n-- SQL for {species!r} / {lifecycle!r}  "
                f"(output table: {c.ranked})\n{'=' * 80}"
            )
            print(build_full_sql(c))
        return

    require_env()
    conn = db_connect()
    try:
        with conn.cursor() as cursor:
            run_ranking(conn, cursor, plan, args.species)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    logger.info("Done!")


if __name__ == "__main__":
    main()
