from pathlib import Path

import pytest


@pytest.fixture
def fixture_bytes():
    return lambda name: (Path(__file__).parent / "fixtures" / name).read_bytes()
