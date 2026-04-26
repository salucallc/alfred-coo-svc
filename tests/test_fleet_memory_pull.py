# Tests for the memory pull loop implementation.

import builtins
import json
import urllib.request
from unittest import mock

from alfred_coo.fleet_endpoint.memory_pull import pull_memory

def test_pull_memory_success(monkeypatch):
    # Mock the HTTP response
    mock_response = mock.Mock()
    mock_response.read.return_value = json.dumps({"records": [{"id": 1, "content": "test"}]).encode()
    mock_response.__enter__.return_value = mock_response
    mock_urlopen = mock.Mock(return_value=mock_response)
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    result = pull_memory(0)
    assert isinstance(result, list)
    assert result[0]["id"] == 1
