"""Tests for fishpass_engine/scripts/length_stats.py on the confluence test network:

    E1 (headwater, N1->N3) --\\
                               E3 (N3->N4) -- E4 (N4->N5, outlet)
    E2 (headwater, N2->N3) --/

Run with: python -m unittest fishpass_engine.tests.test_length_stats
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import graph_stats as gs  # noqa: E402
import length_stats as ls  # noqa: E402


def make_edges():
	return [
		{"id": "E1", "from_nexus_id": "N1", "to_nexus_id": "N3"},
		{"id": "E2", "from_nexus_id": "N2", "to_nexus_id": "N3"},
		{"id": "E3", "from_nexus_id": "N3", "to_nexus_id": "N4"},
		{"id": "E4", "from_nexus_id": "N4", "to_nexus_id": "N5"},
	]


SPECIES_PARAMS = {
	"es": {
		"stream_order_1_rearing_weight": 0.5, "stream_order_2_rearing_weight": 0.8,
		"stream_order_1_spawning_weight": 0.6, "stream_order_2_spawning_weight": 0.9,
	},
	"wl": {
		"stream_order_1_rearing_weight": 0.25, "stream_order_2_rearing_weight": None,
		"stream_order_1_spawning_weight": 0.15, "stream_order_2_spawning_weight": None,
	},
}


class ComputeSpeciesLengthStatsTests(unittest.TestCase):
	def setUp(self):
		self.edges = make_edges()
		self.edge_ids = [e["id"] for e in self.edges]
		self.successor, self.predecessors, self.roots = gs.build_graph(self.edges)
		self.order_up = gs.upstream_order(self.predecessors, self.roots)
		self.effective_length = {"E1": 10.0, "E2": 20.0, "E3": 5.0, "E4": 3.0}
		self.strahler_order = {"E1": 1, "E2": 1, "E3": 2, "E4": 2}
		self.accessibility = {
			"es": {
				"spawn": {eid: gs.ACCESSIBILITY_ACCESSIBLE for eid in self.edge_ids},
				"rear": {eid: gs.ACCESSIBILITY_ACCESSIBLE for eid in self.edge_ids},
			},
		}
		self.habitat = {
			"es": {
				"rear": {eid: True for eid in self.edge_ids},
				"spawn": {"E1": True, "E2": False, "E3": True, "E4": True},
				"spawnrear": {eid: True for eid in self.edge_ids},
			},
		}
		self.barrier_here = {
			"es": {"natural": {eid: 0 for eid in self.edge_ids}, "anthro": {eid: 0 for eid in self.edge_ids}},
		}
		self.downstream_first_barrier_passability = {
			"es": {"rear": {eid: 1.0 for eid in self.edge_ids}, "spawn": {eid: 1.0 for eid in self.edge_ids}},
		}

	def test_upstream_accessible_length_sums_effective_length(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			self.downstream_first_barrier_passability,
		)
		self.assertEqual(result["es"]["spawn_upstream_accessible_length"]["E3"], 10 + 20 + 5)
		self.assertEqual(result["es"]["spawn_upstream_accessible_length"]["E4"], 10 + 20 + 5 + 3)
		self.assertEqual(result["es"]["rear_upstream_accessible_length"]["E3"], 10 + 20 + 5)
		self.assertEqual(result["es"]["rear_upstream_accessible_length"]["E4"], 10 + 20 + 5 + 3)

	def test_upstream_rear_length_all_habitat(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			self.downstream_first_barrier_passability,
		)
		self.assertEqual(result["es"]["rear_upstream_length"]["E4"], 10 + 20 + 5 + 3)

	def test_upstream_spawn_length_excludes_non_habitat_edge(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawn")],
			self.downstream_first_barrier_passability,
		)
		# E2 is not spawn habitat, so its length is excluded from the sum
		self.assertEqual(result["es"]["spawn_upstream_length"]["E4"], 10 + 0 + 5 + 3)

	def test_weighted_length_uses_strahler_order_weight(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			self.downstream_first_barrier_passability,
		)
		# rearing weight: E1 (order 1, weight 0.5): 10*0.5=5; E2 (order 1): 20*0.5=10; E3 (order 2, weight 0.8): 5*0.8=4
		self.assertAlmostEqual(result["es"]["rear_weighted_upstream_length_base"]["E3"], 5 + 10 + 4)

	def test_weighted_length_uses_spawning_weight_for_spawn_lifecycle(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawn")],
			self.downstream_first_barrier_passability,
		)
		# spawning weight: E1 (order 1, weight 0.6): 10*0.6=6; E3 (order 2, weight 0.9): 5*0.9=4.5
		# (E2 excluded, not spawn habitat)
		self.assertAlmostEqual(result["es"]["spawn_weighted_upstream_length_base"]["E3"], 6 + 0 + 4.5)

	def test_no_raw_weighted_length_for_spawnrear_lifecycle(self):
		# There's no spawnrear stream-order weight, so there's no raw per-edge spawnrear_weighted_length
		# field -- only rear/spawn get that.
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawnrear")],
			self.downstream_first_barrier_passability,
		)
		self.assertNotIn("spawnrear_weighted_length", result["es"])

	def test_spawnrear_weighted_upstream_length_is_max_of_rear_and_spawn(self):
		# spawnrear's weighted upstream aggregates sum, per edge, the maximum of that edge's rear
		# and spawn base weighted length -- computed even though "rear"/"spawn" aren't themselves
		# reported here. rearing weight 0.5/0.8 (order1/2), spawning weight 0.6/0.9 (order1/2).
		# E1 (order1): rear 10*0.5=5, spawn 10*0.6=6 -> max 6
		# E2 (order1, not spawn habitat): rear 20*0.5=10, spawn 0 -> max 10
		# E3 (order2): rear 5*0.8=4, spawn 5*0.9=4.5 -> max 4.5
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawnrear")],
			self.downstream_first_barrier_passability,
		)
		self.assertAlmostEqual(result["es"]["spawnrear_weighted_upstream_length_base"]["E3"], 6 + 10 + 4.5)
		self.assertAlmostEqual(result["es"]["spawnrear_functional_weighted_upstream_length_base"]["E3"], 6 + 10 + 4.5)

	def test_functional_length_resets_at_anthro_barrier(self):
		# E3 has predecessors E1/E2, so a barrier there is observable at E4: with a reset,
		# E4's functional total should only include E3's and E4's own length, not E1/E2's.
		barrier_here = {
			"es": {"natural": {eid: 0 for eid in self.edge_ids}, "anthro": {**{eid: 0 for eid in self.edge_ids}, "E3": 1}},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			self.downstream_first_barrier_passability,
		)
		self.assertEqual(result["es"]["rear_functional_upstream_length"]["E4"], 5 + 3)
		# the plain (non-reset) total is unaffected by the barrier
		self.assertEqual(result["es"]["rear_upstream_length"]["E4"], 10 + 20 + 5 + 3)

	def test_functional_length_does_not_reset_at_natural_barrier(self):
		# Same placement as the anthro case above, but as a natural barrier -- it must not reset.
		barrier_here = {
			"es": {"natural": {**{eid: 0 for eid in self.edge_ids}, "E3": 1}, "anthro": {eid: 0 for eid in self.edge_ids}},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			self.downstream_first_barrier_passability,
		)
		self.assertEqual(result["es"]["rear_functional_upstream_length"]["E4"], 10 + 20 + 5 + 3)

	def test_weighted_length_masked_by_habitat(self):
		# E2 is not spawn habitat, so its weighted_length must be zeroed out.
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawn")],
			self.downstream_first_barrier_passability,
		)
		self.assertEqual(result["es"]["spawn_weighted_length"]["E2"], 0.0)
		# E1 is spawn habitat, so it keeps the normal formula (order 1, spawning weight 0.6): 10*0.6=6
		self.assertAlmostEqual(result["es"]["spawn_weighted_length"]["E1"], 10 * 0.6)

	def test_weighted_length_masked_by_accessibility(self):
		# E1 is rear habitat but not naturally accessible for rear, so its weighted_length must be
		# zeroed out even though it would otherwise pass the formula.
		accessibility = {
			"es": {
				"spawn": {eid: gs.ACCESSIBILITY_ACCESSIBLE for eid in self.edge_ids},
				"rear": {**{eid: gs.ACCESSIBILITY_ACCESSIBLE for eid in self.edge_ids}, "E1": gs.ACCESSIBILITY_INACCESSIBLE},
			},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			self.downstream_first_barrier_passability,
		)
		self.assertEqual(result["es"]["rear_weighted_length"]["E1"], 0.0)
		# E2 is unaffected -- still naturally accessible and rear habitat
		self.assertAlmostEqual(result["es"]["rear_weighted_length"]["E2"], 20 * 0.5)

	def test_spawn_and_rear_weighted_length_masked_independently_by_their_own_accessibility(self):
		# spawn_accessibility inaccessible at E1, rear_accessibility inaccessible at E2 -- each
		# lifecycle's weighted_length should be zeroed only by its own accessibility flag, not the
		# other's (this is the bug the spawn/rear split fixes: previously both shared one flag).
		accessibility = {
			"es": {
				"spawn": {**{eid: gs.ACCESSIBILITY_ACCESSIBLE for eid in self.edge_ids}, "E1": gs.ACCESSIBILITY_INACCESSIBLE},
				"rear": {**{eid: gs.ACCESSIBILITY_ACCESSIBLE for eid in self.edge_ids}, "E2": gs.ACCESSIBILITY_INACCESSIBLE},
			},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawn"), ("es", "rear")],
			self.downstream_first_barrier_passability,
		)
		# spawn_weighted_length zeroed at E1 (spawn-inaccessible), but E2 already 0 (not spawn habitat)
		self.assertEqual(result["es"]["spawn_weighted_length"]["E1"], 0.0)
		# rear_weighted_length unaffected at E1 (only spawn is inaccessible there)
		self.assertAlmostEqual(result["es"]["rear_weighted_length"]["E1"], 10 * 0.5)
		# rear_weighted_length zeroed at E2 (rear-inaccessible)
		self.assertEqual(result["es"]["rear_weighted_length"]["E2"], 0.0)
		# spawn_weighted_length at E1 stays 0 regardless, but confirm rear's flag didn't leak into
		# spawn by checking E3, which is untouched by either override
		self.assertAlmostEqual(result["es"]["spawn_weighted_length"]["E3"], 5 * 0.9)

	def test_weighted_length_is_undegraded_by_downstream_barrier_passability(self):
		# <lc>_weighted_length is now the base quantity -- no barrier degradation at all, regardless
		# of downstream barrier passability.
		downstream_first_barrier_passability = {
			"es": {
				"rear": {**{eid: 1.0 for eid in self.edge_ids}, "E1": 0.25},
				"spawn": {eid: 1.0 for eid in self.edge_ids},
			},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			downstream_first_barrier_passability,
		)
		# E1 (order 1, rearing weight 0.5): 10*0.5=5, unaffected by its first downstream barrier's 0.25 passability
		self.assertAlmostEqual(result["es"]["rear_weighted_length"]["E1"], 10 * 0.5)
		self.assertAlmostEqual(result["es"]["rear_weighted_length"]["E3"], 5 * 0.8)

	def test_weighted_connected_and_disconnected_length_split_by_first_downstream_barrier_passability(self):
		downstream_first_barrier_passability = {
			"es": {
				"rear": {**{eid: 1.0 for eid in self.edge_ids}, "E1": 0.25},
				"spawn": {eid: 1.0 for eid in self.edge_ids},
			},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			downstream_first_barrier_passability,
		)
		# E1 base = 10*0.5=5; connected = 5*0.25=1.25; disconnected = 5*0.75=3.75
		self.assertAlmostEqual(result["es"]["rear_weighted_connected_length"]["E1"], 5 * 0.25)
		self.assertAlmostEqual(result["es"]["rear_weighted_disconnected_length"]["E1"], 5 * 0.75)
		# E3 unaffected -- its own first-downstream-barrier passability is 1.0, so fully connected
		self.assertAlmostEqual(result["es"]["rear_weighted_connected_length"]["E3"], 5 * 0.8 * 1.0)
		self.assertAlmostEqual(result["es"]["rear_weighted_disconnected_length"]["E3"], 0.0)

	def test_weighted_upstream_length_base_is_undegraded_by_downstream_barrier_passability(self):
		# the base upstream-length sum consumed by compute_barrier_upstream_downstream_stats sums
		# the undegraded per-edge base weighted length, not one already split by any downstream
		# barrier passability.
		downstream_first_barrier_passability = {
			"es": {
				"rear": {**{eid: 1.0 for eid in self.edge_ids}, "E1": 0.25},
				"spawn": {eid: 1.0 for eid in self.edge_ids},
			},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			downstream_first_barrier_passability,
		)
		# E1: 10*0.5=5; E2: 20*0.5=10; E3: 5*0.8=4 -- none degraded
		self.assertAlmostEqual(result["es"]["rear_weighted_upstream_length_base"]["E3"], 5 + 10 + 4)

	def test_only_nearest_downstream_barrier_splits_weighted_connected_length(self):
		# E1's nearest downstream barrier is at E3 (0.5); a further barrier at E4 (0.5) must not
		# also be multiplied in -- unlike a product-of-all-downstream-barriers formula.
		downstream_first_barrier_passability = {
			"es": {
				"rear": {**{eid: 1.0 for eid in self.edge_ids}, "E1": 0.5, "E2": 0.5},
				"spawn": {eid: 1.0 for eid in self.edge_ids},
			},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			downstream_first_barrier_passability,
		)
		self.assertAlmostEqual(result["es"]["rear_weighted_connected_length"]["E1"], 10 * 0.5 * 0.5)


class ComputeBarrierUpstreamDownstreamStatsTests(unittest.TestCase):
	def test_excludes_barriers_own_position_from_upstream_count(self):
		barriers = [{"id": "b1", "edge_id": "E3", "structure_type": "anthropogenic"}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawnrear_count": {"E3": 0, "E4": 0},
				"upstream_anthro_spawnrear_count": {"E3": 1, "E4": 1},
				"downstream_natural_spawnrear_count": {"E3": 0},
				"downstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawn_ids": {"E3": []},
				"downstream_natural_rear_ids": {"E3": []},
				"downstream_anthro_spawn_ids": {"E3": []},
				"downstream_anthro_rear_ids": {"E3": []},
			},
		}
		barrier_here_by_species = {
			"es": {"natural": {"E3": 0}, "anthro": {"E3": 1}},
		}
		species_length_stats = {"es": {}}
		result = ls.compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species, species_length_stats)
		# upstream count at E3 was 1 (including this barrier's own self-flag) -> excluding self = 0
		self.assertEqual(result["b1"]["es"]["upstream_anthro_spawnrear_count"], 0)
		self.assertEqual(result["b1"]["es"]["downstream_anthro_spawnrear_count"], 0)

	def test_upstream_anthro_ids_excludes_own_id_but_keeps_others(self):
		barriers = [{"id": "b1", "edge_id": "E3", "structure_type": "anthropogenic"}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawnrear_count": {"E3": 0},
				"upstream_anthro_spawnrear_count": {"E3": 2},
				"downstream_natural_spawnrear_count": {"E3": 0},
				"downstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawn_ids": {"E3": []},
				"downstream_natural_rear_ids": {"E3": []},
				"downstream_anthro_spawn_ids": {"E3": []},
				"downstream_anthro_rear_ids": {"E3": []},
				# "E3"'s accumulated upstream_anthro_*_ids includes this barrier's own id (b1,
				# appended at "here") plus one further upstream barrier (b0), for both lifestages.
				"upstream_anthro_spawn_ids": {"E3": ["b0", "b1"]},
				"upstream_anthro_rear_ids": {"E3": ["b0", "b1"]},
			},
		}
		barrier_here_by_species = {
			"es": {"natural": {"E3": 0}, "anthro": {"E3": 1}},
		}
		species_length_stats = {"es": {}}
		result = ls.compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species, species_length_stats)
		self.assertEqual(result["b1"]["es"]["upstream_anthro_spawn_ids"], ["b0"])
		self.assertEqual(result["b1"]["es"]["upstream_anthro_rear_ids"], ["b0"])

	def test_lifestage_specific_counts_exclude_own_position_upstream(self):
		barriers = [{"id": "b1", "edge_id": "E3", "structure_type": "natural"}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawn_count": {"E3": 1, "E4": 1},
				"upstream_natural_rear_count": {"E3": 0, "E4": 0},
				"downstream_natural_spawn_count": {"E3": 0},
				"downstream_natural_rear_count": {"E3": 0},
				"downstream_natural_spawn_ids": {"E3": []},
				"downstream_natural_rear_ids": {"E3": []},
				"downstream_anthro_spawn_ids": {"E3": []},
				"downstream_anthro_rear_ids": {"E3": []},
			},
		}
		barrier_here_by_species = {
			"es": {"natural_spawn": {"E3": 1}, "natural_rear": {"E3": 0}},
		}
		species_length_stats = {"es": {}}
		result = ls.compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species, species_length_stats)
		# upstream_natural_spawn_count at E3 was 1 (this barrier's own spawn-impassable flag) -> excluding self = 0
		self.assertEqual(result["b1"]["es"]["upstream_natural_spawn_count"], 0)
		self.assertEqual(result["b1"]["es"]["upstream_natural_rear_count"], 0)
		self.assertEqual(result["b1"]["es"]["downstream_natural_spawn_count"], 0)

	def test_length_fields_taken_as_is_at_upstream_edge(self):
		barriers = [{
			"id": "b1", "edge_id": "E3", "upstream_edge_id": "E2", "structure_type": "natural",
			"species_passability_value": {"es_spawn": 0.4, "es_rear": 0.4},
		}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawnrear_count": {"E3": 0},
				"upstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawnrear_count": {"E3": 0},
				"downstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawn_ids": {"E3": []},
				"downstream_natural_rear_ids": {"E3": []},
				"downstream_anthro_spawn_ids": {"E3": []},
				"downstream_anthro_rear_ids": {"E3": []},
			},
		}
		barrier_here_by_species = {
			"es": {"natural": {"E3": 1}, "anthro": {"E3": 0}},
		}
		species_length_stats = {
			"es": {
				"spawn_upstream_accessible_length": {"E2": 30.0},
				"rear_upstream_accessible_length": {"E2": 35.0},
				"rear_upstream_length": {"E2": 35.0},
				"rear_functional_upstream_length": {"E2": 5.0},
				"rear_weighted_upstream_length_base": {"E2": 19.0},
				"rear_functional_weighted_upstream_length_base": {"E2": 4.0},
			},
		}
		result = ls.compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species, species_length_stats)
		self.assertEqual(result["b1"]["es"]["spawn_upstream_accessible_length"], 30.0)
		self.assertEqual(result["b1"]["es"]["rear_upstream_accessible_length"], 35.0)
		self.assertEqual(result["b1"]["es"]["rear_upstream_length"], 35.0)
		self.assertEqual(result["b1"]["es"]["rear_functional_upstream_length"], 5.0)
		# the raw base fields are internal-only -- not copied through to the barrier's output
		self.assertNotIn("rear_weighted_upstream_length_base", result["b1"]["es"])
		self.assertNotIn("rear_functional_weighted_upstream_length_base", result["b1"]["es"])
		# derived connected/disconnected fields = base sum * this barrier's own passability (0.4) / (1 - 0.4)
		self.assertAlmostEqual(result["b1"]["es"]["rear_weighted_connected_upstream_length"], 19.0 * 0.4)
		self.assertAlmostEqual(result["b1"]["es"]["rear_weighted_disconnected_upstream_length"], 19.0 * 0.6)
		self.assertAlmostEqual(result["b1"]["es"]["rear_functional_weighted_connected_upstream_length"], 4.0 * 0.4)
		self.assertAlmostEqual(result["b1"]["es"]["rear_functional_weighted_disconnected_upstream_length"], 4.0 * 0.6)

	def test_spawnrear_upstream_length_uses_min_of_spawn_and_rear_passability(self):
		barriers = [{
			"id": "b1", "edge_id": "E3", "upstream_edge_id": "E2", "structure_type": "anthropogenic",
			"species_passability_value": {"es_spawn": 0.7, "es_rear": 0.3},
		}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawnrear_count": {"E3": 0},
				"upstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawnrear_count": {"E3": 0},
				"downstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawn_ids": {"E3": []},
				"downstream_natural_rear_ids": {"E3": []},
				"downstream_anthro_spawn_ids": {"E3": []},
				"downstream_anthro_rear_ids": {"E3": []},
			},
		}
		barrier_here_by_species = {
			"es": {"natural": {"E3": 0}, "anthro": {"E3": 1}},
		}
		species_length_stats = {
			"es": {"spawnrear_weighted_upstream_length_base": {"E2": 20.0}, "spawnrear_functional_weighted_upstream_length_base": {"E2": 10.0}},
		}
		result = ls.compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species, species_length_stats)
		# min(0.7, 0.3) = 0.3
		self.assertAlmostEqual(result["b1"]["es"]["spawnrear_weighted_connected_upstream_length"], 20.0 * 0.3)
		self.assertAlmostEqual(result["b1"]["es"]["spawnrear_weighted_disconnected_upstream_length"], 20.0 * 0.7)
		self.assertAlmostEqual(result["b1"]["es"]["spawnrear_functional_weighted_connected_upstream_length"], 10.0 * 0.3)
		self.assertAlmostEqual(result["b1"]["es"]["spawnrear_functional_weighted_disconnected_upstream_length"], 10.0 * 0.7)

	def test_upstream_length_treats_missing_passability_key_as_zero(self):
		# missing species_lifestage key on this barrier's own species_passability_value is treated
		# as 0 (full barrier), consistent with is_impassable's "missing = full barrier" convention.
		barriers = [{
			"id": "b1", "edge_id": "E3", "upstream_edge_id": "E2", "structure_type": "anthropogenic",
			"species_passability_value": {},
		}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawnrear_count": {"E3": 0},
				"upstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawnrear_count": {"E3": 0},
				"downstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawn_ids": {"E3": []},
				"downstream_natural_rear_ids": {"E3": []},
				"downstream_anthro_spawn_ids": {"E3": []},
				"downstream_anthro_rear_ids": {"E3": []},
			},
		}
		barrier_here_by_species = {
			"es": {"natural": {"E3": 0}, "anthro": {"E3": 1}},
		}
		species_length_stats = {
			"es": {"rear_weighted_upstream_length_base": {"E2": 19.0}, "rear_functional_weighted_upstream_length_base": {"E2": 4.0}},
		}
		result = ls.compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species, species_length_stats)
		self.assertAlmostEqual(result["b1"]["es"]["rear_weighted_connected_upstream_length"], 0.0)
		self.assertAlmostEqual(result["b1"]["es"]["rear_weighted_disconnected_upstream_length"], 19.0)

	def test_length_fields_none_when_upstream_edge_id_missing(self):
		barriers = [{
			"id": "b1", "edge_id": "E3", "upstream_edge_id": None, "structure_type": "natural",
			"species_passability_value": {"es_rear": 0.4},
		}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawnrear_count": {"E3": 0},
				"upstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawnrear_count": {"E3": 0},
				"downstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawn_ids": {"E3": []},
				"downstream_natural_rear_ids": {"E3": []},
				"downstream_anthro_spawn_ids": {"E3": []},
				"downstream_anthro_rear_ids": {"E3": []},
			},
		}
		barrier_here_by_species = {
			"es": {"natural": {"E3": 1}, "anthro": {"E3": 0}},
		}
		species_length_stats = {
			"es": {
				"rear_upstream_length": {"E1": 5.0, "E2": 5.0},
				"rear_weighted_upstream_length_base": {"E1": 5.0, "E2": 5.0},
				"rear_functional_weighted_upstream_length_base": {"E1": 5.0, "E2": 5.0},
			},
		}
		result = ls.compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species, species_length_stats)
		self.assertIsNone(result["b1"]["es"]["rear_upstream_length"])
		self.assertIsNone(result["b1"]["es"]["rear_weighted_connected_upstream_length"])
		self.assertIsNone(result["b1"]["es"]["rear_weighted_disconnected_upstream_length"])
		self.assertIsNone(result["b1"]["es"]["rear_functional_weighted_connected_upstream_length"])
		self.assertIsNone(result["b1"]["es"]["rear_functional_weighted_disconnected_upstream_length"])


if __name__ == "__main__":
	unittest.main()
