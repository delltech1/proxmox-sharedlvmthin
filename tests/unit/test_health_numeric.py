import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json"


def load_numeric_helpers():
    tree = ast.parse(
        COLLECTOR.read_text(encoding="utf-8"), filename=str(COLLECTOR)
    )
    wanted = {
        "exact_decimal", "exact_byte_count", "percentage_decimal",
        "percentage_bytes",
    }
    body = [
        node for node in tree.body
        if (
            isinstance(node, ast.ImportFrom) and node.module == "decimal"
        ) or (
            isinstance(node, ast.FunctionDef) and node.name in wanted
        )
    ]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(COLLECTOR), "exec"), namespace)
    return namespace


class HealthNumericTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_numeric_helpers()

    def test_byte_counts_remain_exact_above_binary_float_precision(self):
        exact = self.helpers["exact_byte_count"]
        self.assertEqual(exact(0), 0)
        self.assertEqual(exact("9007199254740993.00"), 2**53 + 1)
        self.assertEqual(exact(f"{128 * 2**50}.00"), 128 * 2**50)

    def test_invalid_byte_counts_fail_instead_of_rounding(self):
        exact = self.helpers["exact_byte_count"]
        for value in ("1.5", "-1", "NaN", "Infinity", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                exact(value)

    def test_petabyte_payload_calculation_uses_decimal_percentage(self):
        payload = self.helpers["percentage_bytes"]
        size = 128 * 2**50 + 512
        self.assertEqual(payload(size, "12.50"), size // 8)
        self.assertIsNone(payload(size, "101"))
        self.assertIsNone(payload(size, ""))


if __name__ == "__main__":
    unittest.main()
