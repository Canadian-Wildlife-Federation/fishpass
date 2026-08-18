"""Tests for gradient_barriers/scripts/compute_barriers.py.

These drive the whole compute_barriers() function -- including fetch_edges and the shapely
WKB/M-ordinate parsing in edge_vertices() -- against a stubbed psycopg connection/cursor, so no
real database is needed. shapely must be genuinely installed (not stubbed) since these tests build
and parse real WKB fixtures. compute_barriers() writes directly via insert_barriers as its
internal barrier cache fills, so tests patch that module-level function to capture the batches
instead of hitting a database.

Run with: python -m unittest gradient_barriers.tests.test_compute_barriers
"""

import math
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# compute_barriers.py imports psycopg/yaml at module level for its DB-connection
# and species-file-loading code paths, neither of which compute_barriers() itself touches (it
# only calls conn.cursor(...), which FakeConnection below provides, and insert_barriers, which
# tests patch out). Stub them out when unavailable so these tests don't require the full
# production dependency set to be installed.
for _module_name in ("psycopg", "yaml"):
	try:
		__import__(_module_name)
	except ImportError:
		sys.modules[_module_name] = types.ModuleType(_module_name)

import shapely  # noqa: E402 -- must be real; these tests build/parse actual WKB fixtures

import compute_barriers as cb  # noqa: E402


# ---------------------------------------------------------------------------
# Stubbed DB layer -- compute_barriers(conn, cursor, srid, ...) calls fetch_edges(conn), which
# calls conn.cursor(name=...), sets .itersize, .execute(...), iterates the cursor, then closes
# it; compute_barriers also calls insert_barriers(cursor, srid, batch) directly whenever its
# internal cache fills, which tests patch via run_compute_barriers below.
# ---------------------------------------------------------------------------


class FakeNamedCursor:
	def __init__(self, rows):
		self._rows = rows
		self.itersize = None
		self.last_sql = None
		self.last_params = None

	def execute(self, sql, params=None):
		# fixture rows are already prepared -- just record what was asked for, so tests can
		# assert on the query shape without a real database
		self.last_sql = sql
		self.last_params = params

	def __iter__(self):
		return iter(self._rows)

	def close(self):
		pass


class FakeConnection:
	def __init__(self, rows):
		self._rows = rows
		self.commit_count = 0

	def cursor(self, name=None, withhold=False):
		return FakeNamedCursor(self._rows)

	def commit(self):
		self.commit_count += 1


def run_compute_barriers(rows, species_params, aoi_ids=None):
	"""Run compute_barriers against a FakeConnection(rows), with insert_barriers patched to
	record each batch it's called with instead of touching a database.

	Returns (total, batches) -- total is compute_barriers' own returned count, batches is the
	list of barrier-lists passed to each insert_barriers call, in call order. cursor/srid are
	arbitrary placeholders since the real insert_barriers is patched out."""
	batches = []
	with mock.patch.object(cb, "insert_barriers", side_effect=lambda cursor, srid, batch: batches.append(list(batch))):
		total = cb.compute_barriers(FakeConnection(rows), "cursor", "srid", species_params, aoi_ids=aoi_ids)
	return total, batches


def flat_barriers(rows, species_params, aoi_ids=None):
	"""Convenience for tests that just want the full ordered barrier list, not per-batch detail."""
	total, batches = run_compute_barriers(rows, species_params, aoi_ids=aoi_ids)
	barriers = [b for batch in batches for b in batch]
	assert len(barriers) == total
	return barriers


# ---------------------------------------------------------------------------
# WKB fixture helpers
# ---------------------------------------------------------------------------

BASE_LON = -64.0
BASE_LAT = 45.0
DEFAULT_AOI_ID = "aoi-default"


def lat_offset_deg(distance_m):
	"""Degrees of latitude corresponding to distance_m north, at BASE_LAT.

	Holding longitude constant makes haversine_m (which the production code uses) reduce to
	distance ~= EARTH_RADIUS_M * delta_lat_radians for these small distances -- essentially
	exact at this scale, so fixtures built this way have real haversine distances matching
	their intended cumulative distances.
	"""
	return distance_m / cb.EARTH_RADIUS_M * 180.0 / math.pi


def point_at(distance_m, elevation, lon=BASE_LON, lat=BASE_LAT):
	"""A point distance_m upstream (north) of (lon, lat), at the given elevation."""
	return (lon, lat + lat_offset_deg(distance_m), elevation)


