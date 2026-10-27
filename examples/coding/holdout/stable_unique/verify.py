import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("solution", Path.cwd() / "solution.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.stable_unique([3, 1, 3, 2, 1]) == [3, 1, 2]
assert module.stable_unique([]) == []
source = [[1], {"a": 2}, [1], {"a": 2}, [2]]
assert module.stable_unique(source) == [[1], {"a": 2}, [2]]
assert source == [[1], {"a": 2}, [1], {"a": 2}, [2]]
assert module.stable_unique(x for x in ["a", "b", "a"]) == ["a", "b"]
print("All stable-unique checks passed")
