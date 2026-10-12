"""Daily encrypted backups; idle retries keep unfinished training out of snapshots."""
import argparse
import json
import os
from pathlib import Path
import signal
import time
from dotenv import load_dotenv
ROOT=Path(__file__).resolve().parent
STOP=False


def stop(signum,frame):
    global STOP
    STOP=True


def main():
    os.chdir(ROOT);load_dotenv(ROOT/'.env')
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if os.environ.get('BACKUP_ENABLED','true').lower()!='true':
        while not STOP:
            signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop);time.sleep(.5)
        return 0
    # Initialize the local signing key before the very first snapshot.
    from app import app
    from backup_service import backup_directory,take_backup,record_status,successful_backups,prune,mirror_backups
    from datetime import datetime
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    directory=backup_directory()
    try:
        while not STOP:
            state={}
            try:
                prune(directory)
                if successful_backups(directory):
                    mirror_backups(directory)
                path=directory/'status.json'
                if path.exists():state=json.loads(path.read_text())
                last=datetime.fromisoformat(state['last_success']).timestamp() if state.get('last_success') else 0
                due=not successful_backups(directory) or time.time()-last>=max(60,int(os.environ.get('BACKUP_INTERVAL_SECONDS','86400')))
                if args.once or due:
                    saved=take_backup()
                    if args.once:return 0 if saved else 2
            except Exception:
                record_status(directory,'error',reason='Automatic backup failed. Check available storage and the backup configuration.')
                app.logger.warning('Automatic backup failed; another attempt will follow.')
                if args.once:return 1
            for _ in range(120):
                if STOP:break
                time.sleep(.5)
    finally:app.extensions['account_mail_executor'].shutdown(wait=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
