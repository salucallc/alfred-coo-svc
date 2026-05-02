import pathlib

def test_compose_contains_alfred_coo():
    compose_path = pathlib.Path(__file__).parents[1] / 'docker-compose.yml'
    content = compose_path.read_text()
    assert 'alfred-coo' in content, 'alfred-coo service should be declared in compose file'
