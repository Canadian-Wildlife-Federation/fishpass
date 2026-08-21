"""Tests for fishpass_engine/scripts/graph_stats.py -- pure graph algorithm logic on small
synthetic networks. No database.

Test network (a confluence feeding a single outlet):

    E1 (headwater, N1->N3) --\\
                               E3 (N3->N4) -- E4 (N4->N5, outlet)
    E2 (headwater, N2->N3) --/

Run with: python -m unittest fishpass_engine.tests.test_graph_stats
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import graph_stats as gs  # noqa: E402


def make_confluence_edges():
	return [
		{"id": "E1", "from_nexus_id": "N1", "to_nexus_id": "N3"},
		{"id": "E2", "from_nexus_id": "N2", "to_nexus_id": "N3"},
		{"id": "E3", "from_nexus_id": "N3", "to_nexus_id": "N4"},
		{"id": "E4", "from_nexus_id": "N4", "to_nexus_id": "N5"},
	]


class BuildGraphTests(unittest.TestCase):
	def test_successor_and_predecessors(self):
		successor, predecessors, roots = gs.build_graph(make_confluence_edges())
		self.assertEqual(successor["E1"], "E3")
		self.assertEqual(successor["E2"], "E3")
		self.assertEqual(successor["E3"], "E4")
		self.assertIsNone(successor["E4"])
		self.assertEqual(sorted(predecessors["E3"]), ["E1", "E2"])
		self.assertEqual(predecessors["E4"], ["E3"])
		self.assertEqual(predecessors["E1"], [])
		self.assertEqual(roots, ["E4"])


class OrderingTests(unittest.TestCase):
	def test_upstream_order_respects_predecessors(self):
		successor, predecessors, roots = gs.build_graph(make_confluence_edges())
		order = gs.upstream_order(predecessors, roots)
		self.assertLess(order.index("E1"), order.index("E3"))
		self.assertLess(order.index("E2"), order.index("E3"))
		self.assertLess(order.index("E3"), order.index("E4"))

	def test_downstream_order_is_reverse(self):
		successor, predecessors, roots = gs.build_graph(make_confluence_edges())
		up = gs.upstream_order(predecessors, roots)
		down = gs.downstream_order(up)
		self.assertEqual(down, list(reversed(up)))
		self.assertEqual(down[0], "E4")


class PropagateTests(unittest.TestCase):
	def setUp(self):
		self.successor, self.predecessors, self.roots = gs.build_graph(make_confluence_edges())
		self.order_up = gs.upstream_order(self.predecessors, self.roots)
		self.order_down = gs.downstream_order(self.order_up)

	def test_propagate_upstream_sums_lengths(self):
		length = {"E1": 10, "E2": 20, "E3": 5, "E4": 3}
		acc = gs.propagate_upstream(self.order_up, self.predecessors, length, lambda a, b: a + b, 0)
		self.assertEqual(acc["E1"], 10)
		self.assertEqual(acc["E2"], 20)
		self.assertEqual(acc["E3"], 5 + 10 + 20)
		self.assertEqual(acc["E4"], 3 + 35)

	def test_propagate_downstream_counts_barrier_at_confluence(self):
		barrier_here = {"E3": 1}
		down = gs.propagate_downstream(self.order_down, self.successor, barrier_here, lambda a, b: a + b, 0)
		self.assertEqual(down["E4"], 0)  # root: nothing downstream of the outlet
		self.assertEqual(down["E3"], 0)  # barrier is at E3's own start, not downstream of E3
		self.assertEqual(down["E1"], 1)  # barrier at E3 is between E1 and the outlet
		self.assertEqual(down["E2"], 1)

	def test_propagate_upstream_counts_barrier_at_confluence(self):
		barrier_here = {"E3": 1}
		up = gs.propagate_upstream(self.order_up, self.predecessors, barrier_here, lambda a, b: a + b, 0)
		self.assertEqual(up["E1"], 0)
		self.assertEqual(up["E2"], 0)
		self.assertEqual(up["E3"], 1)  # barrier at its own start counts as upstream of itself
		self.assertEqual(up["E4"], 1)  # and upstream of everything further downstream

	def test_propagate_upstream_with_reset_own_length_still_counts(self):
		# a barrier at E1's own start blocks E1 from reaching further upstream, but E1 itself
		# is still reachable from E3 (the barrier isn't between E1 and E3) -- so E1's own
		# length should still be included in E3's functional total.
		length = {"E1": 10, "E2": 20, "E3": 5, "E4": 3}
		is_barrier = {"E1": True}
		acc = gs.propagate_upstream_with_reset(self.order_up, self.predecessors, length, is_barrier)
		self.assertEqual(acc["E1"], 10)  # E1's own functional length is just its own length
		self.assertEqual(acc["E2"], 20)
		self.assertEqual(acc["E3"], 5 + 10 + 20)  # includes E1's own length, not just E2's
		self.assertEqual(acc["E4"], 3 + (5 + 10 + 20))

	def test_propagate_upstream_with_reset_blocks_beyond_the_barrier(self):
		# extend the network with E0 upstream of E1 (E0 -> E1 -> E3), and put the barrier at
		# E1's start: E1's own length still counts toward E3, but E0's (beyond the barrier)
		# must not.
		edges = make_confluence_edges() + [{"id": "E0", "from_nexus_id": "N0", "to_nexus_id": "N1"}]
		successor, predecessors, roots = gs.build_graph(edges)
		order_up = gs.upstream_order(predecessors, roots)

		length = {"E0": 7, "E1": 10, "E2": 20, "E3": 5, "E4": 3}
		is_barrier = {"E1": True}
		acc = gs.propagate_upstream_with_reset(order_up, predecessors, length, is_barrier)

		self.assertEqual(acc["E0"], 7)
		self.assertEqual(acc["E1"], 10)  # E1's own report also stops at itself, excluding E0
		self.assertEqual(acc["E3"], 5 + 10 + 20)  # E1's own length yes, E0's (beyond the barrier) no
		self.assertEqual(acc["E4"], 3 + (5 + 10 + 20))


class PropagateMultiTests(unittest.TestCase):
	def setUp(self):
		self.successor, self.predecessors, self.roots = gs.build_graph(make_confluence_edges())
		self.order_up = gs.upstream_order(self.predecessors, self.roots)
		self.order_down = gs.downstream_order(self.order_up)

	def test_propagate_upstream_multi_matches_separate_single_field_calls(self):
		length = {"E1": 10, "E2": 20, "E3": 5, "E4": 3}
		ids = {"E3": ["b1"]}
		local_values = {eid: {"length": length.get(eid, 0), "ids": ids.get(eid, [])} for eid in length}
		acc = gs.propagate_upstream_multi(self.order_up, self.predecessors, local_values, {"length": 0, "ids": []})

		expected_length = gs.propagate_upstream(self.order_up, self.predecessors, length, lambda a, b: a + b, 0)
		expected_ids = gs.propagate_upstream(self.order_up, self.predecessors, ids, lambda a, b: a + b, [])
		for eid in length:
			self.assertEqual(acc[eid]["length"], expected_length[eid])
			self.assertEqual(acc[eid]["ids"], expected_ids[eid])

	def test_propagate_downstream_multi_matches_separate_single_field_calls(self):
		barrier_here = {"E3": 1}
		ids = {"E3": ["b1"]}
		local_values = {"E3": {"count": 1, "ids": ["b1"]}}
		acc = gs.propagate_downstream_multi(self.order_down, self.successor, local_values, {"count": 0, "ids": []})

		expected_count = gs.propagate_downstream(self.order_down, self.successor, barrier_here, lambda a, b: a + b, 0)
		expected_ids = gs.propagate_downstream(self.order_down, self.successor, ids, lambda a, b: a + b, [])
		for eid in ("E1", "E2", "E3", "E4"):
			self.assertEqual(acc[eid]["count"], expected_count[eid])
			self.assertEqual(acc[eid]["ids"], expected_ids[eid])

	def test_propagate_upstream_with_reset_multi_independent_per_field(self):
		# two fields sharing the same edges but different reset masks -- confirms one field
		# resetting at an edge doesn't affect the other field on the same edge. Extend the
		# network with E0 upstream of E1 so the reset at E1 actually excludes something
		# (E0's length) from field "a" but not field "b", making them observably diverge.
		edges = make_confluence_edges() + [{"id": "E0", "from_nexus_id": "N0", "to_nexus_id": "N1"}]
		successor, predecessors, roots = gs.build_graph(edges)
		order_up = gs.upstream_order(predecessors, roots)

		length = {"E0": 7, "E1": 10, "E2": 20, "E3": 5, "E4": 3}
		local_values = {eid: {"a": v, "b": v} for eid, v in length.items()}
		is_reset = {"E1": {"a": True, "b": False}}

		acc = gs.propagate_upstream_with_reset_multi(order_up, predecessors, local_values, is_reset, {"a": 0.0, "b": 0.0})

		expected_a = gs.propagate_upstream_with_reset(order_up, predecessors, length, {"E1": True})
		expected_b = gs.propagate_upstream_with_reset(order_up, predecessors, length, {})
		for eid in length:
			self.assertEqual(acc[eid]["a"], expected_a[eid])
			self.assertEqual(acc[eid]["b"], expected_b[eid])
		# sanity: the two fields actually diverge at E3 (downstream of the reset edge) -- field
		# "a" excludes E0's length via the reset at E1, field "b" doesn't reset so includes it.
		self.assertNotEqual(acc["E3"]["a"], acc["E3"]["b"])


class IsImpassableTests(unittest.TestCase):
	def test_fully_passable(self):
		self.assertFalse(gs.is_impassable({"es_rear": 1, "es_spawn": 1}, "es", 1.0))

	def test_fully_blocked(self):
		self.assertTrue(gs.is_impassable({"es_rear": 0, "es_spawn": 0}, "es", 1.0))

	def test_worst_of_both_lifestages(self):
		self.assertTrue(gs.is_impassable({"es_rear": 1, "es_spawn": 0}, "es", 1.0))

	def test_missing_key_treated_as_impassable(self):
		self.assertTrue(gs.is_impassable({}, "es", 1.0))

	def test_custom_threshold_partial_passability(self):
		self.assertFalse(gs.is_impassable({"es_rear": 0.5, "es_spawn": 0.5}, "es", 0.5))
		self.assertTrue(gs.is_impassable({"es_rear": 0.25, "es_spawn": 1}, "es", 0.5))


class ComputeBarrierHereTests(unittest.TestCase):
	def test_classifies_natural_vs_anthropogenic(self):
		barriers = [
			{"edge_id": "E3", "species_passability_value": {"es_rear": 0, "es_spawn": 0}, "structure_type": "natural", "id": "b1"},
			{"edge_id": "E1", "species_passability_value": {"es_rear": 0, "es_spawn": 0}, "structure_type": "anthropogenic", "id": "b2"},
		]
		result = gs.compute_barrier_here(["E1", "E2", "E3", "E4"], barriers, ["es"], 1.0)
		self.assertEqual(result["es"]["natural"]["E3"], 1)
		self.assertEqual(result["es"]["anthro"]["E1"], 1)
		self.assertEqual(result["es"]["natural_ids"]["E3"], ["b1"])
		self.assertEqual(result["es"]["anthro_ids"]["E1"], ["b2"])
		self.assertEqual(result["es"]["natural"]["E1"], 0)

	def test_passable_barrier_not_counted(self):
		barriers = [
			{"edge_id": "E3", "species_passability_value": {"es_rear": 1, "es_spawn": 1}, "structure_type": "natural", "id": "b1"},
		]
		result = gs.compute_barrier_here(["E1", "E2", "E3", "E4"], barriers, ["es"], 1.0)
		self.assertEqual(result["es"]["natural"]["E3"], 0)

	def test_barrier_outside_component_ignored(self):
		barriers = [
			{"edge_id": "not-in-this-component", "species_passability_value": {"es_rear": 0, "es_spawn": 0}, "structure_type": "natural", "id": "b1"},
		]
		result = gs.compute_barrier_here(["E1", "E2", "E3", "E4"], barriers, ["es"], 1.0)
		self.assertEqual(sum(result["es"]["natural"].values()), 0)


class ComputeBarrierStatsAndAccessibilityTests(unittest.TestCase):
	def setUp(self):
		self.edges = make_confluence_edges()
		self.successor, self.predecessors, self.roots = gs.build_graph(self.edges)
		self.order_up = gs.upstream_order(self.predecessors, self.roots)
		self.order_down = gs.downstream_order(self.order_up)
		self.edge_ids = [e["id"] for e in self.edges]

	def test_end_to_end_barrier_and_accessibility(self):
		barriers = [
			{"edge_id": "E3", "species_passability_value": {"es_rear": 0, "es_spawn": 0}, "structure_type": "anthropogenic", "id": "b1"},
		]
		barrier_here = gs.compute_barrier_here(self.edge_ids, barriers, ["es"], 1.0)
		stats = gs.compute_barrier_stats(self.order_up, self.order_down, self.predecessors, self.successor, barrier_here)

		self.assertEqual(stats["es"]["downstream_anthro_count"]["E1"], 1)
		self.assertEqual(stats["es"]["downstream_anthro_count"]["E2"], 1)
		self.assertEqual(stats["es"]["downstream_anthro_count"]["E3"], 0)
		self.assertEqual(stats["es"]["upstream_anthro_count"]["E4"], 1)
		self.assertEqual(stats["es"]["downstream_anthro_ids"]["E1"], ["b1"])

		accessibility = gs.compute_accessibility(self.edge_ids, stats)
		self.assertEqual(accessibility["es"]["E1"], gs.ACCESSIBILITY_DISCONNECTED)
		self.assertEqual(accessibility["es"]["E2"], gs.ACCESSIBILITY_DISCONNECTED)
		self.assertEqual(accessibility["es"]["E3"], gs.ACCESSIBILITY_CONNECTED)
		self.assertEqual(accessibility["es"]["E4"], gs.ACCESSIBILITY_CONNECTED)

	def test_multi_species_barriers_dont_leak_between_species(self):
		# es is blocked at E3, wl is blocked at E1 -- since compute_barrier_stats now runs both
		# species' fields through one combined pass, this confirms one species' field values
		# don't bleed into another's within that shared traversal.
		barriers = [
			{"edge_id": "E3", "species_passability_value": {"es_rear": 0, "es_spawn": 0, "wl_rear": 1, "wl_spawn": 1}, "structure_type": "anthropogenic", "id": "b_es"},
			{"edge_id": "E1", "species_passability_value": {"es_rear": 1, "es_spawn": 1, "wl_rear": 0, "wl_spawn": 0}, "structure_type": "natural", "id": "b_wl"},
		]
		barrier_here = gs.compute_barrier_here(self.edge_ids, barriers, ["es", "wl"], 1.0)
		stats = gs.compute_barrier_stats(self.order_up, self.order_down, self.predecessors, self.successor, barrier_here)

		self.assertEqual(stats["es"]["downstream_anthro_count"]["E1"], 1)
		self.assertEqual(stats["es"]["downstream_anthro_ids"]["E1"], ["b_es"])
		self.assertEqual(stats["es"]["downstream_natural_count"]["E1"], 0)
		self.assertEqual(stats["es"]["upstream_anthro_count"]["E1"], 0)  # es not blocked at E1

		self.assertEqual(stats["wl"]["upstream_natural_count"]["E3"], 1)
		self.assertEqual(stats["wl"]["upstream_natural_count"]["E4"], 1)
		self.assertEqual(stats["wl"]["downstream_natural_count"]["E1"], 0)  # barrier is at E1's own start
		self.assertEqual(stats["wl"]["downstream_anthro_count"]["E1"], 0)  # wl not blocked at E3

	def test_natural_barrier_makes_inaccessible_not_disconnected(self):
		barriers = [
			{"edge_id": "E3", "species_passability_value": {"es_rear": 0, "es_spawn": 0}, "structure_type": "natural", "id": "b1"},
		]
		barrier_here = gs.compute_barrier_here(self.edge_ids, barriers, ["es"], 1.0)
		stats = gs.compute_barrier_stats(self.order_up, self.order_down, self.predecessors, self.successor, barrier_here)
		accessibility = gs.compute_accessibility(self.edge_ids, stats)
		self.assertEqual(accessibility["es"]["E1"], gs.ACCESSIBILITY_INACCESSIBLE)

	def test_supports_species_fn_forces_inaccessible(self):
		barrier_here = gs.compute_barrier_here(self.edge_ids, [], ["es"], 1.0)
		stats = gs.compute_barrier_stats(self.order_up, self.order_down, self.predecessors, self.successor, barrier_here)
		accessibility = gs.compute_accessibility(self.edge_ids, stats, supports_species_fn=lambda sp, eid: False)
		self.assertTrue(all(v == gs.ACCESSIBILITY_INACCESSIBLE for v in accessibility["es"].values()))


class ComputeHabitatAssignmentTests(unittest.TestCase):
	def setUp(self):
		self.edge_ids = ["E1", "E2"]
		self.species_params = {
			"es": {
				"rear_gradient_min": 0.0, "rear_gradient_max": 5.0,
				"spawn_gradient_min": 0.0, "spawn_gradient_max": 2.0,
				"strahler_order_rearing_min": 1, "strahler_order_rearing_max": 4,
				"strahler_order_spawning_min": 1, "strahler_order_spawning_max": 4,
			},
		}

	def test_habitat_true_when_all_conditions_met(self):
		accessibility = {"es": {"E1": gs.ACCESSIBILITY_CONNECTED, "E2": gs.ACCESSIBILITY_INACCESSIBLE}}
		gradient = {"E1": 1.0, "E2": 1.0}
		strahler = {"E1": 2, "E2": 2}
		result = gs.compute_habitat_assignment(self.edge_ids, ["es"], accessibility, gradient, strahler, self.species_params)
		self.assertTrue(result["es"]["rear"]["E1"])
		self.assertTrue(result["es"]["spawn"]["E1"])

	def test_habitat_false_when_inaccessible(self):
		accessibility = {"es": {"E1": gs.ACCESSIBILITY_INACCESSIBLE, "E2": gs.ACCESSIBILITY_CONNECTED}}
		gradient = {"E1": 1.0, "E2": 1.0}
		strahler = {"E1": 2, "E2": 2}
		result = gs.compute_habitat_assignment(self.edge_ids, ["es"], accessibility, gradient, strahler, self.species_params)
		self.assertFalse(result["es"]["rear"]["E1"])

	def test_habitat_false_when_gradient_out_of_spawn_range(self):
		accessibility = {"es": {"E1": gs.ACCESSIBILITY_CONNECTED, "E2": gs.ACCESSIBILITY_CONNECTED}}
		gradient = {"E1": 3.0, "E2": 3.0}  # ok for rear (0-5) but not spawn (0-2)
		strahler = {"E1": 2, "E2": 2}
		result = gs.compute_habitat_assignment(self.edge_ids, ["es"], accessibility, gradient, strahler, self.species_params)
		self.assertTrue(result["es"]["rear"]["E1"])
		self.assertFalse(result["es"]["spawn"]["E1"])


if __name__ == "__main__":
	unittest.main()