def make_wkb(vertices):
	"""vertices: [(lon, lat, elevation), ...] in storage (upstream -> downstream) order.

	Built via WKT ("LINESTRING M (...)") since that's the most portable way to express a
	measured (M-only, no Z) LineString, and mirrors what ST_AsBinary on a real LINESTRING M
	flowpath geometry would produce.
	"""
	points = ", ".join(f"{lon} {lat} {m}" for lon, lat, m in vertices)
	return shapely.to_wkb(shapely.from_wkt(f"LINESTRING M ({points})"))


def edge_row(edge_id, mainstem_id, mainstem_seq, vertices, aoi_id=DEFAULT_AOI_ID):
	return (edge_id, mainstem_id, mainstem_seq, aoi_id, make_wkb(vertices))


# Thresholds picked low enough that both worked-example gradients (5.25% and 0.5%) clear
# rearing_max and register as barriers -- not meant to reflect any real species' parameters.
LOW_THRESHOLDS = [{"code": "chn", "spawning_max": 1.0, "rearing_max": 0.003}]

# Thresholds set above both worked-example gradients, so neither should register as a barrier.
HIGH_THRESHOLDS = [{"code": "chn", "spawning_max": 1.0, "rearing_max": 1.0}]

class FlagSpeciesTests(unittest.TestCase):

	def test_blank_threshold_is_never_a_barrier(self):
		species_params = [{"code": "chn", "spawning_max": None, "rearing_max": None}]
		self.assertEqual(cb.flag_species(1000.0, species_params), [])

	def test_blank_threshold_does_not_suppress_other_lifestage(self):
		species_params = [{"code": "chn", "spawning_max": None, "rearing_max": 0.01}]
		self.assertEqual(cb.flag_species(0.5, species_params), ["chn_rear"])


