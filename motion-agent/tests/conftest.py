import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.templates import get_meta, load_sample_input  # noqa: E402


@pytest.fixture
def kt_meta():
    return get_meta("kinetic_teaser", 1)


@pytest.fixture
def kt_input():
    return load_sample_input("kinetic_teaser", 1, "date-place")
