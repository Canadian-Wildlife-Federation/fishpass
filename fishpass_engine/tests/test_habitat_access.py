"""Tests for fishpass_engine/scripts/habitat_access.py -- pure mainstem-chain walking and
habitat override logic. No database.

Test network (same shape as test_graph_stats.py's confluence, but with mainstem_id assigned:
E1/E3/E4 continue the same mainstem M1 through the confluence; E2 is a tributary on mainstem M2):

    E1 (M1, N1->N3) --\\
                        E3 (M1, N3->N4) -- E4 (M1, N4->N5, outlet)
    E2 (M2, N2->N3) --/

Run with: python -m unittest fishpass_engine.tests.test_habitat_access
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import graph_stats as gs  # noqa: E402
import habitat_access as ha  # noqa: E402


def make_edges():
	return [
		{"id": "E1", "from_nexus_id": "N1", "to_nexus_id": "N3", "mainstem_id": "M1"},
		{"id": "E2", "from_nexus_id": "N2", "to_nexus_id": "N3", "mainstem_id": "M2"},
		{"id": "E3", "from_nexus_id": "N3", "to_nexus_id": "N4", "mainstem_id": "M1"},
		{"id": "E4", "from_nexus_id": "N4", "to_nexus_id": "N5", "mainstem_id": "M1"},
	]


class ParseSpeciesLifestageTests(unittest.TestCase):
	def test_bare_species_means_both_lifecycles(self):
		self.assertEqual(ha.parse_species_lifestage("as"), ("as", ["rear", "spawn"], True))

	def test_species_with_spawn_suffix(self):
		self.assertEqual(ha.parse_species_lifestage("as_spawn"), ("as", ["spawn"], True))

	def test_species_with_rear_suffix(self):
		self.assertEqual(ha.parse_species_lifestage("as_rear"), ("as", ["rear"], True))

	def test_not_prefix_alone_means_both_lifecycles_excluded(self):
		self.assertEqual(ha.parse_species_lifestage("not_as"), ("as", ["rear", "spawn"], False))

	def test_not_prefix_with_suffix(self):
		self.assertEqual(ha.parse_species_lifestage("not_as_rear"), ("as", ["rear"], False))


class MainstemWalkTests(unittest.TestCase):
	def setUp(self):
		self.edges = make_edges()
		self.edges_by_id = {e["id"]: e for e in self.edges}
		self.successor, self.predecessors, self.roots = gs.build_graph(self.edges)

	def test_upstream_walk_excludes_other_mainstem(self):
		result = ha.mainstem_segments_upstream(self.edges_by_id, self.predecessors, "E4")
		self.assertEqual(set(result), {"E4", "E3", "E1"})
		self.assertNotIn("E2", result)

	def test_downstream_walk_stops_at_outlet(self):
		result = ha.mainstem_segments_downstream(self.edges_by_id, self.successor, "E1")
		self.assertEqual(result, ["E1", "E3", "E4"])

	def test_downstream_walk_from_tributary_stops_immediately(self):
		# E2's successor E3 is on a different mainstem, so the walk from E2 stops at E2 itself
		result = ha.mainstem_segments_downstream(self.edges_by_id, self.successor, "E2")
		self.assertEqual(result, ["E2"])

	def test_between_walks_upstream_from_downstream_point(self):
		result = ha.mainstem_segments_between(self.edges_by_id, self.predecessors, "E4", "E1")
		self.assertEqual(result, ["E4", "E3", "E1"])

	def test_between_raises_on_mainstem_mismatch(self):
		with self.assertRaises(ValueError):
			ha.mainstem_segments_between(self.edges_by_id, self.predecessors, "E2", "E1")


class ResolveSegmentsTests(unittest.TestCase):
	def setUp(self):
		self.edges = make_edges()
		self.edges_by_id = {e["id"]: e for e in self.edges}
		self.successor, self.predecessors, self.roots = gs.build_graph(self.edges)

	def test_upstream_location_type(self):
		row = {"location_type": "upstream", "upstream_snapped_edge_id": "E3", "downstream_snapped_edge_id": None}
		result = ha.resolve_segments(row, self.edges_by_id, self.predecessors, self.successor)
		self.assertEqual(set(result), {"E3", "E1"})

	def test_unresolved_point_returns_none(self):
		row = {"location_type": "upstream", "upstream_snapped_edge_id": None, "downstream_snapped_edge_id": None}
		result = ha.resolve_segments(row, self.edges_by_id, self.predecessors, self.successor)
		self.assertIsNone(result)

	def test_between_location_type(self):
		row = {"location_type": "between", "upstream_snapped_edge_id": "E1", "downstream_snapped_edge_id": "E4"}
		result = ha.resolve_segments(row, self.edges_by_id, self.predecessors, self.successor)
		self.assertEqual(result, ["E4", "E3", "E1"])

	def test_between_on_different_mainstems_exits_gracefully(self):
		row = {"id": "H1", "location_type": "between", "upstream_snapped_edge_id": "E1", "downstream_snapped_edge_id": "E2"}
		with self.assertRaises(SystemExit) as ctx:
			ha.resolve_segments(row, self.edges_by_id, self.predecessors, self.successor)
		self.assertIn("H1", str(ctx.exception))
		self.assertIn("different mainstems", str(ctx.exception))


class ApplyHabitatAccessOverridesTests(unittest.TestCase):
	def setUp(self):
		self.edges = make_edges()
		self.edges_by_id = {e["id"]: e for e in self.edges}
		self.successor, self.predecessors, self.roots = gs.build_graph(self.edges)
		self.edge_ids = [e["id"] for e in self.edges]
		# start with everything False for a single species "es"
		self.habitat = {"es": {"rear": {eid: False for eid in self.edge_ids}, "spawn": {eid: False for eid in self.edge_ids}}}

	def test_upstream_row_sets_rearing_true(self):
		rows = [{
			"location_type": "upstream", "upstream_snapped_edge_id": "E3", "downstream_snapped_edge_id": None,
			"species_lifestage": ["es_rear"],
		}]
		ha.apply_habitat_access_overrides(self.habitat, self.edges_by_id, self.predecessors, self.successor, rows)
		self.assertTrue(self.habitat["es"]["rear"]["E3"])
		self.assertTrue(self.habitat["es"]["rear"]["E1"])
		self.assertFalse(self.habitat["es"]["rear"]["E2"])  # different mainstem, not touched
		self.assertFalse(self.habitat["es"]["spawn"]["E3"])  # lifestage not touched

	def test_not_rearing_sets_false(self):
		self.habitat["es"]["rear"] = {eid: True for eid in self.edge_ids}
		rows = [{
			"location_type": "downstream", "upstream_snapped_edge_id": None, "downstream_snapped_edge_id": "E1",
			"species_lifestage": ["not_es_rear"],
		}]
		ha.apply_habitat_access_overrides(self.habitat, self.edges_by_id, self.predecessors, self.successor, rows)
		self.assertFalse(self.habitat["es"]["rear"]["E1"])
		self.assertFalse(self.habitat["es"]["rear"]["E3"])
		self.assertFalse(self.habitat["es"]["rear"]["E4"])

	def test_bare_species_sets_both_lifecycles_true(self):
		rows = [{
			"location_type": "upstream", "upstream_snapped_edge_id": "E3", "downstream_snapped_edge_id": None,
			"species_lifestage": ["es"],
		}]
		ha.apply_habitat_access_overrides(self.habitat, self.edges_by_id, self.predecessors, self.successor, rows)
		self.assertTrue(self.habitat["es"]["rear"]["E3"])
		self.assertTrue(self.habitat["es"]["spawn"]["E3"])

	def test_not_bare_species_clears_both_lifecycles(self):
		self.habitat["es"]["rear"] = {eid: True for eid in self.edge_ids}
		self.habitat["es"]["spawn"] = {eid: True for eid in self.edge_ids}
		rows = [{
			"location_type": "upstream", "upstream_snapped_edge_id": "E3", "downstream_snapped_edge_id": None,
			"species_lifestage": ["not_es"],
		}]
		ha.apply_habitat_access_overrides(self.habitat, self.edges_by_id, self.predecessors, self.successor, rows)
		self.assertFalse(self.habitat["es"]["rear"]["E3"])
		self.assertFalse(self.habitat["es"]["spawn"]["E3"])

	def test_later_row_wins_on_overlap(self):
		rows = [
			{"location_type": "upstream", "upstream_snapped_edge_id": "E4", "downstream_snapped_edge_id": None,
			 "species_lifestage": ["es_rear"]},
			{"location_type": "upstream", "upstream_snapped_edge_id": "E4", "downstream_snapped_edge_id": None,
			 "species_lifestage": ["not_es_rear"]},
		]
		ha.apply_habitat_access_overrides(self.habitat, self.edges_by_id, self.predecessors, self.successor, rows)
		self.assertFalse(self.habitat["es"]["rear"]["E4"])  # second row overwrote the first

	def test_species_not_in_habitat_is_skipped(self):
		rows = [{
			"location_type": "upstream", "upstream_snapped_edge_id": "E3", "downstream_snapped_edge_id": None,
			"species_lifestage": ["zz_rear"],
		}]
		# should not raise
		ha.apply_habitat_access_overrides(self.habitat, self.edges_by_id, self.predecessors, self.successor, rows)
		self.assertFalse(self.habitat["es"]["rear"]["E3"])


class DeriveSpawnrearHabitatTests(unittest.TestCase):
	def test_spawnrear_is_union_of_rear_and_spawn(self):
		habitat = {
			"es": {
				"rear": {"E1": True, "E2": False},
				"spawn": {"E1": False, "E2": False},
			},
		}
		ha.derive_spawnrear_habitat(habitat)
		self.assertTrue(habitat["es"]["spawnrear"]["E1"])
		self.assertFalse(habitat["es"]["spawnrear"]["E2"])


if __name__ == "__main__":
	unittest.main()
