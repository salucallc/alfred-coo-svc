import pathlib

def test_compose_contains_tiresias():
    compose_path = pathlib.Path(__file__).parents[1] / 'docker-compose.yml'
    content = compose_path.read_text()
    assert 'tiresias' in content, 'tiresias service should be declared in compose file'
