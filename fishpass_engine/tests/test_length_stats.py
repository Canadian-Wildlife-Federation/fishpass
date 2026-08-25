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
			"es": {eid: gs.ACCESSIBILITY_ACCESSIBLE for eid in self.edge_ids},
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
		self.assertEqual(result["es"]["upstream_accessible_length"]["E3"], 10 + 20 + 5)
		self.assertEqual(result["es"]["upstream_accessible_length"]["E4"], 10 + 20 + 5 + 3)

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
		self.assertAlmostEqual(result["es"]["rear_weighted_upstream_length"]["E3"], 5 + 10 + 4)

	def test_weighted_length_uses_spawning_weight_for_spawn_lifecycle(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawn")],
			self.downstream_first_barrier_passability,
		)
		# spawning weight: E1 (order 1, weight 0.6): 10*0.6=6; E3 (order 2, weight 0.9): 5*0.9=4.5
		# (E2 excluded, not spawn habitat)
		self.assertAlmostEqual(result["es"]["spawn_weighted_upstream_length"]["E3"], 6 + 0 + 4.5)

	def test_no_weighted_length_for_spawnrear_lifecycle(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawnrear")],
			self.downstream_first_barrier_passability,
		)
		self.assertNotIn("spawnrear_weighted_upstream_length", result["es"])
		self.assertNotIn("spawnrear_functional_weighted_upstream_length", result["es"])

	def test_functional_length_resets_at_barrier(self):
		barrier_here = {
			"es": {"natural": {**{eid: 0 for eid in self.edge_ids}, "E1": 1}, "anthro": {eid: 0 for eid in self.edge_ids}},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
			self.downstream_first_barrier_passability,
		)
		# E1 is a barrier at its own start (no predecessors so no difference for E1 itself),
		# but E1's own length should still count toward E3's functional total
		self.assertEqual(result["es"]["rear_functional_upstream_length"]["E3"], 5 + 10 + 20)

	def test_weighted_length_not_masked_by_habitat(self):
		# E2 is not spawn habitat, but weighted_length is a raw per-edge value, unlike
		# spawn_weighted_upstream_length -- it should still be nonzero for E2.
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawn")],
			self.downstream_first_barrier_passability,
		)
		# E2 (order 1, spawning weight 0.6): 20*0.6=12, multiplier 1.0 (no downstream barriers)
		self.assertAlmostEqual(result["es"]["spawn_weighted_length"]["E2"], 20 * 0.6)

	def test_weighted_length_degraded_by_first_downstream_barrier_passability(self):
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
		# E1 (order 1, rearing weight 0.5): 10*0.5=5, degraded by its first downstream barrier's 0.25 passability
		self.assertAlmostEqual(result["es"]["rear_weighted_length"]["E1"], 10 * 0.5 * 0.25)
		# E3 unaffected -- its own first-downstream-barrier passability is untouched
		self.assertAlmostEqual(result["es"]["rear_weighted_length"]["E3"], 5 * 0.8 * 1.0)

	def test_weighted_upstream_length_reflects_downstream_degradation(self):
		# weighted upstream aggregates now sum the already-degraded per-edge weighted_length, not
		# an undegraded effective_length * strahler_weight value.
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
		# E1 degraded: 10*0.5*0.25=1.25; E2 undegraded: 20*0.5=10; E3 undegraded: 5*0.8=4
		self.assertAlmostEqual(result["es"]["rear_weighted_upstream_length"]["E3"], 1.25 + 10 + 4)

	def test_only_nearest_downstream_barrier_degrades_weighted_length(self):
		# E1's nearest downstream barrier is at E3 (0.5); a further barrier at E4 (0.5) must not
		# also be multiplied in -- unlike the old product-of-all-downstream-barriers formula.
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
		self.assertAlmostEqual(result["es"]["rear_weighted_length"]["E1"], 10 * 0.5 * 0.5)


class ComputeBarrierUpstreamDownstreamStatsTests(unittest.TestCase):
	def test_excludes_barriers_own_position_from_upstream_count(self):
		barriers = [{"id": "b1", "edge_id": "E3", "structure_type": "anthropogenic"}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawnrear_count": {"E3": 0, "E4": 0},
				"upstream_anthro_spawnrear_count": {"E3": 1, "E4": 1},
				"downstream_natural_spawnrear_count": {"E3": 0},
				"downstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_ids": {"E3": []},
				"downstream_anthro_ids": {"E3": []},
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

	def test_lifestage_specific_counts_exclude_own_position_upstream(self):
		barriers = [{"id": "b1", "edge_id": "E3", "structure_type": "natural"}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawn_count": {"E3": 1, "E4": 1},
				"upstream_natural_rear_count": {"E3": 0, "E4": 0},
				"downstream_natural_spawn_count": {"E3": 0},
				"downstream_natural_rear_count": {"E3": 0},
				"downstream_natural_ids": {"E3": []},
				"downstream_anthro_ids": {"E3": []},
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

	def test_length_fields_taken_as_is_at_snapped_edge(self):
		barriers = [{"id": "b1", "edge_id": "E3", "structure_type": "natural"}]
		barrier_stats = {
			"es": {
				"upstream_natural_spawnrear_count": {"E3": 0},
				"upstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_spawnrear_count": {"E3": 0},
				"downstream_anthro_spawnrear_count": {"E3": 0},
				"downstream_natural_ids": {"E3": []},
				"downstream_anthro_ids": {"E3": []},
			},
		}
		barrier_here_by_species = {
			"es": {"natural": {"E3": 1}, "anthro": {"E3": 0}},
		}
		species_length_stats = {
			"es": {
				"upstream_accessible_length": {"E3": 35.0},
				"rear_upstream_length": {"E3": 35.0},
				"rear_functional_upstream_length": {"E3": 5.0},
				"rear_weighted_upstream_length": {"E3": 19.0},
				"rear_functional_weighted_upstream_length": {"E3": 4.0},
			},
		}
		result = ls.compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species, species_length_stats)
		self.assertEqual(result["b1"]["es"]["upstream_accessible_length"], 35.0)
		self.assertEqual(result["b1"]["es"]["rear_upstream_length"], 35.0)
		self.assertEqual(result["b1"]["es"]["rear_functional_upstream_length"], 5.0)
		self.assertEqual(result["b1"]["es"]["rear_weighted_upstream_length"], 19.0)
		self.assertEqual(result["b1"]["es"]["rear_functional_weighted_upstream_length"], 4.0)


if __name__ == "__main__":
	unittest.main()
