#!/usr/bin/env python3
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from pathlib import Path

BASE = Path(__file__).resolve().parent
config_path = BASE / 'config.local.json'
if not config_path.exists():
    config_path = BASE / 'config.json'
CONFIG = json.loads(config_path.read_text(encoding='utf-8'))
PROTOCOL_DEBUG = bool(CONFIG.get('protocol_debug'))


def debug_log(*parts):
    try:
        msg = ' '.join(str(p) for p in parts)
        print(f"[relay-debug] {msg}", flush=True)
    except Exception:
        pass


def protocol_log(label, payload=None):
    if not PROTOCOL_DEBUG:
        return
    try:
        if payload is None:
            print(f"[relay-protocol] {label}", flush=True)
        else:
            print(f"[relay-protocol] {label} {json.dumps(payload, ensure_ascii=False)}", flush=True)
    except Exception:
        pass


def rid(prefix='msg'):
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def sse_line(event=None, data=None):
    out = []
    if event:
        out.append(f"event: {event}\n")
    if data is not None:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        for line in payload.splitlines() or [payload]:
            out.append(f"data: {line}\n")
    out.append("\n")
    return ''.join(out).encode('utf-8')


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Connection', 'close')
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


def read_json(handler):
    length = int(handler.headers.get('Content-Length', '0') or '0')
    raw = handler.rfile.read(length) if length > 0 else b'{}'
    return json.loads(raw.decode('utf-8'))


