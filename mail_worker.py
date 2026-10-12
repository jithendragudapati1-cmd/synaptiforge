"""Retry durable verification/reset and welcome mail in the background."""
from pathlib import Path
import os
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
    from app import app
    from auth_service import database,welcome_email
    from account_actions import deliver_message
    from job_queue import heartbeat,queue_db
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    with app.app_context():
        while not STOP:
            heartbeat('mail')
            try:
                from account_management import retry_deletions
                retry_deletions()
                with database() as db:
                    db.execute("DELETE FROM attempts WHERE created<?",(int(time.time())-900,))
                    db.execute("UPDATE account_mail SET status='pending' WHERE status='sending' AND updated<?",(int(time.time())-600,))
                    links=db.execute("SELECT id FROM account_mail WHERE status='pending' ORDER BY id LIMIT 20").fetchall()
                    welcomes=db.execute("SELECT mail_outbox.user_id FROM mail_outbox JOIN users ON users.id=mail_outbox.user_id WHERE users.email_verified=1 AND mail_outbox.status='pending' LIMIT 20").fetchall()
                for row in links:
                    if STOP:break
                    deliver_message(row['id'])
                for row in welcomes:
                    if STOP:break
                    welcome_email(row['user_id'])
            except Exception:
                app.logger.warning('Email worker will retry after an error.')
            for _ in range(20):
                if STOP:break
                time.sleep(.5)
        with queue_db() as db:db.execute("DELETE FROM service_state WHERE name='mail'")
    app.extensions['account_mail_executor'].shutdown(wait=True)

if __name__=='__main__':main()
