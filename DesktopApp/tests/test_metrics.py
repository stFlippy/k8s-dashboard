import unittest

from quaber.metrics import ThrottleSamples, cpu_cores, memory_bytes


class MetricsTests(unittest.TestCase):
    def test_quantities_and_counter_deltas(self):
        self.assertEqual(cpu_cores("250m"), 0.25)
        self.assertEqual(cpu_cores("1000000n"), 0.001)
        self.assertEqual(memory_bytes("2Mi"), 2 * 1024 * 1024)
        self.assertEqual(memory_bytes("1G"), 1_000_000_000)
        self.assertIsNone(memory_bytes("2Xi"))
        samples = ThrottleSamples()
        def text(periods, throttled):
            labels = '{namespace="default",pod="example",container="main"}'
            return f"container_cpu_cfs_periods_total{labels} {periods}\ncontainer_cpu_cfs_throttled_periods_total{labels} {throttled}\n"
        self.assertIsNone(samples.parse(text(100, 10), "cluster", "default", "example")["main"])
        self.assertEqual(samples.parse(text(200, 30), "cluster", "default", "example")["main"], 20)


if __name__ == "__main__":
    unittest.main()