def extract_text_from_any(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                ptype = part.get('type')
                if ptype == 'text':
                    text_parts.append(part.get('text', ''))
                elif ptype == 'tool_result':
                    inner = part.get('content', '')
                    text_parts.append(extract_text_from_any(inner))
                elif 'text' in part:
                    text_parts.append(str(part.get('text', '')))
        return '\n'.join([x for x in text_parts if x])
    if content is None:
        return ''
    return json.dumps(content, ensure_ascii=False)


def convert_tools(tools):
    mapped_tools = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get('name')
        if not name:
            continue
        mapped_tools.append({
            'type': 'function',
            'function': {
                'name': name,
                'description': tool.get('description', ''),
                'parameters': tool.get('input_schema', {'type': 'object', 'properties': {}})
            }
        })
    return mapped_tools


def anthropic_part_to_openai_part(part):
    if isinstance(part, str):
        return {'type': 'text', 'text': part}, None
    if not isinstance(part, dict):
        return None, None

    ptype = part.get('type')
    if ptype == 'text':
        text = part.get('text', '')
        return {'type': 'text', 'text': text}, {'kind': 'text', 'chars': len(text or '')}

    if ptype == 'image':
        source = part.get('source') or {}
        stype = source.get('type')
        media_type = source.get('media_type') or 'image/png'
        if stype == 'base64' and source.get('data'):
            return ({
                'type': 'image_url',
                'image_url': {
                    'url': f"data:{media_type};base64,{source.get('data')}"
                }
            }, {
                'kind': 'image',
                'source_type': 'base64',
                'media_type': media_type
            })
        if stype == 'url' and source.get('url'):
            return ({
                'type': 'image_url',
                'image_url': {
                    'url': source.get('url')
                }
            }, {
                'kind': 'image',
                'source_type': 'url',
                'media_type': media_type
            })

    if ptype == 'image_url':
        image_url = part.get('image_url') or {}
        url = image_url.get('url') if isinstance(image_url, dict) else None
        if url:
            detail = image_url.get('detail') if isinstance(image_url, dict) else None
            return ({
                'type': 'image_url',
                'image_url': {'url': url, **({'detail': detail} if detail else {})}
            }, {
                'kind': 'image',
                'source_type': 'image_url',
                'media_type': 'unknown'
            })

    return None, {'kind': 'ignored', 'type': ptype or 'unknown'}


def anthropic_messages_to_openai(messages):
    oa_messages = []
    pending_assistant_tool_calls = {}

    for msg in messages or []:
        role = msg.get('role', 'user')
        content = msg.get('content', '')

        if role == 'assistant' and isinstance(content, list):
            text_parts = []
            tool_calls = []
            for part in content:
                if not isinstance(part, dict):
                    if isinstance(part, str):
                        text_parts.append(part)
                    continue
                ptype = part.get('type')
                if ptype == 'text':
                    if part.get('text'):
                        text_parts.append(part.get('text', ''))
                elif ptype == 'tool_use':
                    tool_id = part.get('id') or rid('toolu')
                    name = part.get('name') or 'unknown_tool'
                    tool_input = part.get('input', {})
                    tool_calls.append({
                        'id': tool_id,
                        'type': 'function',
                        'function': {
                            'name': name,
                            'arguments': json.dumps(tool_input, ensure_ascii=False)
                        }
                    })
                    pending_assistant_tool_calls[tool_id] = True
            assistant_msg = {'role': 'assistant'}
            if text_parts:
                assistant_msg['content'] = '\n'.join(text_parts)
            else:
                assistant_msg['content'] = ''
            if tool_calls:
                assistant_msg['tool_calls'] = tool_calls
            oa_messages.append(assistant_msg)
            continue

        if role == 'user' and isinstance(content, list):
            user_parts = []
            image_count = 0
            image_types = []
            ignored_types = []
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'tool_result':
                    tool_use_id = part.get('tool_use_id') or part.get('id') or rid('toolu')
                    result_text = extract_text_from_any(part.get('content', ''))
                    oa_messages.append({
                        'role': 'tool',
                        'tool_call_id': tool_use_id,
                        'content': result_text
                    })
                    continue
                converted, meta = anthropic_part_to_openai_part(part)
                if converted:
                    user_parts.append(converted)
                if meta:
                    if meta.get('kind') == 'image':
                        image_count += 1
                        image_types.append(f"{meta.get('source_type')}:{meta.get('media_type')}")
                    elif meta.get('kind') == 'ignored':
                        ignored_types.append(meta.get('type'))
            if user_parts:
                if len(user_parts) == 1 and user_parts[0].get('type') == 'text':
                    oa_messages.append({'role': 'user', 'content': user_parts[0].get('text', '')})
                else:
                    oa_messages.append({'role': 'user', 'content': user_parts})
            if image_count or ignored_types:
                debug_log('multimodal user message', json.dumps({
                    'image_count': image_count,
                    'image_types': image_types,
                    'ignored_types': ignored_types,
                    'part_count': len(content)
                }, ensure_ascii=False))
            continue

        oa_messages.append({'role': role, 'content': extract_text_from_any(content)})

    return oa_messages


def summarize_anthropic_messages(messages):
    out = []
    for msg in messages or []:
        role = msg.get('role', 'user') if isinstance(msg, dict) else 'unknown'
        content = msg.get('content', '') if isinstance(msg, dict) else ''
        summary = {'role': role}
        if isinstance(content, str):
            summary['content_type'] = 'string'
            summary['chars'] = len(content)
        elif isinstance(content, list):
            summary['content_type'] = 'list'
            part_types = []
            for part in content:
                if isinstance(part, dict):
                    ptype = part.get('type', 'unknown')
                    part_types.append(ptype)
                elif isinstance(part, str):
                    part_types.append('text')
                else:
                    part_types.append(type(part).__name__)
            summary['part_types'] = part_types
            summary['part_count'] = len(content)
        else:
            summary['content_type'] = type(content).__name__
        out.append(summary)
    return out


def summarize_openai_messages(messages):
    out = []
    for msg in messages or []:
        role = msg.get('role', 'unknown') if isinstance(msg, dict) else 'unknown'
        content = msg.get('content', '') if isinstance(msg, dict) else ''
        summary = {'role': role}
        if isinstance(content, str):
            summary['content_type'] = 'string'
            summary['chars'] = len(content)
        elif isinstance(content, list):
            summary['content_type'] = 'list'
            part_types = []
            for part in content:
                if isinstance(part, dict):
                    part_types.append(part.get('type', 'unknown'))
                else:
                    part_types.append(type(part).__name__)
            summary['part_types'] = part_types
            summary['part_count'] = len(content)
        else:
            summary['content_type'] = type(content).__name__
        if isinstance(msg, dict) and msg.get('tool_calls'):
            summary['tool_calls'] = [
                (tc.get('function') or {}).get('name', 'unknown')
                for tc in (msg.get('tool_calls') or [])
                if isinstance(tc, dict)
            ]
        out.append(summary)
    return out


def get_provider_payload(req):
    model_map = CONFIG['model_map']
    requested_model = req.get('model') or 'default'
    mapped_model = model_map.get(requested_model, model_map.get('default', requested_model))

    system = req.get('system')
    messages = req.get('messages') or []
    tools = req.get('tools') or []

    oa_messages = []
    if system:
        system_text = extract_text_from_any(system)
        if system_text:
            oa_messages.append({'role': 'system', 'content': system_text})

    protocol_log('incoming anthropic request', {
        'model': requested_model,
        'stream': bool(req.get('stream')),
        'message_count': len(messages),
        'messages': summarize_anthropic_messages(messages),
        'tool_names': [tool.get('name') for tool in tools if isinstance(tool, dict) and tool.get('name')]
    })

    oa_messages.extend(anthropic_messages_to_openai(messages))

    payload = {
        'model': mapped_model,
        'messages': oa_messages,
        'stream': bool(req.get('stream')),
    }
    if 'max_tokens' in req and req['max_tokens'] is not None:
        payload['max_tokens'] = req['max_tokens']
    if 'temperature' in req and req['temperature'] is not None:
        payload['temperature'] = req['temperature']
    mapped_tools = convert_tools(tools)
    if mapped_tools:
        payload['tools'] = mapped_tools

    protocol_log('outgoing openai payload', {
        'mapped_model': mapped_model,
        'stream': payload.get('stream'),
        'message_count': len(oa_messages),
        'messages': summarize_openai_messages(oa_messages),
        'tool_names': [((tool.get('function') or {}).get('name')) for tool in mapped_tools]
    })
    return payload, requested_model, mapped_model


def provider_request(payload):
    prov = CONFIG['provider']
    url = prov['base_url'].rstrip('/') + prov['chat_completions_path']
    body = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {prov['api_key']}"
    }
    req = Request(url, data=body, headers=headers, method='POST')
    return urlopen(req, timeout=prov.get('timeout_seconds', 300))


