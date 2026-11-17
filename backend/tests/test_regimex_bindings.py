"""Bounded metadata reuse does not retain observations or renew stale digests."""

import unittest

from backend.app.integrations.regimex.bindings import VerifiedExports


class VerifiedExportTests(unittest.TestCase):
    def test_ttl_is_not_extended_by_reads_or_repeated_remembering(self):
        now = [0.0]
        cache = VerifiedExports(ttl=60, clock=lambda: now[0])
        export = {"source": {"run_id": "1"}, "time_grid": {}, "instruments": [],
                  "content_sha256": "a" * 64, "observations": [{"step": 0}]}
        cache.remember("run", export)
        now[0] = 59
        value = cache.get("run")
        self.assertNotIn("observations", value)
        value["source"]["run_id"] = "other"
        self.assertEqual(cache.get("run")["source"]["run_id"], "1")
        cache.remember("run", export)
        now[0] = 60
        self.assertIsNone(cache.get("run"))
        cache.remember("run", export)
        self.assertIsNotNone(cache.get("run"))

    def test_capacity_evicts_oldest_unused_binding(self):
        cache = VerifiedExports(capacity=2)
        export = {"source": {}, "time_grid": {}, "instruments": [], "content_sha256": "a" * 64}
        cache.remember("first", export)
        cache.remember("second", export)
        self.assertIsNotNone(cache.get("first"))
        cache.remember("third", export)
        self.assertIsNone(cache.get("second"))
        self.assertIsNotNone(cache.get("first"))
        self.assertIsNotNone(cache.get("third"))
