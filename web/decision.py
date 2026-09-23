"""Natural-language communication with explicit, fail-closed action envelopes."""
import json
import re
import sys


def decision(response):
    if response.get('done_reason') == 'length':
        raise ValueError('Output budget exhausted; incomplete response was not executed')
    content = response.get('message', {}).get('content', '').strip()
    if not content:
        raise ValueError('No final answer returned; possibly reasoning consumed the output budget')
    # Never interpret a shell code sample or prose as a command.
    blocks = re.findall(r'^```village-action\s*\n(.*?)\n```\s*$', content, re.M | re.S)
    if len(blocks)>1:
        raise ValueError('Multiple action blocks; no action executed')
    if blocks:
        try: obj = json.loads(blocks[0])
        except ValueError: raise ValueError('Incomplete village-action block; no action executed')
    elif content.startswith('{'):
        try: obj = json.loads(content)
        except ValueError: raise ValueError('Incomplete legacy action object; no action executed')
    elif '```village-action' in content:
        raise ValueError('Unclosed action block; no action executed')
    else:
        return {'observation': '', 'tool_call': {'name': 'board_message', 'arguments': {'message': content}}}
    if not isinstance(obj, dict): raise ValueError('Action must be an object')
    tool = obj.get('tool_call', obj)
    if not isinstance(tool, dict): raise ValueError('Invalid action envelope')
    name, args = tool.get('name'), tool.get('arguments', {})
    if name not in ('execute_bash', 'board_message', 'idle') or not isinstance(args, dict):
        raise ValueError('Unknown action or malformed arguments')
    field = {'execute_bash':'command','board_message':'message'}.get(name)
    if field and (not isinstance(args.get(field), str) or not args[field].strip()):
        raise ValueError('Missing action text')
    return {'observation': str(obj.get('observation', ''))[:2000], 'tool_call': {'name': name, 'arguments': args}}


if __name__ == '__main__':
    try:
        with open(sys.argv[1]) as handle: obj = json.load(handle)
        print(json.dumps(decision(obj), ensure_ascii=False))
    except (ValueError, TypeError, AttributeError) as exc:
        print(str(exc), file=sys.stderr); sys.exit(2)
