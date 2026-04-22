"""Structured-output parser tests."""

from alfred_coo.structured import parse_envelope, OUTPUT_CONTRACT


BARE = '{"summary": "did a thing", "artifacts": [], "follow_up_tasks": []}'


def test_bare_json_parses():
    env = parse_envelope(BARE)
    assert env is not None
    assert env.summary == "did a thing"
    assert env.artifacts == []
    assert env.follow_up_tasks == []


def test_fenced_json_parses():
    raw = "Here is the result:\n```json\n" + BARE + "\n```\n"
    env = parse_envelope(raw)
    assert env is not None
    assert env.summary == "did a thing"


def test_plain_fence_parses():
    raw = "```\n" + BARE + "\n```"
    env = parse_envelope(raw)
    assert env is not None


def test_prose_prefix_parses():
    raw = "Sure, here you go.\n\n" + BARE + "\n\nLet me know if you want more."
    env = parse_envelope(raw)
    assert env is not None
    assert env.summary == "did a thing"


def test_artifact_extraction():
    raw = (
        '{"summary": "wrote 2 files", '
        '"artifacts": ['
        '{"path": "a.md", "content": "alpha"},'
        '{"path": "sub/b.py", "content": "print(1)"}'
        '], "follow_up_tasks": []}'
    )
    env = parse_envelope(raw)
    assert env is not None
    assert len(env.artifacts) == 2
    assert env.artifacts[0]["path"] == "a.md"
    assert env.artifacts[1]["content"] == "print(1)"


def test_malformed_json_returns_none():
    assert parse_envelope("{not valid json}") is None
    assert parse_envelope("") is None
    assert parse_envelope("not even close") is None


def test_missing_summary_returns_none():
    assert parse_envelope('{"artifacts": []}') is None


def test_artifacts_wrong_type_returns_none():
    assert parse_envelope('{"summary": "x", "artifacts": "not-a-list"}') is None
    assert parse_envelope('{"summary": "x", "artifacts": [{"path": 5, "content": "y"}]}') is None


def test_nested_braces_in_content():
    raw = (
        '{"summary": "json in json",'
        ' "artifacts": [{"path": "config.json", "content": "{\\"nested\\": true}"}],'
        ' "follow_up_tasks": []}'
    )
    env = parse_envelope(raw)
    assert env is not None
    assert env.artifacts[0]["content"] == '{"nested": true}'


def test_output_contract_is_nonempty():
    assert OUTPUT_CONTRACT
    assert "summary" in OUTPUT_CONTRACT
    assert "artifacts" in OUTPUT_CONTRACT


def test_follow_up_tasks_filters_non_strings():
    raw = (
        '{"summary": "x", "artifacts": [],'
        ' "follow_up_tasks": ["good", 42, null, "also good"]}'
    )
    env = parse_envelope(raw)
    assert env is not None
    assert env.follow_up_tasks == ["good", "also good"]
