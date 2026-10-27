"""Public development regression; final variants remain in verify.py."""

import runpy
from decimal import Decimal

total_amounts = runpy.run_path("solution.py")["total_amounts"]
rows = [{"amount": "12.50"}, {"amount": "-2.25"}]
actual = total_amounts(rows)
expected = Decimal("10.25")
assert actual == expected, f"Signed amounts must include credits: expected {expected}, got {actual}"
print("Public signed-credit regression succeeded")
