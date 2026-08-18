"""Tests for fishpass_engine/scripts/compute_statistics.py's steps 3-4 SQL shape against a
stubbed cursor (no database).

Run with: python -m unittest fishpass_engine.tests.test_compute_statistics
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

for _module_name in ("psycopg",):
	if _module_name not in sys.modules:
		try:
			__import__(_module_name)
		except ImportError:
			sys.modules[_module_name] = types.ModuleType(_module_name)

import compute_statistics as cs  # noqa: E402


class FakeCursor:
	def __init__(self):
		self.executed = []

	def execute(self, sql, params=None):
		self.executed.append((" ".join(sql.split()), params))


class ComputeEffectiveLengthTests(unittest.TestCase):
	def test_null_ecatchment_or_mainstem_keeps_own_length(self):
		cursor = FakeCursor()
		cs.compute_effective_length(cursor, "model_test")
		sql0, _ = cursor.executed[0]
		self.assertIn("SET effective_length = length", sql0)
		self.assertIn("WHERE ecatchment_id IS NULL OR mainstem_id IS NULL", sql0)

	def test_defaults_everything_else_to_zero_then_restores_best_mainstem(self):
		cursor = FakeCursor()
		cs.compute_effective_length(cursor, "model_test")
		sql1, _ = cursor.executed[1]
		self.assertIn("SET effective_length = 0", sql1)

		sql2, _ = cursor.executed[2]
		self.assertIn("SUM(length) AS total_length", sql2)
		self.assertIn("ORDER BY ecatchment_id, total_length DESC, mainstem_id", sql2)
		self.assertIn("SET effective_length = s.length", sql2)


class ComputeSegmentGradientTests(unittest.TestCase):
	def test_query_shape(self):
		cursor = FakeCursor()
		cs.compute_segment_gradient(cursor, "model_test")
		sql, _ = cursor.executed[0]
		self.assertIn("ST_M(ST_PointN(geometry, 1))", sql)
		self.assertIn("ST_M(ST_PointN(geometry, ST_NPoints(geometry)))", sql)
		self.assertIn(f"!= {cs.NO_DATA}", sql)
		self.assertIn("SET segment_gradient", sql)


if __name__ == "__main__":
	unittest.main()
