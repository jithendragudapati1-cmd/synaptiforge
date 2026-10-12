"""Global beta limits supplement account quotas and preserve disk headroom."""
import os
from pathlib import Path
import shutil

def storage_bytes():
    root=Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR') or '.')
    total=0
    for name in ('data','models'):
        for p in (root/name).rglob('*'):
            if p.is_file() and not p.is_symlink():
                try:total+=p.stat().st_size
                except FileNotFoundError:pass
    return total

def check_capacity(db,incoming=0):
    maximum=int(os.environ.get('MAX_GLOBAL_QUEUED_JOBS','10'))
    if db.execute("SELECT count(*) FROM queue WHERE state IN ('queued','running','cancelling')").fetchone()[0]>=maximum:
        raise RuntimeError('The training queue is full. Please try again later.')
    reserve=int(os.environ.get('TRAINING_RESERVE_MB','8'))*1024*1024
    if storage_bytes()+incoming+reserve>int(os.environ.get('GLOBAL_STORAGE_MB','1024'))*1024*1024:
        raise RuntimeError('The service storage limit has been reached. Contact support.')
    root=Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR') or '.')
    if shutil.disk_usage(root).free-incoming-reserve<int(os.environ.get('MIN_FREE_DISK_MB','256'))*1024*1024:
        raise RuntimeError('The server needs more free storage before accepting another job.')
