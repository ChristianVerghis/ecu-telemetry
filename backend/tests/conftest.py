import pytest


@pytest.fixture
def db(tmp_path):
    from app.db import Database
    d = Database(str(tmp_path / "t.db"))
    yield d
    d.close()
