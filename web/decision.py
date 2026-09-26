"""Natural-language communication with explicit, fail-closed action envelopes."""
import json
import re
import sys


# Keep this contract in sync with Resident.execute(). The parser previously
# accepted only the founding actions, so valid meeting/team/research/job/artifact
# proposals were discarded before reaching the runtime.
SUPPORTED_ACTIONS = frozenset({
    'execute_bash', 'start_job', 'job_status', 'cancel_job',
    'board_message', 'task_operation', 'team_operation', 'artifact_operation',
    'memory_remember', 'memory_search', 'research_request', 'meeting_operation',
    'idle',
})


def _normalize_action(name, args):
    """Normalize documented legacy aliases without interpreting free prose."""
    normalized = dict(args)
    if name in ('meeting_operation', 'artifact_operation', 'team_operation') and not normalized.get('operation'):
        if normalized.get('action'):
            normalized['operation'] = normalized['action']
        elif name == 'team_operation' and all(normalized.get(k) for k in ('project', 'goal', 'role')):
            normalized['operation'] = 'create'
    return normalized


def _validate_action(name, args):
    if name not in SUPPORTED_ACTIONS or not isinstance(args, dict):
        return 'unknown action or malformed arguments'
    required = {
        'execute_bash': ('command',), 'start_job': ('command',),
        'board_message': ('message',), 'memory_remember': ('content',),
        'memory_search': ('query',), 'research_request': ('source', 'query'),
        'meeting_operation': ('operation',), 'team_operation': ('operation',),
        'artifact_operation': ('operation', 'artifact_id'),
    }.get(name, ())
    if any(not isinstance(args.get(field), str) or not args[field].strip() for field in required):
        return 'missing action argument'
    if name == 'task_operation' and args.get('action') not in ('create', 'claim', 'progress', 'complete', 'yield'):
        return 'unknown task operation'
    if name == 'research_request' and args.get('source') not in ('wikipedia', 'github', 'dockerhub'):
        return 'unsupported research source'
    if name == 'meeting_operation' and args.get('operation') not in ('report', 'close'):
        return 'unknown meeting operation'
    if name == 'team_operation' and args.get('operation') not in ('create', 'join', 'leave', 'create_subtask', 'claim_subtask', 'complete_subtask', 'propose_role', 'vote_role'):
        return 'unknown team operation'
    if name == 'artifact_operation' and args.get('operation') not in ('register', 'claim_success', 'verify', 'adopt', 'inspect'):
        return 'unknown artifact operation'
    return None


def final_content(content):
    # Some imported models emit reasoning tags in message.content even when the
    # server has no separate thinking field. Never execute or broadcast that text.
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.S | re.I)
    if re.search(r'<think>', content, re.I): return ''
    return content.strip()


def fallback(reason, content=''):
    detail = re.sub(r'```village-action.*?```', '[action block omitted]', content, flags=re.S)
    detail = re.sub(r'\s+', ' ', detail).strip()[:240]
    message = f'No executable action was accepted ({reason}). I will record this as a communication and choose a different approach next cycle.'
    if detail: message += f' Model output preview: {detail}'
    return {'observation': '', 'fallback_reason': reason, 'tool_call': {'name': 'board_message', 'arguments': {'message': message}}}


def decision(response):
    if response.get('done_reason') == 'length':
        return fallback('output budget exhausted')
    content = final_content(response.get('message', {}).get('content', ''))
    if not content:
        return fallback('no final answer returned')
    # Never interpret a shell code sample or prose as a command.
    blocks = []
    # Small models often label the exact action envelope json/bash or omit the
    # fence language. Accept an explicit name+arguments object, never shell text.
    for label, body in re.findall(r'^```([^\n]*)\n(.*?)\n```\s*$', content, re.M | re.S):
        body=re.sub(r'^village-action\s*', '', body.strip())
        if label.strip()=='village-action':
            blocks.append(body); continue
        try: candidate=json.loads(body)
        except ValueError: continue
        if isinstance(candidate,dict) and ('name' in candidate or 'tool_call' in candidate):
            blocks.append(body)
        elif isinstance(candidate,dict) and candidate.get('action') in ('create','claim','complete','yield'):
            return fallback('task requires name=task_operation and arguments containing action/task_id',content)
    # An entire terminal named object is also explicit (some models add a prose
    # preface but omit the fence). Do not extract arbitrary inline snippets.
    if not blocks and not content.startswith('{'):
        for match in re.finditer(r'^\{', content, re.M):
            suffix=content[match.start():]
            try: candidate=json.loads(suffix)
            except ValueError: continue
            if isinstance(candidate,dict) and ('name' in candidate or 'tool_call' in candidate):
                blocks.append(suffix)
    if len(blocks)>1:
        return fallback('multiple action blocks', content)
    if blocks:
        try: obj = json.loads(blocks[0])
        except ValueError: return fallback('incomplete village-action block', content)
    elif content.startswith('{'):
        try: obj = json.loads(content)
        except ValueError: return fallback('incomplete legacy action object', content)
    elif '```village-action' in content:
        return fallback('unclosed action block', content)
    else:
        return {'observation': '', 'tool_call': {'name': 'board_message', 'arguments': {'message': content}}}
    if not isinstance(obj, dict): return fallback('action is not an object', content)
    tool = obj.get('tool_call', obj)
    if not isinstance(tool, dict): return fallback('invalid action envelope', content)
    name, args = tool.get('name'), tool.get('arguments', {})
    args = _normalize_action(name, args)
    reason = _validate_action(name, args)
    if reason:
        return fallback(reason, content)
    return {'observation': str(obj.get('observation', ''))[:2000], 'tool_call': {'name': name, 'arguments': args}}


if __name__ == '__main__':
    try:
        with open(sys.argv[1]) as handle: obj = json.load(handle)
        print(json.dumps(decision(obj), ensure_ascii=False))
    except (ValueError, TypeError, AttributeError) as exc:
        print(str(exc), file=sys.stderr); sys.exit(2)