def convert_tool_calls(tool_calls):
    out = []
    for call in tool_calls or []:
        func = call.get('function') or {}
        args = func.get('arguments') or '{}'
        try:
            parsed = json.loads(args) if isinstance(args, str) else args
        except Exception:
            parsed = {'raw': args}
        out.append({
            'type': 'tool_use',
            'id': call.get('id') or rid('toolu'),
            'name': func.get('name') or 'unknown_tool',
            'input': parsed if isinstance(parsed, dict) else {'value': parsed}
        })
    return out


def anthropic_nonstream_from_openai(resp_json, requested_model):
    text = ''
    stop_reason = 'end_turn'
    usage = resp_json.get('usage') or {}
    tool_content = []
    choices = resp_json.get('choices') or []
    if choices:
        msg = choices[0].get('message') or {}
        text = msg.get('content') or ''
        tool_calls = msg.get('tool_calls') or []
        if tool_calls:
            stop_reason = 'tool_use'
            tool_content = convert_tool_calls(tool_calls)
        finish_reason = choices[0].get('finish_reason')
        if finish_reason == 'tool_calls':
            stop_reason = 'tool_use'
    content = []
    if text:
        content.append({'type': 'text', 'text': text})
    content.extend(tool_content)
    if not content:
        content = [{'type': 'text', 'text': ''}]
    return {
        'id': rid('msg'),
        'type': 'message',
        'role': 'assistant',
        'model': requested_model,
        'content': content,
        'stop_reason': stop_reason,
        'stop_sequence': None,
        'usage': {
            'input_tokens': usage.get('prompt_tokens', 0),
            'output_tokens': usage.get('completion_tokens', 0)
        }
    }


