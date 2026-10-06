#!/usr/bin/env python3
"""Generate and run the automated barrier-ranking query for a model plan.

Builds a per-(species, lifecycle) ranked-barriers table from the plan's
<output_schema>.anthropogenic_barriers_<species> view, folding in rehabilitated
structures recorded in the WCRP tracking table, and writes a slim output table
containing ONLY the barrier id plus the ranking fields generated here (group
membership, per-group gains, and the three rank columns). The barriers,
tracking, and ranked tables are joined into a single export view afterwards
(create_combined_view.py), so nothing from the source view is duplicated into
the output.

Not a standalone script: run_ranking() is called by run_model.py after
create_barrier_views, on every model run. The WCRP tracking table is
guaranteed to exist by then -- run_model.py creates it at the start of the
run if it's missing (create_wcrp_tracking_table.ensure_tracking_table).
"""
import logging
import sys

from db import as_role, get_db_roles, quote_ident, wcrp_setting
from model_plan import IDENTIFIER_RE
from postprocess_views import barrier_length_fields

logger = logging.getLogger(__name__)

# =================================================================================
#  CONFIGURATION
# =================================================================================
# Ranking is driven by the plan's reporting_species_lifecycles -- the cached
# result of model_plan.expand_reporting_values(). One ranked table is produced
# per (species, lifecycle) pair (see resolve_ranking_pairs).
#
# Shared settings live in config/fishpass.yaml, not here:
#   * database_roles -- owner + grant roles for the ranked tables (db.get_db_roles)
#   * wcrp.min_avg_gain_km -- per-plan overridable (db.wcrp_setting)

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
# divides each by 1000 and renames it with a _km suffix. The field list is NOT
# hand-typed here -- it comes from postprocess_views.barrier_length_fields(), the
# same source that defines the view's columns (see RankingConfig.length_fields).