class ComputeBarriersTests(unittest.TestCase):

	"""The i / A / B worked example from requirements.md's "Design Decisions" section:
	i: 0m, elevation 0; A: 50m, elevation 5 (10% grade); B: 150m, elevation 5.5 (0.5% grade for
	the 100m beyond A). Interpolating between A and B for i's 100m mark gives 5.25%; A's own
	100m mark lands exactly on B, giving 0.5%.
	"""
	def test_worked_example_single_edge(self):
		i = point_at(0.0, 0.0)
		a = point_at(50.0, 5.0)
		b = point_at(200.0, 5.75)
		rows =  [edge_row("worked-e1", "1", 1, [b, a, i])]

		barriers = flat_barriers(rows, LOW_THRESHOLDS)

		self.assertEqual(len(barriers), 2)

		i_lon, i_lat, i_gradient, i_species = barriers[0]
		self.assertAlmostEqual(i_lon, i[0], places=9)
		self.assertAlmostEqual(i_lat, i[1], places=9)
		self.assertAlmostEqual(i_gradient, 0.0525)
		self.assertEqual(i_species, ["chn_rear"])

		a_lon, a_lat, a_gradient, a_species = barriers[1]
		self.assertAlmostEqual(a_lon, a[0], places=9)
		self.assertAlmostEqual(a_lat, a[1], places=9)
		self.assertAlmostEqual(a_gradient, 0.005)
		self.assertEqual(a_species, ["chn_rear"])

		barriers = flat_barriers(rows, HIGH_THRESHOLDS)
		self.assertEqual(barriers, [])

	def test_nearest_vertex_would_have_given_a_different_answer(self):
		"""Documents *why* interpolation matters: using vertex B's elevation directly (the old
		nearest-vertex-past-100m behavior) instead of interpolating would average the gradient
		over the full 150m from i to B, not the 100m the spec asks about. Pure arithmetic --
		no fixture needed."""
		nearest_vertex_gradient = (5.5 - 0.0) / 150.0
		interpolated_elevation = 5.0 + (100.0 - 50.0) / (150.0 - 50.0) * (5.5 - 5.0)
		interpolated_gradient = (interpolated_elevation - 0.0) / 100.0

		self.assertAlmostEqual(nearest_vertex_gradient, 0.036666, places=5)
		self.assertAlmostEqual(interpolated_gradient, 0.0525)
		self.assertNotAlmostEqual(nearest_vertex_gradient, interpolated_gradient, places=3)

	def test_no_gradient_when_upstream_mainstem_too_short(self):
		# A single 60m edge -- well under UPSTREAM_DISTANCE_M, so the downstream vertex never
		# reaches a point 100m upstream and no barrier can be produced.
		start = point_at(0.0, 0.0)
		end = point_at(60.0, 3.0)
		rows = [edge_row("short-e1", "short", 1, [end, start])]

		barriers = flat_barriers(rows, LOW_THRESHOLDS)
		self.assertEqual(barriers, [])

	def test_multi_edge_mainstem_crosses_edge_boundary(self):
		# Two edges on the same mainstem: edge1 (mainstem_seq=1, most downstream) covers 0-60m,
		# edge2 (mainstem_seq=2, upstream of edge1) continues 60-105m. The shared boundary
		# vertex is the same point object in both edges' vertex lists, as real topologically
		# connected edges would share exact coordinates.
		i2 = point_at(0.0, 0.0)
		boundary = point_at(60.0, 6.0)
		up = point_at(105.0, 7.0)
		rows = [
			edge_row("multi-e1", "multi", 1, [boundary, i2]),
			edge_row("multi-e2", "multi", 2, [up, boundary]),
		]

		barriers = flat_barriers(rows, LOW_THRESHOLDS)

		# Only i2 resolves: its 100m mark (falls between boundary@60m and up@105m) is reached
		# once `up` is walked. boundary's own 100m mark (target 160m) is never reached -- the
		# mainstem only reaches 105m -- so it's left in the window and discarded.
		self.assertEqual(len(barriers), 1)
		lon, lat, gradient, species = barriers[0]
		self.assertAlmostEqual(lon, i2[0], places=9)
		self.assertAlmostEqual(lat, i2[1], places=9)
		expected_interp_elev = 6.0 + (100.0 - 60.0) / (105.0 - 60.0) * (7.0 - 6.0)
		expected_gradient = (expected_interp_elev - 0.0) / 100.0
		self.assertAlmostEqual(gradient, expected_gradient)
		self.assertEqual(species, ["chn_rear"])

	def test_invalid_elevation_vertex_is_skipped_but_distance_carries_through(self):
		# A (0m, elev 0) -- invalid (50m, NO_DATA) -- B (150m, elev 15). The invalid vertex must
		# never appear as a barrier, must not become the interpolation reference, but distance
		# must still accumulate through it so A's 100m mark (reached once B arrives) interpolates
		# directly from A to B: interp_elev = 0 + (100/150)*15 = 10, gradient = 10/100 = 0.10.
		a = point_at(0.0, 0.0)
		invalid = point_at(50.0, cb.NO_DATA)
		b = point_at(150.0, 15.0)
		rows = [edge_row("nodata-e1", "nodata", 1, [b, invalid, a])]

		barriers = flat_barriers(rows, LOW_THRESHOLDS)

		self.assertEqual(len(barriers), 1)
		lon, lat, gradient, species = barriers[0]
		self.assertAlmostEqual(lon, a[0], places=9)
		self.assertAlmostEqual(lat, a[1], places=9)
		self.assertAlmostEqual(gradient, 0.10)
		self.assertEqual(species, ["chn_rear"])

	def test_multiple_mainstems_do_not_leak_into_each_other(self):
		# A short (no-barrier) mainstem followed by the worked-example mainstem in the same
		# fetch. Confirms running_total/window/prev reset cleanly on the mainstem_id boundary --
		# the short mainstem's leftover unresolved vertex must not carry into the next one.
		short_start = point_at(0.0, 0.0)
		short_end = point_at(60.0, 3.0)
		short_rows = [edge_row("short-e1", "short", 1, [short_end, short_start])]

		i = point_at(0.0, 0.0)
		a = point_at(50.0, 5.0)
		b = point_at(200.0, 5.75)
		worked_rows =  [edge_row("worked-e1", "2", 1, [b, a, i])]

		barriers = flat_barriers(short_rows + worked_rows, LOW_THRESHOLDS)

		self.assertEqual(len(barriers), 2)
		self.assertAlmostEqual(barriers[0][0], i[0], places=9)
		self.assertAlmostEqual(barriers[0][2], 0.0525)
		self.assertAlmostEqual(barriers[1][0], a[0], places=9)
		self.assertAlmostEqual(barriers[1][2], 0.005)


