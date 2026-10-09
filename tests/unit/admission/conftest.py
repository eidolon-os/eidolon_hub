"""Fixtures shared by the admission suite.

The Authority harness is defined beside the characterisation tests that first
needed it; re-exporting it here lets a sibling module use it as a fixture
without importing a name it then appears to shadow.
"""

from test_admission_authority import harness  # noqa: F401
