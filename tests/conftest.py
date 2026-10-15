import pytest

from support import boot


@pytest.fixture(scope="module")
def economy(tmp_path_factory):
    with boot(tmp_path_factory.mktemp("pacific") / "home", seed=1) as env:
        yield env