class AoiScopedComputeBarriersTests(unittest.TestCase):

	"""A single mainstem of 4 contiguous edges -- e1 (in-scope), e2 (OUT of scope), e3
	(in-scope), e4 (in-scope) -- covering vertices p0@0m, p1@40m, p2@90m, p3@150m, p4@260m at
	elevations 0/4/9/15/26. p2 is the only out-of-scope vertex.

	Walking this mainstem resolves p0, p1, p3 (all in-scope, all gradient exactly 0.10 -- worked
	by hand below) and p2 (out-of-scope, also gradient 0.10, but must NOT appear in the output).
	This proves the walk carries distance/elevation state correctly *through* an out-of-scope
	edge -- p0 and p1's gradients depend on p2's elevation via interpolation even though p2
	itself never becomes a barrier -- rather than resetting or corrupting at the AOI boundary.
	"""

	def test_out_of_scope_edge_is_walked_through_but_not_emitted(self):
		p0 = point_at(0.0, 0.0)
		p1 = point_at(40.0, 4.0)
		p2 = point_at(90.0, 9.0)
		p3 = point_at(150.0, 15.0)
		p4 = point_at(260.0, 26.0)  # margin past p3's exact 100m target (250m) to avoid a float boundary tie

		rows = [
			edge_row("e1", "ms", 1, [p1, p0], aoi_id="aoi-target"),
			edge_row("e2", "ms", 2, [p2, p1], aoi_id="aoi-other"),
			edge_row("e3", "ms", 3, [p3, p2], aoi_id="aoi-target"),
			edge_row("e4", "ms", 4, [p4, p3], aoi_id="aoi-target"),
		]

		barriers = flat_barriers(rows, LOW_THRESHOLDS, aoi_ids=["aoi-target"])

		self.assertEqual(len(barriers), 3)
		for (lon, lat, gradient, species), expected_point in zip(barriers, [p0, p1, p3]):
			self.assertAlmostEqual(lon, expected_point[0], places=9)
			self.assertAlmostEqual(lat, expected_point[1], places=9)
			self.assertAlmostEqual(gradient, 0.10)
			self.assertEqual(species, ["chn_rear"])

	def test_aoi_ids_none_treats_every_edge_as_in_scope(self):
		# Same fixture as above, but with no aoi_ids -- p2 (edge2's vertex) should now also
		# resolve and appear, since an unscoped run treats every edge as in scope.
		p0 = point_at(0.0, 0.0)
		p1 = point_at(40.0, 4.0)
		p2 = point_at(90.0, 9.0)
		p3 = point_at(150.0, 15.0)
		p4 = point_at(260.0, 26.0)  # margin past p3's exact 100m target (250m) to avoid a float boundary tie

		rows = [
			edge_row("e1", "ms", 1, [p1, p0], aoi_id="aoi-target"),
			edge_row("e2", "ms", 2, [p2, p1], aoi_id="aoi-other"),
			edge_row("e3", "ms", 3, [p3, p2], aoi_id="aoi-target"),
			edge_row("e4", "ms", 4, [p4, p3], aoi_id="aoi-target"),
		]

		barriers = flat_barriers(rows, LOW_THRESHOLDS)
		self.assertEqual(len(barriers), 4)


class BarrierCacheFlushTests(unittest.TestCase):

	def test_cache_flushes_in_batches_around_the_configured_size(self):
		# A single long mainstem with vertices every 10m, steeply climbing, so every
		# resolvable vertex clears LOW_THRESHOLDS and becomes a barrier.
		n_vertices = 20
		vertices_downstream_to_upstream = [
			point_at(i * 10.0, i * 10.0 * 0.5) for i in range(n_vertices)
		]
		storage_order = list(reversed(vertices_downstream_to_upstream))
		rows = [edge_row("big-edge", "big-mainstem", 1, storage_order)]

		with mock.patch.object(cb, "BARRIER_CACHE_SIZE", 3):
			total, batches = run_compute_barriers(rows, LOW_THRESHOLDS)

		self.assertGreater(total, 3)
		self.assertGreater(len(batches), 1)
		# Every flush except the final (trailing leftovers) fires only once the cache has
		# reached the configured size -- it can occasionally overshoot slightly if a single
		# vertex resolves more than one queued barrier at once, so this checks the flush
		# trigger fired promptly rather than asserting an exact upper bound.
		for batch in batches[:-1]:
			self.assertGreaterEqual(len(batch), 3)
		self.assertEqual(sum(len(batch) for batch in batches), total)


