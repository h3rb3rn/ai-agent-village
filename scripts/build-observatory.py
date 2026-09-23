#!/usr/bin/env python3
"""Build a small observability release without running the Village bootstrap."""
import argparse
from pathlib import Path
import shutil

parser = argparse.ArgumentParser()
parser.add_argument('destination', type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
args.destination.mkdir(parents=True, exist_ok=True)
source = (root / 'bootstrap-ai-village.sh').read_text()
for marker, name in [('WEBUI', 'webui.py'), ('TELEMETRY', 'telemetry-collector.py'), ('RUNNER', 'agent-runner'), ('PROMPT', 'system-prompt.txt')]:
    code = source.split("<<'"+marker+"'\n", 1)[1].split('\n'+marker+'\n', 1)[0]
    if name.endswith('.py'): compile(code, name, 'exec')
    target = args.destination / name
    target.write_text(code+'\n')
    target.chmod(0o755)
for item in (root / 'web').glob('*'):
    if item.is_file(): shutil.copy2(item, args.destination / item.name)
unit = source.split("cat > /etc/systemd/system/ai-village-telemetry.service <<'UNIT'\n", 1)[1].split('\nUNIT', 1)[0]
(args.destination / 'ai-village-telemetry.service').write_text(unit+'\n')
