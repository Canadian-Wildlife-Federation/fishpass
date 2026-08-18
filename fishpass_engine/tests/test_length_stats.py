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
	"es": {"stream_order_1_weight": 0.5, "stream_order_2_weight": 0.8},
	"wl": {"stream_order_1_weight": 0.25, "stream_order_2_weight": None},
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
			"es": {eid: gs.ACCESSIBILITY_CONNECTED for eid in self.edge_ids},
		}
		self.habitat = {
			"es": {
				"rear": {eid: True for eid in self.edge_ids},
				"spawn": {"E1": True, "E2": False, "E3": True, "E4": True},
				"general": {eid: True for eid in self.edge_ids},
			},
		}
		self.barrier_here = {
			"es": {"natural": {eid: 0 for eid in self.edge_ids}, "anthro": {eid: 0 for eid in self.edge_ids}},
		}

	def test_upstream_accessible_length_sums_effective_length(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
		)
		self.assertEqual(result["es"]["upstream_accessible_length"]["E3"], 10 + 20 + 5)
		self.assertEqual(result["es"]["upstream_accessible_length"]["E4"], 10 + 20 + 5 + 3)

	def test_upstream_rear_length_all_habitat(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
		)
		self.assertEqual(result["es"]["rear_upstream_length"]["E4"], 10 + 20 + 5 + 3)

	def test_upstream_spawn_length_excludes_non_habitat_edge(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "spawn")],
		)
		# E2 is not spawn habitat, so its length is excluded from the sum
		self.assertEqual(result["es"]["spawn_upstream_length"]["E4"], 10 + 0 + 5 + 3)

	def test_weighted_length_uses_strahler_order_weight(self):
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, self.barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
		)
		# E1 (order 1, weight 0.5): 10*0.5=5; E2 (order 1): 20*0.5=10; E3 (order 2, weight 0.8): 5*0.8=4
		self.assertAlmostEqual(result["es"]["rear_weighted_upstream_length"]["E3"], 5 + 10 + 4)

	def test_functional_length_resets_at_barrier(self):
		barrier_here = {
			"es": {"natural": {**{eid: 0 for eid in self.edge_ids}, "E1": 1}, "anthro": {eid: 0 for eid in self.edge_ids}},
		}
		result = ls.compute_species_length_stats(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			self.accessibility, self.habitat, barrier_here, SPECIES_PARAMS,
			[("es", "rear")],
		)
		# E1 is a barrier at its own start (no predecessors so no difference for E1 itself),
		# but E1's own length should still count toward E3's functional total
		self.assertEqual(result["es"]["rear_functional_upstream_length"]["E3"], 5 + 10 + 20)


class ComputeLifecycleRollupsTests(unittest.TestCase):
	def setUp(self):
		self.edges = make_edges()
		self.edge_ids = [e["id"] for e in self.edges]
		self.successor, self.predecessors, self.roots = gs.build_graph(self.edges)
		self.order_up = gs.upstream_order(self.predecessors, self.roots)
		self.effective_length = {"E1": 10.0, "E2": 20.0, "E3": 5.0, "E4": 3.0}
		self.strahler_order = {"E1": 1, "E2": 1, "E3": 2, "E4": 2}

	def test_rollup_is_true_if_any_species_has_habitat(self):
		habitat = {
			"es": {"rear": {"E1": True, "E2": False, "E3": False, "E4": False}},
			"wl": {"rear": {"E1": False, "E2": True, "E3": False, "E4": False}},
		}
		barrier_here = {
			sp: {"natural": {eid: 0 for eid in self.edge_ids}, "anthro": {eid: 0 for eid in self.edge_ids}}
			for sp in ("es", "wl")
		}
		result = ls.compute_lifecycle_rollups(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			habitat, barrier_here, SPECIES_PARAMS, [("es", "rear"), ("wl", "rear")],
		)
		# E3's upstream rollup should include both E1 (es habitat) and E2 (wl habitat), but not
		# E3's own length (habitat for neither species there)
		self.assertEqual(result["rear"]["upstream_length"]["E3"], 10 + 20)

	def test_functional_reset_requires_all_species_blocked(self):
		habitat = {
			"es": {"rear": {eid: True for eid in self.edge_ids}},
			"wl": {"rear": {eid: True for eid in self.edge_ids}},
		}
		# E1 is a barrier for es but not wl -> should NOT reset the rollup (wl can still pass)
		barrier_here = {
			"es": {"natural": {**{eid: 0 for eid in self.edge_ids}, "E1": 1}, "anthro": {eid: 0 for eid in self.edge_ids}},
			"wl": {"natural": {eid: 0 for eid in self.edge_ids}, "anthro": {eid: 0 for eid in self.edge_ids}},
		}
		result = ls.compute_lifecycle_rollups(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			habitat, barrier_here, SPECIES_PARAMS, [("es", "rear"), ("wl", "rear")],
		)
		self.assertEqual(result["rear"]["functional_upstream_length"]["E3"], 5 + 10 + 20)

	def test_functional_reset_when_all_species_blocked(self):
		habitat = {
			"es": {"rear": {eid: True for eid in self.edge_ids}},
			"wl": {"rear": {eid: True for eid in self.edge_ids}},
		}
		barrier_here = {
			sp: {"natural": {**{eid: 0 for eid in self.edge_ids}, "E1": 1}, "anthro": {eid: 0 for eid in self.edge_ids}}
			for sp in ("es", "wl")
		}
		result = ls.compute_lifecycle_rollups(
			self.order_up, self.predecessors, self.edge_ids, self.effective_length, self.strahler_order,
			habitat, barrier_here, SPECIES_PARAMS, [("es", "rear"), ("wl", "rear")],
		)
		# both species blocked at E1 -> E1's own length still counts, but nothing beyond it would
		# (E1 has no predecessors here, so this just confirms E1's own value is unaffected)
		self.assertEqual(result["rear"]["functional_upstream_length"]["E1"], 10)


class ComputeBarrierUpstreamDownstreamStatsTests(unittest.TestCase):
	def test_excludes_barriers_own_position_from_upstream_count(self):
		barriers = [{"id": "b1", "edge_id": "E3", "structure_type": "anthropogenic"}]
		barrier_stats = {
			"es": {
				"upstream_natural_count": {"E3": 0, "E4": 0},
				"upstream_anthro_count": {"E3": 1, "E4": 1},
				"downstream_natural_count": {"E3": 0},
				"downstream_anthro_count": {"E3": 0},
				"downstream_natural_ids": {"E3": []},
				"downstream_anthro_ids": {"E3": []},
			},
		}
		barrier_here_by_species = {
			"es": {"natural": {"E3": 0}, "anthro": {"E3": 1}},
		}
		result = ls.compute_barrier_upstream_downstream_stats(barriers, barrier_stats, barrier_here_by_species)
		# upstream count at E3 was 1 (including this barrier's own self-flag) -> excluding self = 0
		self.assertEqual(result["b1"]["es"]["upstream_anthro_count"], 0)
		self.assertEqual(result["b1"]["es"]["downstream_anthro_count"], 0)


if __name__ == "__main__":
	unittest.main()