# ---------------------------------------------------------------------------
# Real-world WKT fixtures
# ---------------------------------------------------------------------------
WKT_EDGE1 = "LINESTRING M (-121.57772709999999 49.750451999999996 1170.0229913152182, -121.5775213 49.7504122 1171.2576989714273, -121.5772027 49.7503993 1173.0924816417173, -121.5767963 49.7505204 1175.6647531365957, -121.5765967 49.7506984 1177.6200855468862, -121.5764947 49.750867899999996 1179.2378520861357, -121.5764655 49.750895 1179.5315895316153, -121.5763959 49.7508935 1179.9318420935306, -121.57609459999999 49.7507725 1181.9708364248295, -121.5757654 49.750642899999995 1184.1865003392272, -121.575667 49.7506414 1184.752220434949, -121.5754992 49.7506756 1185.7635116639033, -121.5754429 49.7507112 1186.216279006282, -121.5753857 49.750800999999996 1187.0801112217412, -121.57540669999999 49.751016 1188.9964713802294, -121.57530469999999 49.7511855 1190.6142365283608, -121.57494639999999 49.7511015 1192.804951937868, -121.57465739999999 49.751034499999996 1194.569681835381, -121.57455909999999 49.7510331 1195.1348024680324, -121.5742377 49.7511371 1197.2007773271907, -121.57419469999999 49.7511727 1197.6024843108096, -121.5742016 49.751370599999994 1199.363371455064, -121.57410429999999 49.751404799999996 1200.0)"
WKT_EDGE2 = "LINESTRING M (-121.58477909999999 49.7480736 1110.9347596873101, -121.58464049999999 49.748080699999996 1111.7339172679183, -121.58429199999999 49.7481035 1113.7472988451466, -121.5839575 49.7482075 1115.8809665332385, -121.5836892 49.748329999999996 1117.769275053229, -121.5834491 49.748479499999995 1119.6858293796101, -121.58320889999999 49.748629 1121.6027945705468, -121.5829972 49.7487715 1123.3599242656762, -121.58279739999999 49.748912499999996 1125.0605320888646, -121.58258389999999 49.749099 1127.124092955208, -121.582568 49.7491973 1128.0032937545268, -121.5827008 49.749361099999994 1129.6482162767995, -121.5828219 49.7495334 1131.3315744868796, -121.582815 49.749739899999994 1133.1689452788867, -121.58277149999999 49.749802599999995 1133.780175516194, -121.5826754 49.749819699999996 1134.3530912704737, -121.5823418 49.7497798 1136.3030943396916, -121.5820815 49.7497129 1137.9132349937313, -121.5816522 49.749644499999995 1140.4546369199084, -121.581485 49.749670099999996 1141.4422664971203, -121.5812589 49.7498297 1143.3669737374992, -121.58105959999999 49.7499621 1145.0099464950229, -121.5810065 49.75 1145.4647139588858, -121.580862 49.7501031 1146.7020221064238, -121.5806489 49.750244099999996 1148.455136716634, -121.5804371 49.750367999999995 1150.0973012865904, -121.5801699 49.750526099999995 1152.179730722186, -121.579984 49.7506771 1153.8961015631282, -121.579771 49.7508181 1155.6488045398032, -121.57950439999999 49.750967599999996 1157.6777344539944, -121.5791438 49.750954799999995 1159.7534266405635, -121.5788517 49.7509605 1196.3464779731153, -121.57857489999999 49.7509477 1163.0280435845339, -121.57850579999999 49.750937699999994 1163.4350398931529, -121.5784638 49.7509192 1163.7271965571833, -121.5783725 49.750729799999995 1165.4918524633426, -121.5781415 49.7505831 1167.35350618631, -121.57783739999999 49.7504705 1223.0136030424017, -121.57772709999999 49.750451999999996 1170.0229913152182)"
WKT_EDGE3 = "LINESTRING M (-121.5882759 49.745750699999995 1107.8717378685049, -121.5878982 49.745871799999996 1080.1841770738467, -121.587656 49.7460398 1082.2266359747493, -121.58745789999999 49.746207899999995 1084.1061894437921, -121.58721519999999 49.746403 1086.3329087732313, -121.5871712 49.7464927 1087.1699679569285, -121.58718549999999 49.746528299999994 1087.4971451673073, -121.5871514 49.7467434 1089.4206032646723, -121.586896 49.746911399999995 1091.5154984437918, -121.58662899999999 49.7470168 1093.3139592141015, -121.5863887 49.747147899999995 1095.121692064578, -121.5861643 49.7472632 1096.7696298998953, -121.58595109999999 49.747385699999995 1098.409522499559, -121.5859083 49.747439799999995 1098.9500095592266, -121.5859065 49.747484 1099.3433314667964, -121.5860405 49.747701899999996 1101.429106606254, -121.5858945 49.7479611 1103.882816706641, -121.58561189999999 49.7481192 1106.031416071776, -121.5854579 49.7481448 1106.945414766374, -121.5851264 49.7480864 1108.9203901163053, -121.58495959999999 49.7480665 1109.8953442862173, -121.58477909999999 49.7480736 1110.9347596873101)"
WKT_EDGE4 = "LINESTRING M (-121.5963767 49.7521725 1116.099731496357, -121.5961123 49.752232299999996 1117.7097637275656, -121.5958318 49.7523007 1119.4329100113412, -121.59551049999999 49.7524046 1121.497929531631, -121.595203 49.7525 1123.4584403031117, -121.5949644 49.7525684 1124.9586884723176, -121.5946719 49.7526197 1126.7006282813575, -121.59430959999999 49.752651 1128.8014270102358, -121.5940167 49.7526467 1130.4852605647072, -121.59371139999999 49.752652399999995 1132.2406591808935, -121.59346239999999 49.752641 1133.6753381314104, -121.59340809999999 49.752639599999995 1133.9876677227176, -121.5931303 49.7526097 1135.6062855120056, -121.59282599999999 49.752561299999996 1137.4074229520365, -121.5925369 49.752494299999995 1139.1726467688634, -121.5922329 49.7524003 1141.109634927172, -121.5919457 49.7523078 1142.9540085987237, -121.59167099999999 49.7522052 1144.777643069431, -121.59142329999999 49.7520756 1146.609539222503, -121.59119229999999 49.7519474 1148.3597447485304, -121.5909453 49.7518278 1150.1337879101884, -121.590713 49.7516982 1151.8977871433237, -121.5905083 49.7515515 1153.6548178866963, -121.5903197 49.751396299999996 1155.4101117207406, -121.59015679999999 49.7512425 1157.0679472250179, -121.5899953 49.7510787 1158.7955884455162, -121.5898325 49.750932 1160.401366233331, -121.58965719999999 49.750776699999996 1162.1112378346888, -121.5894393 49.750630099999995 1163.9193215621008, -121.58917919999999 49.7505289 1165.6643998831491, -121.5888773 49.7504164 1167.667507869661, -121.5885746 49.7503053 1169.6684232870684, -121.5882998 49.7501843 1171.5797662771802, -121.58809529999999 49.750056099999995 1173.2174788842344, -121.5880043 49.75 1173.9403981912478, -121.5879174 49.7499464 1174.6309162178147, -121.5877561 49.7498012 1176.2208391489773, -121.58758119999999 49.749618899999994 1178.1288180301422, -121.5874063 49.7494181 1180.1785073360975, -121.587216 49.7492358 1182.1345747328921, -121.58701479999999 49.749053499999995 1184.126354916797, -121.586782 49.748879699999996 1186.1710459431627, -121.5865936 49.748743 1187.799348979266, -121.58631829999999 49.748649 1189.5890746869559, -121.58602769999999 49.7485735 1191.389362779998, -121.5856968 49.7485066 1193.3822533828163, -121.58536699999999 49.748404 1195.4861671694548, -121.5852986 49.7483855 1195.9123741115777, -121.5849985 49.7482288 1198.1301368343716, -121.58477909999999 49.7480736 1200.0)"
WKT_EDGE5 = "LINESTRING M (-121.58075989999999 49.7416832 1112.0485570361311, -121.5807715 49.7417458 1112.6094004913696, -121.5808242 49.7418997 1228.7248673362944, -121.5808728 49.7420791 1115.631696293556, -121.58088509999999 49.7421517 1116.2813765116937, -121.5809089 49.742268499999994 1117.32935261618, -121.58093059999999 49.742493499999995 1119.3347476159297, -121.5809238 49.7427371 1121.502069884692, -121.5809312 49.742926499999996 1123.1874346731142, -121.5809328 49.7429351 1123.264487803127, -121.5809403 49.7431245 1124.949867194468, -121.58093439999999 49.7432954 1126.4705046056883, -121.5809439 49.743466299999994 1127.9917442726526, -121.5808977 49.743645699999995 1129.609563212094, -121.5808349 49.7438423 1131.3953092637287, -121.580761 49.7440388 1133.1941747562255, -121.58067229999999 49.7442268 1134.9425488412185, -121.5805704 49.7444148 1136.714537678274, -121.5804678 49.7445928 1138.4042268619428, -121.5803802 49.7447452 1139.8504149113678, -121.58031749999999 49.744960299999995 1141.7975080833028, -121.58032709999999 49.745131199999996 1143.3187684261861, -121.580292 49.7453291 1145.0907331675298, -121.58021919999999 49.745471599999995 1146.4256441186494, -121.58020479999999 49.7455072 1146.7529666528853, -121.5801039 49.745641 1148.0769921756357, -121.58001639999999 49.7457934 1149.5229763943848, -121.57999729999999 49.7459643 1151.0471947601152, -121.5800592 49.746162299999995 1152.8441042979491, -121.5801113 49.746324699999995 1154.319465150005, -121.58014849999999 49.746478499999995 1155.704218402726, -121.580198 49.746686399999994 1157.575374380788, -121.58027489999999 49.746911499999996 1159.6259829518929, -121.58035129999999 49.7470923 1161.2931850881766, -121.5804173 49.7472646 1162.8721496617925, -121.5804794 49.747481099999995 1164.8308479423067, -121.58054369999999 49.747697599999995 1166.7918898294815, -121.5806472 49.7479412 1169.0390381844218, -121.58075249999999 49.748159099999995 1171.0696889782932, -121.58077209999999 49.748384099999996 1173.0743684889671, -121.5806557 49.7485892 1175.0176611257623, -121.5804979 49.7487487 1176.7016426185273, -121.5803414 49.7489096 1178.3921410399976, -121.5801152 49.7490691 1180.3165942327776, -121.57988619999999 49.749237199999996 1182.3087184426101, -121.5796742 49.7494138 1184.2968642288338, -121.57946129999999 49.749573299999994 1186.170510871575, -121.5792482 49.7497143 1187.9236348287566, -121.5790358 49.7498467 1189.619965430588, -121.5787962 49.75 1191.5580546545154, -121.5787273 49.75 1191.9540687468862, -121.57842169999999 49.749964999999996 1193.737936132185, -121.57828769999999 49.75 1194.5686737597066, -121.57812729999999 49.7500419 1195.563093350023, -121.57791549999999 49.7501658 1197.2052616819603, -121.57774339999999 49.7503794 1199.3474190559057, -121.57772709999999 49.750451999999996 1200.0)"
REALWORLD_THRESHOLDS = [
	{"code": "chn", "spawning_max": 0.16, "rearing_max": 0.11},
	{"code": "as", "spawning_max": 0.2, "rearing_max": 0.145},
]

