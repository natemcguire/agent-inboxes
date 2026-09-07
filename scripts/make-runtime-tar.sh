#!/bin/sh
# Usage: make-runtime-tar.sh [commit=HEAD] [output-directory=dist]
set -eu
cd "$(dirname "$0")/.."
exec python3 - "$@" <<'PY'
import ast
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

def git(*args):
    return subprocess.check_output(['git', *args])

commit = git('rev-parse', '--verify', (sys.argv[1] if len(sys.argv) > 1 else 'HEAD') + '^{commit}').decode().strip()
out = Path(sys.argv[2] if len(sys.argv) > 2 else 'dist')
out.mkdir(parents=True, exist_ok=True)
names = sorted(n for n in git('ls-tree', '-r', '--name-only', commit).decode().splitlines()
               if n == 'bin/agent-inbox' or (n.startswith('agent_inbox/') and n.endswith('.py') and n.count('/') == 1))
# FILES is an explicit distribution contract shared with the standalone installer.
namespace = {}
source = ast.parse(git('show', f'{commit}:agent_inbox/distribution.py'))
assignment = next(n for n in source.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'FILES' for t in n.targets))
exec(compile(ast.Module(body=[assignment], type_ignores=[]), '<FILES>', 'exec'), namespace)
if set(names) != namespace['FILES']:
    sys.exit('Runtime FILES does not match tracked runtime modules; update the distribution and installer allowlists.')
version_source = ast.parse(git('show', f'{commit}:agent_inbox/__init__.py'))
version = next(ast.literal_eval(n.value) for n in version_source.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets))
archive = out / f'agent-inboxes-{commit}-runtime.tar'
with tarfile.open(archive, 'w', format=tarfile.USTAR_FORMAT) as tar:
    for name in names:
        payload = git('show', f'{commit}:{name}')
        entry = tarfile.TarInfo(name)
        entry.size = len(payload)
        entry.mode = 0o755 if name == 'bin/agent-inbox' else 0o644
        entry.uid = entry.gid = entry.mtime = 0
        entry.uname = entry.gname = ''
        tar.addfile(entry, io.BytesIO(payload))
sha = hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix('.tar.sha256').write_text(f'{sha}  {archive.name}\n')
manifest = dict(schema=1, version=version, commit=commit, sha256=sha,
                url=f'https://nates-software.com/downloads/{archive.name}', minimum_supported='1.2.0')
archive.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest, indent=2))
PY
