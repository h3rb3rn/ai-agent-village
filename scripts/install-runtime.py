#!/usr/bin/env python3
"""Targeted, backed-up runtime rollout. Never runs bootstrap or edits model settings.

Usage: sudo python3 scripts/install-runtime.py --env /opt/ai-agent-village/.env
Use --provision-only during bootstrap (no restart, no source installation).
Backups contain credentials: keep them root-only. Manifest lists rollback targets.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import grp
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

# Import the release package from the checkout being installed, whatever the cwd is.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from village.release import RELEASE_FILES, compute_sha256, get_git_revision


# Resident bash commands run as ordinary Unix users inside these cgroups. Without limits one
# runaway command (fork bomb, memory hog, endless file) can starve Neo4j, Chroma, the memory
# gateway and every other resident on the 4-core / 16 GiB control host.
AGENT_LIMITS = (('MemoryHigh', '1536M'), ('MemoryMax', '3G'), ('TasksMax', '512'),
                ('CPUQuota', '150%'), ('LimitFSIZE', '4G'))
SLICE_LIMITS = (('MemoryMax', '10G'), ('CPUQuota', '300%'))
SLICE_NAME = 'ai-village-agents.slice'


def agent_limits_dropin():
    return '[Service]\nSlice=' + SLICE_NAME + '\n' + ''.join(f'{k}={v}\n' for k, v in AGENT_LIMITS)


def agents_slice_unit():
    return '[Unit]\nDescription=AI Village resident agents (shared resource ceiling)\n[Slice]\n' + ''.join(f'{k}={v}\n' for k, v in SLICE_LIMITS)


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line: continue
        key, value = line.removeprefix('export ').split('=', 1)
        parts = shlex.split(value, comments=True)
        values[key.strip()] = ' '.join(parts)
    return values


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_release_sources(source):
    """Fail closed when the checkout is missing a manifest-declared source."""
    missing = []
    hashes = {}
    for relative, _, _ in RELEASE_FILES:
        path = source / relative
        if not path.is_file():
            missing.append(relative)
            continue
        hashes[relative] = compute_sha256(path)
    if missing:
        raise ValueError('release source manifest incomplete: ' + ', '.join(missing))
    return hashes


def build_targets(source):
    """The full set of files this installer deploys, as {destination: source}.

    Extracted out of main() (P65) so it is directly unit-testable without
    mocking systemd/root/filesystem effects - the drift this caught (webui.py
    and observatory.* were never in this list at all, see docs/evidence/P65.md)
    would otherwise only ever surface live, the way it did for P64.
    """
    targets = {
        Path('/usr/local/lib/ai-village/runtime.py'): source / 'web/runtime.py',
        Path('/usr/local/lib/ai-village/event_history.py'): source / 'web/event_history.py',
        Path('/usr/local/lib/ai-village/decision.py'): source / 'web/decision.py',
        Path('/usr/local/lib/ai-village/memory-gateway.py'): source / 'memory/gateway.py',
        Path('/usr/local/lib/ai-village/append-event.py'): source / 'scripts/append-event.py',
        Path('/usr/local/lib/ai-village/mcp-tools-server.py'): source / 'scripts/mcp-tools-server.py',
        Path('/usr/local/share/ai-village/system-prompt.txt'): source / 'prompts/resident-system.txt',
        Path('/usr/local/share/ai-village/system-prompt-core.txt'): source / 'prompts/resident-core.txt',
        Path('/usr/local/share/ai-village/runtime-policy.json'): source / 'config/runtime-policy.json',
        Path('/usr/local/share/ai-village/task-templates.json'): source / 'config/task-templates.json',
        # P64/P65: webui.py and its static assets were never in this list -
        # every resident-agent-facing file had a redeploy path, but the
        # dashboard itself did not. Found live: the P64 Gazette dashboard
        # page was pushed, "installed" successfully, and the host kept
        # serving the old webui.py/observatory.* indefinitely (404s on the
        # new routes) until a manual install/restart. See docs/evidence/P65.md.
        Path('/usr/local/lib/ai-village/webui.py'): source / 'web/webui.py',
        Path('/usr/local/share/ai-village/web/observatory.html'): source / 'web/observatory.html',
        Path('/usr/local/share/ai-village/web/observatory.css'): source / 'web/observatory.css',
        Path('/usr/local/share/ai-village/web/observatory.js'): source / 'web/observatory.js',
    }
    # Runtime imports are installed as a self-contained package. Keeping these
    # modules in the targeted release prevents a live host from running a newer
    # runtime with an older, incomplete import tree.
    for module in ('__init__.py', 'actions.py', 'collaboration.py', 'policy.py', 'prompting.py', 'tools.py', 'auditor.py', 'auditor_llm.py', 'artifacts.py', 'authority.py', 'config.py',
                   'containers.py', 'control.py', 'coordinator.py', 'inference.py',
                   'events.py', 'event_retention.py', 'firewatch.py', 'jobs.py', 'lifecycle.py', 'meetings.py', 'gazette.py', 'gazette_pdf.py', 'research.py', 'research_protocol.py', 'interventions.py', 'research_tasks.py', 'rollout.py', 'lineage.py', 'recovery.py', 'security.py', 'teams.py'):
        targets[Path('/usr/local/lib/ai-village/village') / module] = source / 'village' / module
    return targets


def should_stop_active_services(active, provision_only, no_start):
    """Whether currently-active resident units should be stopped before
    installing new files.

    P50: --no-start's own help text promises "without starting or
    restarting resident services" - i.e. leave already-running residents
    alone. It previously stopped every active unit unconditionally and
    relied on --no-start only to skip the RESTART afterward, so any
    resident that happened to be running (not just the one an operator
    explicitly restarted post-install) was silently left dead with no
    error - observed live: 8 of 9 residents stopped and never came back
    after a --no-start install into an actively running village (see
    docs/evidence/P50.md). Stopping is now itself part of what --no-start
    skips.
    """
    return bool(active) and not provision_only and not no_start


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',type=Path,default=Path('/opt/ai-agent-village/.env'))
    parser.add_argument('--provision-only',action='store_true')
    parser.add_argument('--dry-run',action='store_true',help='Validate targets, config and compilation without host changes')
    parser.add_argument('--no-start',action='store_true',help='Install files without stopping, starting or restarting currently running resident services; they keep running the old code until separately/manually restarted')
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1],help='Source directory containing web/ and prompts/')
    args=parser.parse_args()
    if not args.dry_run and os.geteuid()!=0: parser.error('must run as root')
    source=args.source.resolve()
    try:
        release_hashes = validate_release_sources(source)
    except ValueError as exc:
        parser.error(str(exc))
    config=read_env(args.env); env_hash=digest(args.env)
    agent_files=sorted(Path('/etc/ai-village/agents').glob('*.env'))
    agents=[read_env(p) for p in agent_files]
    if not agents: parser.error('no installed agents; bootstrap first')
    # Fail rather than silently use a different model configuration from the host.
    fields={'NAME':'AGENT_NAME','ROLE':'AGENT_ROLE','URL':'OLLAMA_URL','MODEL':'OLLAMA_MODEL',
            'NUM_CTX':'OLLAMA_NUM_CTX','NUM_PREDICT':'OLLAMA_NUM_PREDICT',
            'THINK_LEVEL':'OLLAMA_THINK_LEVEL','KEEP_ALIVE':'OLLAMA_KEEP_ALIVE',
            'API_TYPE':'API_TYPE','API_TOKEN':'API_TOKEN'}
    for agent in agents:
        index=int(agent['AGENT_ID'].split('-',1)[0])
        for field,live_key in fields.items():
            key=f'OLLAMA_AGENT_{index}_{field}'
            if key in config and config[key].rstrip('/')!=agent.get(live_key,'').rstrip('/'):
                parser.error(f'{key} differs from installed environment; reconcile deliberately before rollout (no change made)')
    root=Path(agents[0]['VILLAGE_ROOT'])
    if any(Path(a['VILLAGE_ROOT'])!=root for a in agents): parser.error('multiple roots not supported')
    targets=build_targets(source)
    if not args.provision_only:
        for target,src in targets.items():
            if src.suffix=='.py': compile(src.read_text(),str(src),'exec')
            elif not src.is_file(): parser.error(f'missing {src}')
    if args.dry_run:
        print(f"Dry-run: successfully validated {len(targets)} targets and {len(agents)} agent environments.")
        return
    units=[f'ai-village-agent-{a["AGENT_ID"]}.service' for a in agents]
    active=[u for u in units if subprocess.run(['systemctl','is-active','--quiet',u]).returncode==0]
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=Path('/var/backups/ai-village')/('runtime-'+stamp)
    backup.mkdir(parents=True,mode=0o700); backup.chmod(0o700)
    saved={}
    def preserve(path):
        if str(path) in saved: return
        saved[str(path)]=dict(existed=path.exists())
        if path.exists():
            dest=backup/str(path).lstrip('/')
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,dest)
            saved[str(path)].update(uid=path.stat().st_uid,gid=path.stat().st_gid,sha256=digest(path))
    def put(path,text,mode=0o644,uid=0,gid=0):
        preserve(path); path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
        temporary=Path(name)
        try:
            with os.fdopen(fd,'w') as handle: handle.write(text)
            temporary.chmod(mode); os.chown(temporary,uid,gid)
            temporary.replace(path)
        finally:
            if temporary.exists(): temporary.unlink()
    try:
        if should_stop_active_services(active, args.provision_only, args.no_start):
            subprocess.run(['systemctl','stop',*active],check=True)
        if not args.provision_only:
            for target,src in targets.items(): put(target,src.read_text())
            put(Path('/usr/local/lib/ai-village/agent-runner'), '#!/bin/sh\nexec /usr/bin/python3 /usr/local/lib/ai-village/runtime.py\n',0o755)
            # P65: unlike every other .py target above (invoked as `python3
            # <path>`), systemd's ai-village-webui.service execs webui.py
            # directly (ExecStart=/usr/local/lib/ai-village/webui.py) - it
            # needs its own executable bit, which the shared put() default
            # (0o644) does not set.
            Path('/usr/local/lib/ai-village/webui.py').chmod(0o755)
        token_path=Path('/etc/ai-village/memory-agent-tokens.json')
        tokens=json.loads(token_path.read_text()) if token_path.exists() else {}
        credentials=Path('/etc/ai-village/credentials'); credentials.mkdir(exist_ok=True,mode=0o700)
        credentials.chmod(0o700)
        for agent in agents:
            ident=agent['AGENT_ID']; token=tokens.get(ident) or secrets.token_urlsafe(36); tokens[ident]=token
            credential=credentials/(ident+'.env')
            gateway=config.get('MEMORY_GATEWAY_URL','http://127.0.0.1:'+config.get('MEMORY_PORT','8090'))
            put(credential,f'MEMORY_AGENT_TOKEN={token}\nMEMORY_GATEWAY_URL={gateway}\nVILLAGE_MIN_FREE_MEMORY_MIB={config.get("VILLAGE_MIN_FREE_MEMORY_MIB","4096")}\n',0o600)
            dropin=Path('/etc/systemd/system')/f'ai-village-agent-{ident}.service.d/30-memory-runtime.conf'
            put(dropin,f'[Service]\nEnvironmentFile={credential}\n')
            pause_dropin=Path('/etc/systemd/system')/f'ai-village-agent-{ident}.service.d/10-pause.conf'
            put(pause_dropin,'[Unit]\nConditionPathExists=!/etc/ai-village/paused\n')
            put(Path('/etc/systemd/system')/f'ai-village-agent-{ident}.service.d/40-resource-limits.conf',agent_limits_dropin())
        put(Path('/etc/systemd/system')/SLICE_NAME,agents_slice_unit())
        put(token_path,json.dumps(tokens),0o600,pwd.getpwnam('village-web').pw_uid,0)
        peers=[dict(id=a['AGENT_ID'],name=a['AGENT_NAME'],role=a['AGENT_ROLE'],model=a['OLLAMA_MODEL']) for a in agents]
        put(Path('/etc/ai-village/runtime-peers.json'),json.dumps(peers))
        gid=grp.getgrnam('ai-village').gr_gid
        for path,text in [(root/'board/work-items.json','[]'),(root/'board/work-items.lock','')]:
            if not path.exists(): put(path,text,0o660,0,gid)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        if not args.provision_only:
            subprocess.run(['systemctl','restart','ai-village-memory-gateway.service'],check=True)
            subprocess.run(['systemctl','is-active','--quiet','ai-village-memory-gateway.service'],check=True)
            # P65: webui.py/observatory.* are now targets too (see above) -
            # without this, a changed dashboard file sits on disk until
            # someone happens to restart the service separately, exactly the
            # gap the P64 Gazette page hit live.
            subprocess.run(['systemctl','restart','ai-village-webui.service'],check=True)
            subprocess.run(['systemctl','is-active','--quiet','ai-village-webui.service'],check=True)
            if args.no_start:
                print('Installed successfully without touching currently running resident services (--no-start); any resident that was already active keeps running the old code until you restart it.')
            else:
                pause_marker=Path(os.environ.get('VILLAGE_PAUSE_MARKER','/etc/ai-village/paused'))
                if pause_marker.exists():
                    print(f'Notice: Village is paused by operator ({pause_marker} exists). Resident services will not be started.')
                elif active:
                    subprocess.run(['systemctl','start',*active],check=True)
        if digest(args.env)!=env_hash: raise RuntimeError('host .env changed concurrently; investigate before continuing')
        status='installed'
    except BaseException:
        for name,info in reversed(list(saved.items())):
            path=Path(name)
            if info['existed']:
                shutil.copy2(backup/name.lstrip('/'),path); os.chown(path,info['uid'],info['gid'])
            elif path.exists(): path.unlink()
        subprocess.run(['systemctl','daemon-reload'])
        if not args.provision_only:
            subprocess.run(['systemctl','restart','ai-village-memory-gateway.service'])
            subprocess.run(['systemctl','restart','ai-village-webui.service'])
            if active: subprocess.run(['systemctl','restart',*active])
        status='rolled-back'
        raise
    finally:
        manifest=dict(timestamp=stamp,status=locals().get('status','failed'),source_revision=get_git_revision(source),release_source_hashes=release_hashes,source_hashes={str(v.relative_to(source)):digest(v) for v in targets.values() if v.exists()},env_sha256=env_hash,active_before=active,files=saved)
        (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
        print('Runtime backup/intervention manifest:',backup)
    print('Host .env unchanged; no dashboard files or model parameters rewritten.')


if __name__=='__main__': main()