def wkb_from_wkt(wkt):
	"""Parse real WKT text directly (rather than building it via make_wkb from raw
	coordinate tuples), mirroring what ST_AsBinary on a real geometry produces."""
	return shapely.to_wkb(shapely.from_wkt(wkt))


class RealWorldMultiSpeciesTests(unittest.TestCase):
	"""End-to-end test against real (non-synthetic) river geometry spanning 3 mainstems,
	read from WKT text, with 2 species/4 thresholds active at once. See the fixture
	comment above WKT_EDGE1 for how elevation was assigned to make specific vertices'
	OWN reported gradient hit an exact target value, and why nearby vertices can still
	end up reporting elevated gradients too."""

	def test_real_world_mainstems(self):
		rows = [
			("edge1", "mainstem-1", 1, DEFAULT_AOI_ID, wkb_from_wkt(WKT_EDGE1)),
			("edge2", "mainstem-1", 2, DEFAULT_AOI_ID, wkb_from_wkt(WKT_EDGE2)),
			("edge3", "mainstem-1", 3, DEFAULT_AOI_ID, wkb_from_wkt(WKT_EDGE3)),
			("edge4", "mainstem-2", 1, DEFAULT_AOI_ID, wkb_from_wkt(WKT_EDGE4)),
			("edge5", "mainstem-3", 1, DEFAULT_AOI_ID, wkb_from_wkt(WKT_EDGE5)),
		]

		barriers = flat_barriers(rows, REALWORLD_THRESHOLDS)

		expected = [
			# edge 1, vertex 5 -- engineered to hit exactly 0.25.
			(-121.5765967, 49.7506984, 0.25, ["chn_spawn", "chn_rear", "as_spawn", "as_rear"]),
			# edge 2's last vertex (== edge 1's first vertex) -- engineered to hit exactly 0.14.
			(-121.57772709999999, 49.750451999999996, 0.14, ["chn_rear"]),
			# edge 3, vertex 5 -- engineered to hit exactly 0.15
			(-121.58721519999999, 49.746403, 0.15, ["chn_rear", "as_rear"]),
			# edge 5, vertex 10 -- engineered to hit exactly 0.18.
			(-121.5809328, 49.7429351, 0.18, ["chn_spawn", "chn_rear", "as_rear"]),
			# Collateral effect of the edge 5 vertex 10 feature: these two neighboring
			# vertices' own 100m windows also fully contain it, and see an even larger
			# share of it than vertex 10 itself does.
			(-121.5809312, 49.742926499999996, 0.2345570549850936, ["chn_spawn", "chn_rear", "as_spawn", "as_rear"]),
			(-121.5809238, 49.7427371, 0.6503128495868623, ["chn_spawn", "chn_rear", "as_spawn", "as_rear"]),
		]

		self.assertEqual(len(barriers), len(expected))
		for (lon, lat, gradient, species), (exp_lon, exp_lat, exp_gradient, exp_species) in zip(barriers, expected):
			self.assertAlmostEqual(lon, exp_lon, places=9)
			self.assertAlmostEqual(lat, exp_lat, places=9)
			self.assertAlmostEqual(gradient, exp_gradient)
			self.assertEqual(species, exp_species)


