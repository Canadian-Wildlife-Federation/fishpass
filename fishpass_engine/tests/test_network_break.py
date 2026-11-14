"""Tests for fishpass_engine/scripts/network_break.py's pure splitting logic (match_vertex_index,
break_edge) plus find_downstream_edge_ids/break_network's DB-facing orchestration against a fake
cursor. No real database needed.

Run with: python -m unittest fishpass_engine.tests.test_network_break
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

for _module_name in ("psycopg",):
	if _module_name not in sys.modules:
		try:
			__import__(_module_name)
		except ImportError:
			sys.modules[_module_name] = types.ModuleType(_module_name)

import graph_stats as gs  # noqa: E402
import network_break as nb  # noqa: E402
import network_snap as ns  # noqa: E402


def counting_id_factory():
	counter = [0]

	def factory():
		counter[0] += 1
		return f"new-nexus-{counter[0]}"

	return factory


def make_edge(vertices, from_nexus="N-from", to_nexus="N-to"):
	return {"vertices": vertices, "from_nexus_id": from_nexus, "to_nexus_id": to_nexus}


class MatchVertexIndexTests(unittest.TestCase):
	def test_finds_exact_match(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		self.assertEqual(nb.match_vertex_index(vertices, 1.0, 0.0), 1)

	def test_no_match_returns_none(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0]]
		self.assertIsNone(nb.match_vertex_index(vertices, 5.0, 5.0))

	def test_tolerates_tiny_float_noise(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0]]
		self.assertEqual(nb.match_vertex_index(vertices, 1.0 + 1e-12, 0.0), 1)


class BreakEdgeTests(unittest.TestCase):
	def test_no_markers_produces_one_unchanged_segment(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		edge = make_edge(vertices)
		segments, end_markers = nb.break_edge(edge, [], new_id_factory=counting_id_factory())
		self.assertEqual(len(segments), 1)
		self.assertEqual(segments[0]["vertices"], vertices)
		self.assertEqual(segments[0]["from_nexus_id"], "N-from")
		self.assertEqual(segments[0]["to_nexus_id"], "N-to")
		self.assertEqual(segments[0]["start_markers"], [])
		self.assertEqual(end_markers, [])

	def test_single_internal_marker_splits_into_two_segments(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		edge = make_edge(vertices)
		points = [(1.0, 0.0, "barrier", "b1")]
		segments, end_markers = nb.break_edge(edge, points, new_id_factory=counting_id_factory())

		self.assertEqual(len(segments), 2)
		seg1, seg2 = segments
		self.assertEqual(seg1["vertices"], vertices[0:2])
		self.assertEqual(seg1["from_nexus_id"], "N-from")
		self.assertEqual(seg1["to_nexus_id"], "new-nexus-1")
		self.assertEqual(seg1["start_markers"], [])

		self.assertEqual(seg2["vertices"], vertices[1:3])
		self.assertEqual(seg2["from_nexus_id"], "new-nexus-1")
		self.assertEqual(seg2["to_nexus_id"], "N-to")
		self.assertEqual(seg2["start_markers"], [("barrier", "b1")])
		self.assertEqual(end_markers, [])

	def test_marker_on_first_vertex_stays_in_single_segment(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0]]
		edge = make_edge(vertices)
		points = [(0.0, 0.0, "habitat_upstream", "h1")]
		segments, end_markers = nb.break_edge(edge, points, new_id_factory=counting_id_factory())

		self.assertEqual(len(segments), 1)
		self.assertEqual(segments[0]["start_markers"], [("habitat_upstream", "h1")])
		self.assertEqual(end_markers, [])

	def test_marker_on_last_vertex_returned_as_end_marker(self):
		# never attributed to a segment of this edge -- caller resolves it against whatever
		# edge starts at this edge's to_nexus_id instead
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0]]
		edge = make_edge(vertices)
		points = [(1.0, 0.0, "barrier", "b1")]
		segments, end_markers = nb.break_edge(edge, points, new_id_factory=counting_id_factory())

		self.assertEqual(len(segments), 1)
		self.assertEqual(segments[0]["start_markers"], [])
		self.assertEqual(end_markers, [("barrier", "b1")])

	def test_marker_on_last_vertex_returned_as_end_marker_even_when_edge_also_splits(self):
		# the earlier bug: an end-vertex marker must never fall back onto this edge's own last
		# segment just because this edge happens to be split elsewhere along its length
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		edge = make_edge(vertices)
		points = [(1.0, 0.0, "barrier", "b1"), (2.0, 0.0, "barrier", "b2")]
		segments, end_markers = nb.break_edge(edge, points, new_id_factory=counting_id_factory())

		self.assertEqual(len(segments), 2)
		self.assertEqual(segments[0]["start_markers"], [])
		self.assertEqual(segments[1]["start_markers"], [("barrier", "b1")])
		self.assertEqual(end_markers, [("barrier", "b2")])

	def test_multiple_markers_on_same_vertex_are_grouped(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		edge = make_edge(vertices)
		points = [(1.0, 0.0, "barrier", "b1"), (1.0, 0.0, "habitat_downstream", "h1")]
		segments, end_markers = nb.break_edge(edge, points, new_id_factory=counting_id_factory())

		self.assertEqual(len(segments), 2)
		self.assertEqual(
			sorted(segments[1]["start_markers"]),
			sorted([("barrier", "b1"), ("habitat_downstream", "h1")]),
		)
		self.assertEqual(end_markers, [])

	def test_multiple_split_points_produce_ordered_segments(self):
		vertices = [
			[0.0, 0.0, 0.0, 0.0],
			[1.0, 0.0, 0.0, 10.0],
			[2.0, 0.0, 0.0, 20.0],
			[3.0, 0.0, 0.0, 30.0],
		]
		edge = make_edge(vertices)
		points = [(1.0, 0.0, "barrier", "b1"), (2.0, 0.0, "barrier", "b2")]
		segments, end_markers = nb.break_edge(edge, points, new_id_factory=counting_id_factory())

		self.assertEqual(len(segments), 3)
		self.assertEqual([s["vertices"] for s in segments], [vertices[0:2], vertices[1:3], vertices[2:4]])
		self.assertEqual(segments[0]["to_nexus_id"], segments[1]["from_nexus_id"])
		self.assertEqual(segments[1]["to_nexus_id"], segments[2]["from_nexus_id"])
		self.assertEqual(segments[1]["start_markers"], [("barrier", "b1")])
		self.assertEqual(segments[2]["start_markers"], [("barrier", "b2")])
		self.assertEqual(end_markers, [])


class _FakeUUID:
	"""str() needs to hit the type's __str__, not an instance attribute -- mock.Mock() doesn't
	support that, so use a trivial real object instead."""

	def __init__(self, s):
		self._s = s

	def __str__(self):
		return self._s


class FakeCursor:
	def __init__(self, fetch_results=None):
		self.executed = []
		self.executemany_calls = []
		self._fetch_results = list(fetch_results or [])

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))

	def executemany(self, sql, params_seq):
		params_seq = list(params_seq)
		self.executemany_calls.append((" ".join(sql.split()), params_seq))
		for params in params_seq:
			self.executed.append((" ".join(sql.split()), params))

	def fetchall(self):
		return self._fetch_results.pop(0) if self._fetch_results else []


class FindDownstreamEdgeIdsTests(unittest.TestCase):
	def test_maps_to_nexus_to_edge_starting_there(self):
		cursor = FakeCursor(fetch_results=[[("N2", "F")]])
		result = nb.find_downstream_edge_ids(cursor, "out", ["N2"])
		self.assertEqual(result, {"N2": "F"})

	def test_braid_picks_the_edge_build_graph_treats_as_downstream(self):
		# Two edges start at N2. Whatever order the rows come back in, the lookup must agree with
		# build_graph's successor, or a barrier moved here would have no predecessors.
		rows = [("N2", "Fb"), ("N2", "Fa")]
		edges = [
			{"id": "E", "from_nexus_id": "N0", "to_nexus_id": "N2"},
			{"id": "Fb", "from_nexus_id": "N2", "to_nexus_id": "N3"},
			{"id": "Fa", "from_nexus_id": "N2", "to_nexus_id": "N4"},
		]
		graph_successor = gs.build_graph(edges)[0]["E"]
		for ordering in (rows, list(reversed(rows))):
			cursor = FakeCursor(fetch_results=[ordering])
			result = nb.find_downstream_edge_ids(cursor, "out", ["N2"])
			self.assertEqual(result, {"N2": graph_successor})

	def test_outlet_nexus_has_no_entry(self):
		cursor = FakeCursor(fetch_results=[[]])
		result = nb.find_downstream_edge_ids(cursor, "out", ["N-outlet"])
		self.assertEqual(result, {})


class BreakNetworkEndMarkerTests(unittest.TestCase):
	"""End-to-end (against a fake cursor) coverage of the bug this module used to have: a marker
	on an edge's own last vertex must be reassigned to the real downstream edge, not left
	pointing at whatever segment id its own (upstream) edge happened to keep after being split
	elsewhere -- and must be left untouched when there is no downstream edge at all."""

	DEFAULT_BARRIERS = [("E", 1.0, 0.0, "b1"), ("E", 2.0, 0.0, "b2")]  # b1 internal, b2 on E's last vertex

	def _run(self, streams_row, downstream_lookup_result, barriers=None, habitat_downstream=None):
		# get_break_points: barrier / habitat_upstream / habitat_downstream queries, in order.
		# fetch_edges_with_markers, then find_downstream_edge_ids.
		fetch_results = [
			self.DEFAULT_BARRIERS if barriers is None else barriers,
			[],  # habitat_upstream
			habitat_downstream or [],
			[streams_row],  # fetch_edges_with_markers
			downstream_lookup_result,  # find_downstream_edge_ids
		]
		cursor = FakeCursor(fetch_results=fetch_results)
		conn = mock.Mock()
		plan = {"output_schema": "out"}

		uuids = [
			_FakeUUID("new-nexus-1"),
			_FakeUUID("new-seg-1"),
			_FakeUUID("sink-edge-1"),
			_FakeUUID("sink-nexus-1"),
		]
		with mock.patch.object(nb.uuid, "uuid4", side_effect=uuids):
			self.new_rows = nb.break_network(conn, cursor, plan, srid=4326)

		return cursor

	@staticmethod
	def _edge_inserts(cursor):
		return [params for sql, params in cursor.executed if sql.startswith("INSERT INTO") and ".streams" in sql]

	@classmethod
	def _sink_edge_inserts(cls, cursor):
		# Every new edge goes through the same INSERT; the last param is is_sink_barrier_edge.
		return [params for params in cls._edge_inserts(cursor) if params[-1] is True]

	def _streams_row(self):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		wkb = ns.linestring_zm_wkb(vertices)
		# STREAM_FIELDS order: id, aoi_id, ef_type, ef_subtype, rank, from_nexus_id, to_nexus_id,
		# ecatchment_id, mainstem_id, graph_id, is_isolated, strahler_order, rivernameid1,
		# stream_name_en, then geometry wkb.
		return ("E", "aoi", "single", None, 1, "N0", "N2", "ec", "ms", "g1", False, 1, "rn1", "Test Brook", bytes(wkb))

	def _downstream_edge_by_ref_id(self, cursor):
		by_ref_id = {}
		for sql, params in cursor.executed:
			if sql.startswith("UPDATE") and "all_barriers" in sql and "downstream_edge_id" in sql:
				by_ref_id[params[1]] = params[0]
		return by_ref_id

	def test_end_marker_reassigned_to_downstream_edge_not_own_split_segment(self):
		cursor = self._run(self._streams_row(), downstream_lookup_result=[("N2", "F")])
		downstream = self._downstream_edge_by_ref_id(cursor)

		# b1 sits on the internal split vertex -> downstream is E's own new second segment.
		self.assertEqual(downstream["b1"], "new-seg-1")
		# b2 sits on E's last vertex -> downstream must be reassigned to the real downstream
		# edge F, not left on "E" (which after the split only names the *first* segment) and
		# not on "new-seg-1" (E's own last segment) either.
		self.assertEqual(downstream["b2"], "F")

	def test_end_marker_on_outlet_edge_goes_on_new_sink_edge(self):
		cursor = self._run(self._streams_row(), downstream_lookup_result=[])
		downstream = self._downstream_edge_by_ref_id(cursor)

		self.assertEqual(downstream["b1"], "new-seg-1")
		# b2 is on the last vertex of an outlet edge that was also split: it goes on the new sink
		# edge, not on "E" (which after the split only names E's *first* segment).
		self.assertEqual(downstream["b2"], "sink-edge-1")

		sink_rows = self._sink_edge_inserts(cursor)
		self.assertEqual(len(sink_rows), 1)
		row = dict(zip(nb.STREAM_FIELDS + ("wkb", "srid"), sink_rows[0]))
		self.assertEqual(row["id"], "sink-edge-1")
		self.assertEqual(row["from_nexus_id"], "N2")  # E's to_nexus_id
		self.assertEqual(row["to_nexus_id"], "sink-nexus-1")
		for field in ("aoi_id", "ecatchment_id", "mainstem_id", "graph_id", "strahler_order", "stream_name_en"):
			self.assertEqual(row[field], dict(zip(nb.STREAM_FIELDS, self._streams_row()))[field], field)
		# Zero-length: E's last vertex twice, Z and M kept.
		self.assertEqual(ns.edge_vertices(row["wkb"]), [[2.0, 0.0, 0.0, 20.0], [2.0, 0.0, 0.0, 20.0]])
		self.assertEqual(row["srid"], 4326)
		# One split segment plus one sink edge.
		self.assertEqual(self.new_rows, 2)

	def test_sink_edge_inserted_before_barriers_reassigned(self):
		cursor = self._run(self._streams_row(), downstream_lookup_result=[])
		sink_insert = next(
			i
			for i, (sql, params) in enumerate(cursor.executed)
			if sql.startswith("INSERT INTO") and ".streams" in sql and params[-1] is True
		)
		reassign = next(
			i for i, (sql, _) in enumerate(cursor.executed) if sql.startswith("UPDATE") and "all_barriers" in sql
		)
		self.assertLess(sink_insert, reassign)

	def test_barriers_on_same_outlet_end_share_one_sink_edge(self):
		barriers = [("E", 2.0, 0.0, "b2"), ("E", 2.0, 0.0, "b3")]
		cursor = self._run(self._streams_row(), downstream_lookup_result=[], barriers=barriers)
		downstream = self._downstream_edge_by_ref_id(cursor)
		# No internal split here, so the first new uuid is the sink edge.
		self.assertEqual(downstream["b2"], downstream["b3"])
		self.assertEqual(len(self._sink_edge_inserts(cursor)), 1)

	def test_outlet_edges_ending_at_same_nexus_share_one_sink_edge(self):
		# E and G both end at outlet nexus N2, each with a barrier on its last vertex. One sink edge
		# starting at N2 is created, so both are its predecessors (two would look like a braid).
		vertices_g = [[5.0, 1.0, 0.0, 0.0], [2.0, 0.0, 0.0, 20.0]]
		row_g = ("G", "aoi", "single", None, 1, "N9", "N2", "ec", "ms", "g1", False, 1, "rn2", "Other Brook")
		fetch_results = [
			[("E", 2.0, 0.0, "b2"), ("G", 2.0, 0.0, "b4")],  # barriers on each edge's last vertex
			[],  # habitat_upstream
			[],  # habitat_downstream
			[self._streams_row(), row_g + (bytes(ns.linestring_zm_wkb(vertices_g)),)],
			[],  # find_downstream_edge_ids: N2 is an outlet
		]
		cursor = FakeCursor(fetch_results=fetch_results)
		uuids = [_FakeUUID("sink-edge-1"), _FakeUUID("sink-nexus-1")]
		with mock.patch.object(nb.uuid, "uuid4", side_effect=uuids):
			new_rows = nb.break_network(mock.Mock(), cursor, {"output_schema": "out"}, srid=4326)

		downstream = self._downstream_edge_by_ref_id(cursor)
		self.assertEqual(downstream["b2"], "sink-edge-1")
		self.assertEqual(downstream["b4"], "sink-edge-1")
		sink_rows = self._sink_edge_inserts(cursor)
		self.assertEqual(len(sink_rows), 1)
		self.assertEqual(dict(zip(nb.STREAM_FIELDS, sink_rows[0]))["from_nexus_id"], "N2")
		self.assertEqual(new_rows, 1)

	def test_split_segment_and_sink_edge_rows_share_one_layout(self):
		cursor = self._run(self._streams_row(), downstream_lookup_result=[])
		inserts = self._edge_inserts(cursor)
		# One split segment (not a sink edge) and one sink edge, same column layout.
		self.assertEqual([params[-1] for params in inserts], [False, True])
		self.assertEqual({len(params) for params in inserts}, {len(nb.STREAM_FIELDS) + 3})
		segment = dict(zip(nb.STREAM_FIELDS, inserts[0]))
		self.assertEqual(segment["id"], "new-seg-1")
		self.assertEqual(segment["from_nexus_id"], "new-nexus-1")
		self.assertEqual(segment["to_nexus_id"], "N2")
		self.assertEqual(segment["stream_name_en"], "Test Brook")

	def test_no_sink_edge_when_edge_has_downstream_edge(self):
		cursor = self._run(self._streams_row(), downstream_lookup_result=[("N2", "F")])
		self.assertEqual(self._sink_edge_inserts(cursor), [])

	def test_habitat_marker_on_outlet_end_gets_no_sink_edge(self):
		cursor = self._run(
			self._streams_row(),
			downstream_lookup_result=[],
			barriers=[],
			habitat_downstream=[("E", 2.0, 0.0, "h1")],
		)
		self.assertEqual(self._sink_edge_inserts(cursor), [])
		self.assertFalse(any("habitat_updates" in sql for sql, _ in cursor.executed if sql.startswith("UPDATE")))

	def test_no_upstream_edge_id_written(self):
		cursor = self._run(self._streams_row(), downstream_lookup_result=[("N2", "F")])
		for sql, _ in cursor.executed:
			self.assertNotIn("upstream_edge_id", sql)

	def test_segment_writes_do_not_set_length(self):
		# Lengths are recomputed for every edge in compute_effective_length_and_gradient.
		cursor = self._run(self._streams_row(), downstream_lookup_result=[("N2", "F")])
		stream_writes = [sql for sql, _ in cursor.executed if ".streams" in sql and ("UPDATE" in sql or "INSERT" in sql)]
		self.assertTrue(stream_writes)
		for sql in stream_writes:
			self.assertNotIn("length", sql)
			self.assertNotIn("ST_Length", sql)


class BreakNetworkBatchingTests(unittest.TestCase):
	"""With BATCH_SIZE forced small, the walk-edges loop must flush in multiple
	executemany() calls rather than accumulating every row for one flush at the end."""

	def _streams_row(self, edge_id, from_nexus, to_nexus):
		vertices = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 10.0], [2.0, 0.0, 0.0, 20.0]]
		wkb = ns.linestring_zm_wkb(vertices)
		return (
			edge_id,
			"aoi",
			"single",
			None,
			1,
			from_nexus,
			to_nexus,
			"ec",
			"ms",
			"g1",
			False,
			1,
			"rn1",
			"Test Brook",
			bytes(wkb),
		)

	def test_multiple_flushes_when_batch_size_is_small(self):
		edge_ids = ["E1", "E2", "E3"]
		fetch_results = [
			[(eid, 1.0, 0.0, f"b-{eid}") for eid in edge_ids],  # barriers, one internal marker each
			[],  # habitat_upstream
			[],  # habitat_downstream
			[self._streams_row(eid, f"N0-{eid}", f"N2-{eid}") for eid in edge_ids],  # fetch_edges_with_markers
			[],  # find_downstream_edge_ids
		]
		cursor = FakeCursor(fetch_results=fetch_results)
		conn = mock.Mock()
		plan = {"output_schema": "out"}

		new_ids = [_FakeUUID(f"new-{i}") for i in range(len(edge_ids) * 2)]
		with mock.patch.object(nb, "BATCH_SIZE", 1), mock.patch.object(nb.uuid, "uuid4", side_effect=new_ids):
			nb.break_network(conn, cursor, plan, srid=4326)

		update_flushes = [call for call in cursor.executemany_calls if "UPDATE" in call[0] and "streams" in call[0]]
		insert_flushes = [
			call for call in cursor.executemany_calls if call[0].startswith("INSERT INTO") and "streams" in call[0]
		]
		self.assertEqual(len(update_flushes), len(edge_ids))
		self.assertEqual(len(insert_flushes), len(edge_ids))
		for _sql, rows in update_flushes + insert_flushes:
			self.assertEqual(len(rows), 1)


if __name__ == "__main__":
	unittest.main()
