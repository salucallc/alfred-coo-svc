import pathlib

def test_compose_contains_soul_svc():
    compose_path = pathlib.Path(__file__).parents[1] / 'docker-compose.yml'
    content = compose_path.read_text()
    assert 'soul-svc' in content, 'soul-svc service should be declared in compose file'
