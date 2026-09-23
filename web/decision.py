"""Natural-language communication with explicit, fail-closed action envelopes."""
import json
import re
import sys


def fallback(reason, content=''):
    detail = re.sub(r'```village-action.*?```', '[action block omitted]', content, flags=re.S)
    detail = re.sub(r'\s+', ' ', detail).strip()[:240]
    message = f'No executable action was accepted ({reason}). I will record this as a communication and choose a different approach next cycle.'
    if detail: message += f' Model output preview: {detail}'
    return {'observation': '', 'fallback_reason': reason, 'tool_call': {'name': 'board_message', 'arguments': {'message': message}}}


def decision(response):
    if response.get('done_reason') == 'length':
        return fallback('output budget exhausted')
    content = response.get('message', {}).get('content', '').strip()
    if not content:
        return fallback('no final answer returned')
    # Never interpret a shell code sample or prose as a command.
    blocks = re.findall(r'^```village-action\s*\n(.*?)\n```\s*$', content, re.M | re.S)
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
    if name not in ('execute_bash', 'board_message', 'idle') or not isinstance(args, dict):
        return fallback('unknown action or malformed arguments', content)
    field = {'execute_bash':'command','board_message':'message'}.get(name)
    if field and (not isinstance(args.get(field), str) or not args[field].strip()):
        return fallback('missing action text', content)
    return {'observation': str(obj.get('observation', ''))[:2000], 'tool_call': {'name': name, 'arguments': args}}


if __name__ == '__main__':
    try:
        with open(sys.argv[1]) as handle: obj = json.load(handle)
        print(json.dumps(decision(obj), ensure_ascii=False))
    except (ValueError, TypeError, AttributeError) as exc:
        print(str(exc), file=sys.stderr); sys.exit(2)