class Handler(BaseHTTPRequestHandler):
    server_version = 'ClaudeCodeRelay/0.3'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        return

    def end_sse(self):
        try:
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True

    def do_GET(self):
        p = urlparse(self.path).path
        if p == '/health':
            return json_response(self, 200, {
                'ok': True,
                'service': 'claude-code-relay',
                'provider': 'openai-compatible'
            })
        if p == '/v1/models':
            models = []
            seen = set()
            for k in CONFIG['model_map'].keys():
                if k == 'default' or k in seen:
                    continue
                seen.add(k)
                models.append({'id': k, 'object': 'model', 'owned_by': 'relay'})
            return json_response(self, 200, {'data': models})
        return json_response(self, 404, {'error': 'not_found'})

    def do_POST(self):
        p = urlparse(self.path).path
        if p != '/v1/messages':
            return json_response(self, 404, {'error': 'not_found'})
        try:
            req = read_json(self)
            payload, requested_model, _mapped_model = get_provider_payload(req)
            stream = bool(req.get('stream'))

            if not stream:
                upstream = provider_request(payload)
                body = upstream.read().decode('utf-8', 'ignore')
                resp_json = json.loads(body)
                out = anthropic_nonstream_from_openai(resp_json, requested_model)
                return json_response(self, 200, out)

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()

            payload['stream'] = True
            upstream = provider_request(payload)

            msg_id = rid('msg')
            self.wfile.write(sse_line('message_start', {
                'type': 'message_start',
                'message': {
                    'id': msg_id,
                    'type': 'message',
                    'role': 'assistant',
                    'model': requested_model,
                    'content': [],
                    'stop_reason': None,
                    'stop_sequence': None,
                    'usage': {'input_tokens': 0, 'output_tokens': 0}
                }
            }))
            self.wfile.write(sse_line('ping', {'type': 'ping'}))
            self.wfile.flush()

            usage = None
            text_block_started = False
            text_block_index = 0
            encountered_tool_call = False
            tool_call_accumulators = {}
            done = False

            protocol_log('stream start', {'requested_model': requested_model})

            while not done:
                line = upstream.readline()
                if not line:
                    break
                text = line.decode('utf-8', 'ignore').strip()
                if not text or not text.startswith('data:'):
                    continue
                data = text[5:].strip()
                if data == '[DONE]':
                    protocol_log('upstream chunk done')
                    done = True
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue

                if chunk.get('error'):
                    protocol_log('upstream chunk error', chunk.get('error'))
                    raise Exception(chunk.get('error'))

                u = chunk.get('usage') or {}
                if u:
                    usage = {
                        'input_tokens': u.get('prompt_tokens', 0),
                        'output_tokens': u.get('completion_tokens', 0)
                    }
                    protocol_log('upstream usage', usage)

                choices = chunk.get('choices') or []
                if not choices:
                    continue
                delta = choices[0].get('delta') or {}

                if delta.get('tool_calls'):
                    for tool_call in delta.get('tool_calls') or []:
                        encountered_tool_call = True
                        idx = int(tool_call.get('index', 0)) + 1
                        state = tool_call_accumulators.setdefault(idx, {
                            'started': False,
                            'id': None,
                            'name': None,
                            'args_raw': ''
                        })
                        if tool_call.get('id'):
                            state['id'] = tool_call.get('id')
                        func = tool_call.get('function') or {}
                        if func.get('name'):
                            state['name'] = func.get('name')
                        if not state['started']:
                            protocol_log('emit content_block_start tool_use', {
                                'index': idx,
                                'tool_id': state['id'] or 'pending',
                                'tool_name': state['name'] or 'unknown_tool'
                            })
                            self.wfile.write(sse_line('content_block_start', {
                                'type': 'content_block_start',
                                'index': idx,
                                'content_block': {
                                    'type': 'tool_use',
                                    'id': state['id'] or rid('toolu'),
                                    'name': state['name'] or 'unknown_tool',
                                    'input': {}
                                }
                            }))
                            state['started'] = True
                        new_args = func.get('arguments') or ''
                        if new_args:
                            prev = state['args_raw']
                            delta_text = new_args[len(prev):] if new_args.startswith(prev) else new_args
                            if delta_text:
                                protocol_log('emit input_json_delta', {
                                    'index': idx,
                                    'tool_name': state.get('name') or 'unknown_tool',
                                    'delta_chars': len(delta_text)
                                })
                                self.wfile.write(sse_line('content_block_delta', {
                                    'type': 'content_block_delta',
                                    'index': idx,
                                    'delta': {
                                        'type': 'input_json_delta',
                                        'partial_json': delta_text
                                    }
                                }))
                            state['args_raw'] = new_args
                        self.wfile.flush()
                    continue

                piece = delta.get('content')
                if piece:
                    if not text_block_started:
                        protocol_log('emit content_block_start text', {'index': text_block_index})
                        self.wfile.write(sse_line('content_block_start', {
                            'type': 'content_block_start',
                            'index': text_block_index,
                            'content_block': {'type': 'text', 'text': ''}
                        }))
                        text_block_started = True
                    protocol_log('emit text_delta', {
                        'index': text_block_index,
                        'chars': len(piece)
                    })
                    self.wfile.write(sse_line('content_block_delta', {
                        'type': 'content_block_delta',
                        'index': text_block_index,
                        'delta': {'type': 'text_delta', 'text': piece}
                    }))
                    self.wfile.flush()
                    continue

            if encountered_tool_call:
                for idx in sorted(tool_call_accumulators.keys()):
                    state = tool_call_accumulators[idx]
                    tool_name = str(state.get('name') or 'unknown_tool').lower()
                    args_raw = state.get('args_raw') or ''
                    if tool_name == 'web_search' and args_raw.strip() == '{}':
                        continue
                    protocol_log('emit content_block_stop tool_use', {'index': idx, 'tool_name': state.get('name') or 'unknown_tool'})
                    self.wfile.write(sse_line('content_block_stop', {
                        'type': 'content_block_stop',
                        'index': idx
                    }))
            elif text_block_started:
                protocol_log('emit content_block_stop text', {'index': text_block_index})
                self.wfile.write(sse_line('content_block_stop', {
                    'type': 'content_block_stop',
                    'index': text_block_index
                }))

            final_stop_reason = 'tool_use' if encountered_tool_call else 'end_turn'
            protocol_log('emit message_delta', {
                'stop_reason': final_stop_reason,
                'usage': usage or {'input_tokens': 0, 'output_tokens': 0}
            })
            self.wfile.write(sse_line('message_delta', {
                'type': 'message_delta',
                'delta': {
                    'stop_reason': final_stop_reason,
                    'stop_sequence': None
                },
                'usage': usage or {'input_tokens': 0, 'output_tokens': 0}
            }))
            protocol_log('emit message_stop')
            self.wfile.write(sse_line('message_stop', {
                'type': 'message_stop'
            }))
            self.end_sse()
        except HTTPError as e:
            body = e.read().decode('utf-8', 'ignore') if hasattr(e, 'read') else str(e)
            return json_response(self, e.code if getattr(e, 'code', None) else 502, {
                'type': 'error',
                'error': {
                    'type': 'upstream_error',
                    'message': body or str(e)
                }
            })
        except URLError as e:
            return json_response(self, 502, {
                'type': 'error',
                'error': {
                    'type': 'connection_error',
                    'message': str(e)
                }
            })
        except Exception as e:
            return json_response(self, 500, {
                'type': 'error',
                'error': {
                    'type': 'internal_error',
                    'message': str(e)
                }
            })


if __name__ == '__main__':
    host = CONFIG['server']['host']
    port = int(CONFIG['server']['port'])
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f'Claude Code Relay listening on http://{host}:{port}')
    httpd.serve_forever()
