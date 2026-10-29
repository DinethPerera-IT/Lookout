import pytest

from lookout import config


@pytest.fixture
def cfg():
    return config.load()
