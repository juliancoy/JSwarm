import unittest
from unittest.mock import patch
from benchmark import Benchmark


class BenchmarkTests(unittest.TestCase):
    def test_single_session_budget_and_expiry(self):
        clock = [100]
        with patch('benchmark.time.monotonic', side_effect=lambda: clock[0]):
            benchmark = Benchmark()
            token = benchmark.start()
            self.assertIsNone(benchmark.start())
            self.assertFalse(benchmark.claim('wrong', 'download'))
            self.assertFalse(benchmark.claim(token, 'unknown'))
            for _ in range(3):
                self.assertTrue(benchmark.claim(token, 'download'))
            self.assertFalse(benchmark.claim(token, 'download'))
            self.assertTrue(benchmark.claim(token, 'upload'))
            clock[0] = 146
            self.assertFalse(benchmark.claim(token, 'ping'))
            self.assertNotEqual(benchmark.start(), token)


if __name__ == '__main__':
    unittest.main()
