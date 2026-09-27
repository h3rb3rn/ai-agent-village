"""P30: deterministic tools (calc, unit_convert, subnet_info, hash_digest, stats_summary).

No network, no filesystem, no eval(). One of the fixtures is the exact real-world
motivation: chronicler misread ram_available_mib=13216 as "below the 4096 MiB reserve"
three times on 2026-09-27 (docs/evidence/P21.21.md); unit_convert lets an agent verify
13216 MiB is ~12.9 GiB, well above a 4 GiB reserve, instead of guessing.
"""
import unittest

from village.tools import call_tool, calc, hash_digest, stats_summary, subnet_info, unit_convert


class CalcTests(unittest.TestCase):
    def test_operator_precedence_and_parens(self):
        self.assertEqual(calc("2 + 3 * 4")["result"], 14)
        self.assertEqual(calc("(2 + 3) * 4")["result"], 20)

    def test_division_by_zero_is_a_value_error_not_a_crash(self):
        with self.assertRaises(ValueError):
            calc("1 / 0")

    def test_no_code_execution_is_possible(self):
        for payload in ("__import__('os').system('id')", "open('/etc/passwd').read()", "[x for x in range(3)]", "'a' * 3"):
            with self.assertRaises(ValueError):
                calc(payload)

    def test_oversized_expression_is_rejected(self):
        with self.assertRaises(ValueError):
            calc("1+" * 500 + "1")

    def test_huge_exponent_is_rejected_not_hung(self):
        with self.assertRaises(ValueError):
            calc("2 ** 9999999999")


class UnitConvertTests(unittest.TestCase):
    def test_mib_to_gib_matches_the_chronicler_incident(self):
        result = unit_convert(13216, "MiB", "GiB")["result"]
        self.assertAlmostEqual(result, 12.90625, places=4)
        self.assertGreater(result * 1024, 4096)  # well above the 4 GiB reserve, not below it

    def test_celsius_fahrenheit_round_trip(self):
        self.assertAlmostEqual(unit_convert(unit_convert(100, "celsius", "fahrenheit")["result"], "fahrenheit", "celsius")["result"], 100, places=6)

    def test_unsupported_pair_is_rejected(self):
        with self.assertRaises(ValueError):
            unit_convert(1, "mib", "celsius")

    def test_non_numeric_value_is_rejected(self):
        with self.assertRaises(ValueError):
            unit_convert("not a number", "mib", "gib")


class SubnetInfoTests(unittest.TestCase):
    def test_v4_slash_24(self):
        info = subnet_info("192.168.155.0/24")
        self.assertEqual(info["usable_hosts"], 254)
        self.assertEqual(info["broadcast_address"], "192.168.155.255")
        self.assertTrue(info["is_private"])

    def test_v4_slash_31_has_no_broadcast_reservation(self):
        self.assertEqual(subnet_info("10.0.0.0/31")["usable_hosts"], 2)

    def test_v6_is_supported(self):
        info = subnet_info("2001:db8::/64")
        self.assertEqual(info["version"], 6)
        self.assertIsNone(info["broadcast_address"])

    def test_invalid_cidr_is_rejected(self):
        with self.assertRaises(ValueError):
            subnet_info("not-a-cidr")

    def test_host_bits_set_is_normalized_not_rejected(self):
        self.assertEqual(subnet_info("192.168.1.5/24")["network_address"], "192.168.1.0")


class HashDigestTests(unittest.TestCase):
    def test_known_sha256_vector(self):
        self.assertEqual(hash_digest("hello")["digest"], "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")

    def test_unsupported_algorithm_is_rejected(self):
        with self.assertRaises(ValueError):
            hash_digest("x", "md5")


class StatsSummaryTests(unittest.TestCase):
    def test_basic_summary(self):
        summary = stats_summary([1, 2, 3, 4, 5])
        self.assertEqual((summary["mean"], summary["median"], summary["min"], summary["max"]), (3.0, 3.0, 1.0, 5.0))

    def test_single_value_has_zero_stdev(self):
        self.assertEqual(stats_summary([42])["stdev"], 0.0)

    def test_empty_list_is_rejected(self):
        with self.assertRaises(ValueError):
            stats_summary([])

    def test_oversized_list_is_rejected(self):
        with self.assertRaises(ValueError):
            stats_summary(list(range(1000)))

    def test_non_numeric_element_is_rejected(self):
        with self.assertRaises(ValueError):
            stats_summary([1, "two", 3])


class DispatchTests(unittest.TestCase):
    def test_call_tool_routes_by_name(self):
        self.assertEqual(call_tool("calc", {"expression": "1+1"})["result"], 2)

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ValueError):
            call_tool("delete_everything", {})

    def test_non_dict_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            call_tool("calc", "1+1")

    def test_unexpected_keyword_argument_is_rejected(self):
        with self.assertRaises(TypeError):
            call_tool("calc", {"expression": "1+1", "bogus": True})


if __name__ == "__main__":
    unittest.main()
