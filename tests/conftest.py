import pytest

from myapp.config import Config
from myapp.db import open_db


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=tmp_path / "test.db", secure_cookies=False)


@pytest.fixture
def db(cfg):
    database = open_db(cfg.db_path)
    yield database
    database.conn.close()