# =================================================================================
#  PLAN-DERIVED VALUES + IDENTIFIERS
# =================================================================================
def resolve_ranking_pairs(plan):
    """Return the (species, lifecycle) tuples this run will rank.

    This is simply the cached result of model_plan.expand_reporting_values() --
    plan['reporting_species_lifecycles']. The expansion of reporting_values
    ('<species>_<lifestage>' parsing, 'all' handling, lifecycle validation,
    species-in-target_species checks) is NOT re-done here; it lives entirely in
    model_plan.py. One ranked table is built per pair.

    Two extra guards: a clear error if there is nothing to rank (model_plan
    already rejects an empty reporting_values, so this is defense in depth), and
    a SQL-safety check on the species code (it is interpolated into column
    names, which can't be bound params).
    """
    pairs = [tuple(pair) for pair in plan.get("reporting_species_lifecycles") or []]
    if not pairs:
        sys.exit(
            f"Plan {plan.get('code')!r} has no (species, lifecycle) pairs to rank -- "
            f"reporting_values must list at least one '<species>_<lifecycle>' entry."
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

        # Every lifecycle the plan reports for this species. These decide which
        # <lc>_* length columns exist on anthropogenic_barriers_<species> (see
        # postprocess_views.create_species_barrier_views), so the km conversion
        # touches exactly the columns that are actually there.
        self.view_lifecycles = sorted(
            {lc for sp, lc in plan["reporting_species_lifecycles"] if sp == species_code}
        )
        self.length_fields = barrier_length_fields(self.view_lifecycles)

        # Per-plan settings (plan override, else config/fishpass.yaml default).
        self.min_avg_gain_km = wcrp_setting(plan, "min_avg_gain_km")
        self.roles = get_db_roles()

        # Identifiers. Every schema/table name is quote_ident()-ed: plan['code']
        # is only held to PLAN_CODE_RE, which allows a leading digit (e.g. '1ns'),
        # and an unquoted 1ns_wcrp is a SQL syntax error. *_name attributes keep
        # the RAW names for bound parameters (catalog lookups) and for building
        # derived names (index names).
        #
        # Persistent per-WCRP schema (owned by the owner role) -- holds BOTH the
        # tracking table and the ranked output/_work tables.
        self.wcrp_schema_name = f"{self.watershed}_wcrp"
        self.wcrp_schema = quote_ident(self.wcrp_schema_name)

        # Sources are READ from the ephemeral output_schema.
        self.schema_q = quote_ident(self.schema)
        self.barriers_view = (
            f"{self.schema_q}.{quote_ident(f'anthropogenic_barriers_{self.species}')}"
        )
        self.streams = f"{self.schema_q}.{quote_ident('streams')}"
        self.all_barriers = f"{self.schema_q}.{quote_ident(ALL_BARRIERS_TABLE)}"

        # Outputs are WRITTEN to the persistent WCRP schema. The lifecycle is part
        # of the name so a plan reporting multiple lifecycles for a species yields
        # one distinct ranked table per (species, lifecycle) pair.
        table_stem = f"ranked_barriers_{self.species}_{self.lifecycle}_{self.watershed}"
        self.ranked_name = table_stem
        self.work_name = f"{table_stem}_work"
        self.ranked = f"{self.wcrp_schema}.{quote_ident(self.ranked_name)}"
        self.work = f"{self.wcrp_schema}.{quote_ident(self.work_name)}"

        # Tracking table (create_wcrp_tracking_table.py). Status column is
        # species-suffixed (lifecycle-agnostic).
        self.tracking_table_name = f"tracking_table_{self.watershed}"
        self.tracking_table = (
            f"{self.wcrp_schema}.{quote_ident(self.tracking_table_name)}"
        )
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
    (strict enum match). The tracking table is guaranteed to exist --
    run_model.py creates it at the start of the run if it's missing. Length
    fields are still in METRES here; the next stage converts them to km. anthropogenic_barriers_<species>
    is a view, but SELECT ... INTO materialises the needed columns into a real
    table, so all later ALTER/UPDATE stages work normally. The passability gate
    is lifecycle-specific (see _passability_predicate): a barrier is kept only if
    it blocks the ranked lifestage(s), with rehabilitated barriers always kept.
    """
    passability_predicate = _passability_predicate(c)
    return f"""
    DROP TABLE IF EXISTS {c.work} CASCADE;
    SELECT b.*
        -- Only ADDED column(s): passability surfaced as decimal for readability.
        ,b.{c.col_passability_spawn}::decimal AS {c.out_passability_spawn}
        ,b.{c.col_passability_rear}::decimal  AS {c.out_passability_rear}
        INTO {c.work}
    FROM {c.barriers_view} b
    LEFT JOIN {c.tracking_table} tt
        ON tt.barrier_id = b.id
    WHERE (
                {passability_predicate}
             OR tt.{c.col_tracking_status} = '{REHABILITATED_STATUS}'
          )
        AND b.{c.src_hab_exists} != 0
    ORDER BY b.{c.src_func_upstr} DESC;
    ALTER TABLE IF EXISTS {c.work} ALTER COLUMN id SET NOT NULL;
    ALTER TABLE IF EXISTS {c.work} ADD COLUMN group_id numeric;
    ALTER TABLE IF EXISTS {c.work} ADD PRIMARY KEY (id);
    """


def sql_convert_lengths_to_km(c):
    """Convert every length column from metres to km and rename with a _km suffix.

    Covers exactly the length columns the source view has for this species
    (c.length_fields -- the per-species accessible lengths plus <lc>_* for each
    lifecycle the plan reports), so every length on the working table ends up in
    km and no ALTER targets a column that doesn't exist."""
    lines = [
        "    ------------- CONVERT LENGTH FIELDS FROM METRES TO KILOMETRES -------------",
        "    -- Divide each *_length column by 1000 and rename with a _km suffix so the",
        "    -- unit is explicit for all downstream stages (gains, ranks, min_avg_gain_km).",
    ]
    for f in c.length_fields:
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
    idx_mainstem = quote_ident(f"{c.work_name}_idx_mainstem")
    idx_group_id = quote_ident(f"{c.work_name}_idx_group_id")
    idx_id = quote_ident(f"{c.work_name}_idx_id")
    return f"""
    --TO FIX: some group_ids need to get combined - e.g., multiple branches of river
    ALTER TABLE {c.work} ADD COLUMN IF NOT EXISTS mainstem_id uuid;
    UPDATE {c.work} SET mainstem_id = t.mainstem_id
        FROM {c.streams} t WHERE t.id = stream_id_up;
    CREATE INDEX IF NOT EXISTS {idx_mainstem} ON {c.work} (mainstem_id);
    CREATE INDEX IF NOT EXISTS {idx_group_id} ON {c.work} (group_id);
    CREATE INDEX IF NOT EXISTS {idx_id}       ON {c.work} (id);
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
        WHERE {c.gain_w_avg} >= {c.min_avg_gain_km}
        UNION ALL
        -- groups blocking < min_avg_gain_km of habitat are moved to the bottom
        SELECT id, group_id, {c.col_upstr_count}, {c.col_downstr_count},
               {c.gain_w_total}, {c.gain_w_avg},
               {c.out_passability_spawn},
               (SELECT MAX(row_num) FROM (
                   SELECT ROW_NUMBER() OVER(ORDER BY COALESCE({c.col_downstr_count}, 0),
                                            {c.gain_w_avg} DESC) AS row_num
                   FROM {c.work}
                   WHERE {c.gain_w_avg} >= {c.min_avg_gain_km}
               ) AS subquery)
               + ROW_NUMBER() OVER(ORDER BY COALESCE({c.col_downstr_count}, 0),
                                   {c.gain_w_avg} DESC) AS row_num
        FROM {c.work}
        WHERE {c.gain_w_avg} < {c.min_avg_gain_km}
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
    owner = quote_ident(c.roles["owner"])
    grants = "".join(
        f"\n    GRANT ALL ON TABLE {c.ranked} TO {quote_ident(r)};"
        for r in c.roles["grant_all"]
    ) + "".join(
        f"\n    GRANT SELECT ON TABLE {c.ranked} TO {quote_ident(r)};"
        for r in c.roles["grant_select"]
    )
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
    ALTER TABLE {c.ranked} OWNER TO {owner};{grants}
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


# =================================================================================
#  TRACKING-TABLE VALIDATION (FK replacement)
# =================================================================================
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
def run_ranking(conn, cursor, plan):
    """Rank every (species, lifecycle) pair in the plan's reporting_values,
    committing per stage. Called from run_model.py after create_barrier_views,
    reusing the run's conn/cursor.

    Validates the tracking table's barrier_ids against all_barriers once (it is
    per-watershed, shared across all species/lifecycles), then ranks each pair
    with rehabilitated structures folded in.
    """
    pairs = resolve_ranking_pairs(plan)
    owner = get_db_roles()["owner"]

    # Act as the owner role so the ranked/_work tables (created in the owner-
    # owned <code>_wcrp schema) and the finalize-stage OWNER TO succeed even when
    # the connecting user is only a granted member of the role. as_role() rolls
    # back + RESETs afterwards (even on error) because run_model.py shares this
    # connection across phases, so the role change must not leak past ranking.
    with as_role(conn, cursor, owner):
        probe = RankingConfig(plan, *pairs[0])
        validate_tracking_barrier_ids(cursor, probe)

        for species, lifecycle in pairs:
            c = RankingConfig(plan, species, lifecycle)
            logger.info(
                "Ranking barriers for %r / %r -> %s", species, lifecycle, c.ranked
            )
            for label, fn in STAGES:
                logger.info("  -> %s", label)
                cursor.execute(fn(c))
                conn.commit()