class LoadSpeciesParametersTests(unittest.TestCase):

	def _write_params(self, tmp, yaml_text):
		params_path = Path(tmp) / "fish_species_parameters.yaml"
		params_path.write_text(yaml_text)
		return params_path

	def test_valid_thresholds_are_parsed(self):
		with tempfile.TemporaryDirectory() as tmp:
			params_path = self._write_params(tmp, """
species:
  - code: chn
    accessibility_gradient_spawning_max: 0.16
    accessibility_gradient_rearing_max: 0.11
""")
			species = cb.load_species_parameters(params_path)
			self.assertEqual(species, [{"code": "chn", "spawning_max": 0.16, "rearing_max": 0.11}])

	def test_blank_threshold_is_allowed(self):
		with tempfile.TemporaryDirectory() as tmp:
			params_path = self._write_params(tmp, """
species:
  - code: chn
    accessibility_gradient_spawning_max: 0.16
""")
			species = cb.load_species_parameters(params_path)
			self.assertIsNone(species[0]["rearing_max"])

	def test_non_numeric_threshold_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			params_path = self._write_params(tmp, """
species:
  - code: chn
    accessibility_gradient_spawning_max: "not-a-number"
""")
			with self.assertRaises(SystemExit):
				cb.load_species_parameters(params_path)

	def test_out_of_range_threshold_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			params_path = self._write_params(tmp, """
species:
  - code: chn
    accessibility_gradient_rearing_max: 1.5
""")
			with self.assertRaises(SystemExit):
				cb.load_species_parameters(params_path)

	def test_negative_threshold_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			params_path = self._write_params(tmp, """
species:
  - code: chn
    accessibility_gradient_rearing_max: -0.1
""")
			with self.assertRaises(SystemExit):
				cb.load_species_parameters(params_path)


