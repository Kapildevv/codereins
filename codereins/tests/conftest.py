"""Shared fixtures. Every test runs with CODEREINS_HOME in a temp folder, so
no test can touch the real ~/.codereins (window registry, session, backups)."""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("cli-home")
    old = os.environ.get("CODEREINS_HOME")
    os.environ["CODEREINS_HOME"] = str(home)
    yield home
    if old is None:
        os.environ.pop("CODEREINS_HOME", None)
    else:
        os.environ["CODEREINS_HOME"] = old


@pytest.fixture
def tmp_dir(tmp_path):
    return str(tmp_path)
