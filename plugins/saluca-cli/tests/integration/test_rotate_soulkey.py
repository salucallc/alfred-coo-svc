import pathlib

def test_compose_contains_all_services():
    compose_path = pathlib.Path(__file__).parents[1] / 'docker-compose.yml'
    content = compose_path.read_text()
    for svc in ['alfred-coo', 'soul-svc', 'tiresias']:
        assert svc in content, f"{svc} must be present in compose"
