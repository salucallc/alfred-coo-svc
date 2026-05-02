import pathlib, subprocess

def test_policy_enforcement_requires_tiresias():
    # Run a command that should fail when tiresias is absent
    # Here we simulate by removing the service line from compose and expecting failure
    compose_path = pathlib.Path(__file__).parents[1] / 'docker-compose.yml'
    original = compose_path.read_text()
    modified = original.replace('tiresias:', '# tiresias:')
    compose_path.write_text(modified)
    result = subprocess.run(['docker-compose', '-f', str(compose_path), 'up', '-d'], capture_output=True, text=True)
    # Expect non-zero because policy enforcement will detect missing tiresias
    assert result.returncode != 0
    # Restore original compose file
    compose_path.write_text(original)
