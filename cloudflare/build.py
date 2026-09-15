"""Bundle the same Python engine used by the local service."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent
package = root / 'src' / 'agent_inbox'
package.mkdir(parents=True, exist_ok=True)
for source in (root.parent / 'agent_inbox').glob('*.py'):
    target = package / source.name
    if not target.exists() or target.read_bytes() != source.read_bytes():
        shutil.copyfile(source, target)
