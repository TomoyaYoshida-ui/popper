import json
import io
import unittest
from unittest.mock import patch

from popper.scoop import make_json_client


class JsonRequestPreflightTests(unittest.TestCase):
    def test_json_format_requires_explicit_json_instruction(self):
        captured = []
        class Response:
            def __init__(self):
                frames = [{'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]},
                          {'choices': [], 'usage': {'total_tokens': 5}}]
                self.stream = io.BytesIO((''.join('data: '+json.dumps(f)+'\n\n' for f in frames)+'data: [DONE]\n\n').encode())
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def readline(self, limit): return self.stream.readline(limit)
            def close(self): self.stream.close()
        class Opener:
            def open(self, request, timeout):
                captured.append(json.loads(request.data))
                return Response()
        with patch('popper.scoop.urllib.request.build_opener', return_value=Opener()):
            call = make_json_client('https://provider.example/v1', 'test-model', api_key='example-key')
            self.assertTrue(call('Propose a structured experiment.', {})['ok'])
        self.assertIn('json', captured[0]['messages'][0]['content'].lower())
        self.assertTrue(captured[0]['stream'])
