"""Tests for fishpass_engine/scripts/cabd_client.py against a fake requests session (no real
network access).

Run with: python -m unittest fishpass_engine.tests.test_cabd_client
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cabd_client as cc  # noqa: E402


class FakeResponse:
	def __init__(self, features):
		self._features = features

	def raise_for_status(self):
		pass

	def json(self):
		return {"type": "FeatureCollection", "features": self._features}


class FakeSession:
	def __init__(self, features_by_chunk):
		"""features_by_chunk: list of feature-lists, one per expected request, returned in order."""
		self._features_by_chunk = list(features_by_chunk)
		self.urls = []

	def get(self, url, timeout=None):
		self.urls.append(url)
		return FakeResponse(self._features_by_chunk.pop(0))


class FetchFeatureTypeTests(unittest.TestCase):
	def test_single_chunk(self):
		session = FakeSession([[{"id": 1}, {"id": 2}]])
		features = list(cc.fetch_feature_type("dams", ["AOI1", "AOI2"], session=session))
		self.assertEqual(features, [{"id": 1}, {"id": 2}])
		self.assertIn("features/dams", session.urls[0])
		self.assertIn("nhn_watershed_id:in:AOI1;AOI2", session.urls[0])

	def test_chunks_by_chunk_size(self):
		session = FakeSession([[{"id": 1}], [{"id": 2}]])
		features = list(cc.fetch_feature_type("dams", ["AOI1", "AOI2", "AOI3"], chunk_size=2, session=session))
		self.assertEqual(len(session.urls), 2)
		self.assertEqual(features, [{"id": 1}, {"id": 2}])

	def test_truncated_result_exits(self):
		session = FakeSession([[{"id": i} for i in range(cc.RESULT_CAP)]])
		with self.assertRaises(SystemExit):
			list(cc.fetch_feature_type("dams", ["AOI1"], session=session))

	def test_use_analysis_false_is_filtered_out(self):
		session = FakeSession([[
			{"id": 1, "properties": {"use_analysis": True}},
			{"id": 2, "properties": {"use_analysis": False}},
			{"id": 3, "properties": {"use_analysis": None}},
			{"id": 4, "properties": {}},
			{"id": 5},
		]])
		features = list(cc.fetch_feature_type("dams", ["AOI1"], session=session))
		self.assertEqual([f["id"] for f in features], [1, 3, 4, 5])


class MapPassabilityTests(unittest.TestCase):
	def test_known_and_unknown_codes(self):
		self.assertEqual(cc.map_passability(3), 1)
		self.assertEqual(cc.map_passability(1), 0)
		self.assertEqual(cc.map_passability(None), 0)


if __name__ == "__main__":
	unittest.main()
