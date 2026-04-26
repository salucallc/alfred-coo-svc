import pytest

@pytest.fixture(scope="session")
def docker_compose_file():
    return "tests/fleet/e2e/docker-compose.fleet-e2e.yml"