class LoadAoiConfigTests(unittest.TestCase):

	def test_missing_file_means_full_run(self):
		self.assertEqual(cb.load_aoi_config(Path("/no/such/gradient_barriers.ini")), [])

	def test_blank_short_names_means_full_run(self):
		with tempfile.TemporaryDirectory() as tmp:
			config_path = Path(tmp) / "gradient_barriers.ini"
			config_path.write_text("[aoi]\nshort_names =\n")
			self.assertEqual(cb.load_aoi_config(config_path), [])

	def test_populated_short_names_are_parsed_and_stripped(self):
		with tempfile.TemporaryDirectory() as tmp:
			config_path = Path(tmp) / "gradient_barriers.ini"
			config_path.write_text("[aoi]\nshort_names = 08MF001, 08MF002\n")
			self.assertEqual(cb.load_aoi_config(config_path), ["08MF001", "08MF002"])

	def test_invalid_short_name_exits(self):
		with tempfile.TemporaryDirectory() as tmp:
			config_path = Path(tmp) / "gradient_barriers.ini"
			config_path.write_text("[aoi]\nshort_names = 08MF001; DROP TABLE support.gradient_barriers;\n")
			with self.assertRaises(SystemExit):
				cb.load_aoi_config(config_path)


class FetchEdgesAoiFilterTests(unittest.TestCase):

	def test_no_aoi_ids_omits_mainstem_filter(self):
		conn = FakeConnection([])
		cursor = cb.fetch_edges(conn)
		self.assertNotIn("mainstem_id = ANY", cursor.last_sql)

	def test_aoi_ids_adds_mainstem_subquery_filter(self):
		conn = FakeConnection([])
		cursor = cb.fetch_edges(conn, aoi_ids=["aoi-1", "aoi-2"])
		self.assertIn("aoi_id = ANY(%(aoi_ids)s)", cursor.last_sql)
		self.assertEqual(cursor.last_params, {"aoi_ids": ["aoi-1", "aoi-2"]})


if __name__ == "__main__":
	unittest.main()
