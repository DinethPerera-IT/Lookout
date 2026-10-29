import pytest

from ops_summary import config


@pytest.fixture
def cfg():
    return config.load()
