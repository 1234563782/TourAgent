import unittest

from business_travel.policy_hnsw import hnsw_index_name, render_index_ddl, validate_hnsw_parameters
from scripts.policy_hnsw_ddl import render_ddl


class HnswDdlTests(unittest.TestCase):
    def test_render_uses_global_cosine_index_and_dimension(self):
        ddl = render_index_ddl(1024, m=16, ef_construction=128)
        self.assertIn("policy_chunks_embedding_hnsw_1024", ddl)
        self.assertIn("embedding::vector(1024)", ddl)
        self.assertIn("vector_cosine_ops", ddl)
        self.assertIn("m = 16", ddl)
        self.assertIn("ef_construction = 128", ddl)

    def test_script_wrapper_matches_runtime_ddl(self):
        self.assertEqual(render_ddl(3), render_index_ddl(3))
        self.assertEqual(hnsw_index_name(3), "policy_chunks_embedding_hnsw_3")

    def test_invalid_parameters_rejected(self):
        with self.assertRaises(ValueError):
            validate_hnsw_parameters(0, m=16, ef_construction=128, ef_search=80)
        with self.assertRaises(ValueError):
            validate_hnsw_parameters(1024, m=16, ef_construction=8, ef_search=80)
        with self.assertRaises(ValueError):
            validate_hnsw_parameters(1024, m=16, ef_construction=128, ef_search=0)


if __name__ == "__main__":
    unittest.main()
