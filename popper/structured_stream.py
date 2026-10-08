"""Collect a bounded JSON answer from SSE; never downgrade to non-streaming."""
import json
from .request_lifecycle import RequestFailure, SSEReader


def collect_json_stream(lifecycle, opener, request, diagnostic):
    content, finish, response_id = [], None, None
    wire, payload = 0, 0
    with lifecycle.open(opener, request) as response:
        response = SSEReader(response)
        while True:
            line = response.readline(65537)
            if not line:
                raise RequestFailure('stream_interrupted', usage=lifecycle.usage)
            wire += len(line)
            if len(line) > 65536 or wire > 8_000_000:
                raise RequestFailure('response_size_limit', usage=lifecycle.usage)
            text = line.decode('utf-8').strip()
            if not text or text.startswith(':'):
                continue
            if not text.startswith('data:'):
                raise RequestFailure('invalid_sse', usage=lifecycle.usage)
            data = text[5:].strip()
            if data == '[DONE]':
                lifecycle.stream_completed = True
                lifecycle.transport_phase = 'completed'
                break
            envelope = json.loads(data)
            if isinstance(envelope.get('usage'), dict):
                lifecycle.usage = {k: v for k, v in envelope['usage'].items()
                                   if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                                   and type(v) is int and v >= 0}
                diagnostic['usage'] = lifecycle.usage
            if isinstance(envelope.get('id'), str):
                response_id = envelope['id'][:256]
            choices = envelope.get('choices') or []
            if not choices:
                continue
            finish = choices[0].get('finish_reason') or finish
            piece = (choices[0].get('delta') or {}).get('content') or ''
            lifecycle.observe_output((choices[0].get('delta') or {}).get('reasoning_content'))
            if not isinstance(piece, str):
                raise RequestFailure('invalid_response', usage=lifecycle.usage)
            payload += len(piece.encode('utf-8'))
            if payload > 1_000_000:
                raise RequestFailure('response_size_limit', usage=lifecycle.usage)
            content.append(piece)
            if piece:
                lifecycle.observe_output(piece)
                lifecycle.progress('receiving')
    diagnostic['finish_reason'] = finish
    diagnostic['response_id'] = response_id
    if finish == 'length':
        raise RequestFailure('output_truncated', usage=lifecycle.usage)
    if finish != 'stop':
        raise RequestFailure('invalid_finish_reason', usage=lifecycle.usage)
    return {'choices': [{'finish_reason': finish, 'message': {'content': ''.join(content)}}],
            'usage': lifecycle.usage}
