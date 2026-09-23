"""Read-only resource attribution and deterministic summaries; no model calls."""
import collections
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import time


def command(args, timeout=8):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.stdout if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def hardware():
    """One boot-time inventory, never publish serial numbers or network addresses."""
    try:
        tree = json.loads(command(['lshw', '-json'], 30))
    except ValueError:
        return []
    rows = []
    def walk(node):
        if isinstance(node, list):
            for n in node: walk(n)
            return
        if node.get('class') in ('system', 'processor', 'memory', 'disk', 'display', 'network'):
            row = {k: node[k] for k in ('id', 'class', 'description', 'product', 'vendor', 'size', 'capacity', 'units', 'businfo', 'logicalname') if k in node}
            rows.append(row)
        for child in node.get('children', []): walk(child)
    walk(tree)
    return rows


class Resources:
    def __init__(self):
        self.previous = {}
        self.sample_time = time.monotonic()

    def sample(self, agents):
        owners = {}; ranges = []
        for a in agents:
            try: owners[pwd.getpwnam('village-' + a['name']).pw_uid] = a['id']
            except KeyError: pass
        try:
            for line in Path('/etc/subuid').read_text().splitlines():
                user, first, count = line.split(':')
                for a in agents:
                    if user == 'village-' + a['name']: ranges.append((int(first), int(first)+int(count), a['id']))
        except (OSError, ValueError): pass
        def owner(uid):
            return owners.get(uid) or next((a for lo, hi, a in ranges if lo <= uid < hi), None)
        elapsed = max(.001, time.monotonic() - self.sample_time)
        processes = []; current = {}; containers = {}
        for line in command(['ps', '-eo', 'uid=,pid=,ppid=,rss=,comm=']).splitlines():
            p = line.split(None, 4)
            if len(p) != 5: continue
            uid, pid, ppid, rss = map(int, p[:4]); agent = owner(uid)
            if not agent: continue
            row = {'pid': pid, 'ppid': ppid, 'agent': agent, 'name': p[4], 'rss': rss*1024}
            try:
                stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                start = stat[19]; ticks = int(stat[11])+int(stat[12]); key = (pid, start)
                prev = self.previous.get(key); current[key] = ticks
                row['cpu_percent'] = max(0, (ticks-prev)/os.sysconf('SC_CLK_TCK')/elapsed*100) if prev is not None else None
                cg = Path(f'/proc/{pid}/cgroup').read_text()
                match = re.search(r'libpod-([a-f0-9]{12,64})', cg)
                if match:
                    cid = match[1]; row['container'] = cid
                    containers.setdefault(cid, {'id': cid, 'agent': agent, 'pids': [], 'source': 'cgroup'})['pids'].append(pid)
            except (OSError, ValueError, IndexError): pass
            processes.append(row)
        self.previous = current; self.sample_time = time.monotonic()
        by_pid = {p['pid']: p for p in processes}
        gpu_processes = []
        for line in command(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid,process_name,used_memory', '--format=csv,noheader,nounits']).splitlines():
            p = [x.strip() for x in line.split(',')]
            if len(p) != 4 or not p[1].isdigit(): continue
            row = {'gpu_uuid': p[0], 'pid': int(p[1]), 'name': p[2], 'memory_mib': p[3], 'agent': by_pid.get(int(p[1]), {}).get('agent')}
            gpu_processes.append(row)
        gpu_ids = {}
        for line in command(['nvidia-smi', '--query-gpu=index,uuid', '--format=csv,noheader,nounits']).splitlines():
            p = line.split(',')
            if len(p) == 2: gpu_ids[p[0].strip()] = p[1].strip()
        sockets = []
        for line in command(['ss', '-Htnp']).splitlines():
            p = line.split()
            match = re.search(r'pid=(\d+)', line)
            if len(p)<5 or not match: continue
            pid = int(match[1]); proc = by_pid.get(pid)
            if proc: sockets.append({'pid': pid, 'agent': proc['agent'], 'state': p[0], 'local': p[3], 'peer': p[4], 'container': proc.get('container')})
        # Only reciprocal, observed host-namespace TCP endpoints form links.
        edges = []
        for a in sockets:
            for b in sockets:
                if a['container'] and b['container'] and a['container'] < b['container'] and a['local']==b['peer'] and a['peer']==b['local']:
                    pair = {'from': a['container'], 'to': b['container']}
                    if pair not in edges: edges.append(pair)
        return {'processes': processes[:512], 'containers': list(containers.values()), 'connections': sockets[:128], 'service_links': edges, 'gpu_processes': gpu_processes, 'gpu_ids': gpu_ids, 'scope': 'Host-Prozesse nach Unix-UID/SubUID; Container über libpod-cgroups. TCP nur im Host-Netzwerknamespace. Kein Vollinventar gestoppter Container.'}


def outcome_stats(events):
    """Shell exit status is an execution result, not proof of task completion."""
    result = {}
    for e in events:
        agent = e.get('agent')
        if not agent: continue
        s = result.setdefault(agent, {'success': 0, 'failure': 0, 'unclassified': 0, 'invalid': 0, 'blocked': 0, 'repeated_attempts': 0, 'commands': {}, 'inference_errors': 0})
        kind, detail = e.get('event', ''), str(e.get('detail', ''))
        if kind == 'command_result':
            if re.match(r'result=success;', detail): s['success'] += 1
            elif re.match(r'result=failure\(', detail): s['failure'] += 1
            else: s['unclassified'] += 1
        if kind == 'command_start':
            match = re.search(r'(?:^|; )command=(.*?)(?:; message=|$)', detail, re.S)
            if match:
                sig = ' '.join(match[1].split())
                s['commands'][sig] = s['commands'].get(sig, 0)+1
        if kind == 'invalid_decision': s['invalid'] += 1
        if kind == 'escalation': s['blocked'] += 1
        if kind == 'inference_error': s['inference_errors'] += 1
    for s in result.values():
        s['repeated_attempts'] = sum(max(0,n-1) for n in s['commands'].values())
        s['repeats'] = [{'command': c, 'count': n} for c,n in sorted(s.pop('commands').items(), key=lambda x: -x[1]) if n>1][:5]
        total = s['success'] + s['failure']
        s['success_rate'] = s['success']/total if total else None
        s['completed_actions'] = total
    return result
