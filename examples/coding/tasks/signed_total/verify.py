import importlib.util
from decimal import Decimal
from pathlib import Path

spec = importlib.util.spec_from_file_location("solution", Path.cwd() / "solution.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rows = [{"amount": "100.25"}, {"amount": "-20.10"}, {"amount": "5.00"}]
assert module.total_amounts(rows) == Decimal("85.15")
assert rows == [{"amount": "100.25"}, {"amount": "-20.10"}, {"amount": "5.00"}]
assert module.total_amounts([]) == Decimal("0")
assert isinstance(module.total_amounts([]), Decimal)
assert module.total_amounts([{"amount": "0.1"}, {"amount": "0.2"}]) == Decimal("0.3")
for row in ({}, {"amount": "bad"}, {"amount": "NaN"}, {"amount": "Infinity"},
            {"amount": "-Infinity"}, {"amount": "sNaN"}):
    try:
        module.total_amounts([row])
    except ValueError:
        pass
    else:
        raise AssertionError(f"Expected ValueError for {row!r}")
print("All signed-total checks passed")
