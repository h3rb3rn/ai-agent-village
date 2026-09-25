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
import tempfile
from datetime import datetime, timezone


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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',type=Path,default=Path('/opt/ai-agent-village/.env'))
    parser.add_argument('--provision-only',action='store_true')
    parser.add_argument('--dry-run',action='store_true',help='Validate targets, config and compilation without host changes')
    parser.add_argument('--no-start',action='store_true',help='Install files without starting or restarting resident services')
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1],help='Source directory containing web/ and prompts/')
    args=parser.parse_args()
    if not args.dry_run and os.geteuid()!=0: parser.error('must run as root')
    source=args.source.resolve()
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
    targets={
        Path('/usr/local/lib/ai-village/runtime.py'):source/'web/runtime.py',
        Path('/usr/local/lib/ai-village/decision.py'):source/'web/decision.py',
        Path('/usr/local/lib/ai-village/memory-gateway.py'):source/'memory/gateway.py',
        Path('/usr/local/share/ai-village/system-prompt.txt'):source/'prompts/resident-system.txt'}
    # Runtime imports are installed as a self-contained package.  Keeping these
    # modules in the targeted release prevents a live host from running a newer
    # runtime with an older, incomplete import tree.
    for module in ('__init__.py', 'artifacts.py', 'authority.py', 'config.py',
                   'containers.py', 'control.py', 'coordinator.py', 'inference.py',
                   'jobs.py', 'lifecycle.py', 'security.py', 'teams.py'):
        targets[Path('/usr/local/lib/ai-village/village') / module] = source / 'village' / module
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
        if not args.provision_only and active:
            subprocess.run(['systemctl','stop',*active],check=True)
        if not args.provision_only:
            for target,src in targets.items(): put(target,src.read_text())
            put(Path('/usr/local/lib/ai-village/agent-runner'), '#!/bin/sh\nexec /usr/bin/python3 /usr/local/lib/ai-village/runtime.py\n',0o755)
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
            if args.no_start:
                print('Installed successfully without starting or restarting resident services (--no-start).')
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
            if active: subprocess.run(['systemctl','restart',*active])
        status='rolled-back'
        raise
    finally:
        manifest=dict(timestamp=stamp,status=locals().get('status','failed'),source_hashes={str(v.relative_to(source)):digest(v) for v in targets.values() if v.exists()},env_sha256=env_hash,active_before=active,files=saved)
        (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
        print('Runtime backup/intervention manifest:',backup)
    print('Host .env unchanged; no dashboard files or model parameters rewritten.')


if __name__=='__main__': main()
