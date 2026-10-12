import json
import os
from pathlib import Path
import py_compile
import re
import shutil
import sys
import tempfile
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]

def csrf(client):
    with client.session_transaction() as state:return state['csrf_token']

def main():
    with tempfile.TemporaryDirectory(prefix='synaptiforge_account_actions_') as directory:
        base=Path(directory)
        for name in [file.name for file in ROOT.glob('*.py')]:
            shutil.copy2(ROOT/name,base/name);py_compile.compile(str(base/name),doraise=True)
        shutil.copytree(ROOT/'templates',base/'templates')
        os.chdir(base);sys.path.insert(0,str(base))
        for key in ('SYNAPTIFORGE_ENV','SYNAPTIFORGE_STORAGE_DIR'):os.environ.pop(key,None)
        os.environ['AUTH_DATA_DIR']=str(base/'data/auth')
        for key in ('SMTP_USERNAME','SMTP_PASSWORD','GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET'):os.environ[key]=''
        from app import app
        import account_actions as actions
        import auth_service
        app.config.update(TESTING=True,ACCOUNT_MAIL_INLINE_TEST=True)
        mails=[]
        def send(email,subject,body):
            mails.append((email,subject,body));return True
        def email_token(purpose):
            subject='Verify your Synaptiforge email' if purpose=='verify' else 'Reset your Synaptiforge password'
            body=next(body for _,title,body in reversed(mails) if title==subject)
            return re.search(r'token=([A-Za-z0-9_-]+)',body).group(1)
        with patch.object(actions,'send_account_message',side_effect=send),patch.object(auth_service,'send_account_message',side_effect=send):
            client=app.test_client();client.get('/signup')
            response=client.post('/signup',data={'csrf_token':csrf(client),'name':'Test','email':'verify@example.com','password':'original-password-2026','confirm_password':'original-password-2026'})
            assert response.location=='/verification-pending'
            assert client.get('/api/jobs').status_code==403
            verify=email_token('verify')
            with app.app_context(),auth_service.database() as db:
                assert db.execute('SELECT email_verified FROM users WHERE email=?',('verify@example.com',)).fetchone()[0]==0
                assert db.execute('SELECT digest FROM account_tokens').fetchone()[0]!=verify
                assert verify not in '\n'.join(row[0] for row in db.execute('SELECT payload FROM account_mail'))
            assert client.get('/verify-email?token='+verify).status_code==200
            assert client.get('/api/jobs').status_code==403 # GET must not activate account.
            assert client.post('/verify-email',data={'csrf_token':csrf(client),'token':verify}).status_code==302
            assert client.get('/api/jobs').status_code==200
            assert client.get('/verify-email?token='+verify).status_code==400
            assert client.get('/reset-password?token='+verify).status_code==400
            assert client.get('/api/jobs').json['jobs']==[]
            print('PASS: verification email, restricted unverified account, explicit confirmation, hashed/encrypted token storage, single use and token-purpose separation',flush=True)
            other_session=app.test_client();other_session.get('/login')
            assert other_session.post('/login',data={'csrf_token':csrf(other_session),'email':'verify@example.com','password':'original-password-2026'}).status_code==302
            reset=app.test_client();reset.get('/forgot-password')
            response=reset.post('/forgot-password',data={'csrf_token':csrf(reset),'email':'verify@example.com'})
            text=response.get_data(as_text=True)
            reset_token=email_token('reset')
            unknown=app.test_client();unknown.get('/forgot-password')
            missing=unknown.post('/forgot-password',data={'csrf_token':csrf(unknown),'email':'absent@example.com'}).get_data(as_text=True)
            # Only the session CSRF value differs in otherwise identical result pages.
            assert 'If a password account exists' in text and 'If a password account exists' in missing
            assert len(mails)==2
            assert reset.get('/reset-password?token='+reset_token).status_code==200
            assert reset.post('/reset-password',data={'csrf_token':csrf(reset),'token':reset_token,'password':'new-password-2026','confirm_password':'mismatch'}).status_code==400
            assert reset.post('/reset-password',data={'csrf_token':csrf(reset),'token':reset_token,'password':'new-password-2026','confirm_password':'new-password-2026'}).location=='/login'
            assert client.get('/api/jobs').status_code==401 and other_session.get('/api/jobs').status_code==401
            assert reset.get('/reset-password?token='+reset_token).status_code==400
            reset.get('/login')
            assert reset.post('/login',data={'csrf_token':csrf(reset),'email':'verify@example.com','password':'original-password-2026'}).status_code==401
            assert reset.post('/login',data={'csrf_token':csrf(reset),'email':'verify@example.com','password':'new-password-2026'}).status_code==302
            assert any(subject=='Your Synaptiforge password was changed' for _,subject,_ in mails)
            print('PASS: generic reset response, matching password confirmation, single-use reset, password-change notification, old password rejection and all previous sessions invalidated',flush=True)
            with app.app_context():
                google_id=auth_service.create_account('Google','google@example.com',google_sub='google-sub')
                with auth_service.database() as db:
                    assert db.execute('SELECT email_verified FROM users WHERE id=?',(google_id,)).fetchone()[0]==1
                before=len(mails);actions.request_link(google_id,'verify');actions.request_link(google_id,'reset');assert len(mails)==before
                user_id=auth_service.create_account('Expiry','expired@example.com',password_hash=auth_service.generate_password_hash('valid-password-2026'))
                actions.request_link(user_id,'verify')
                expired=email_token('verify')
                with auth_service.database() as db:
                    db.execute('UPDATE account_tokens SET expires=0 WHERE user_id=?',(user_id,))
            assert unknown.get('/verify-email?token='+expired).status_code==400
            with app.app_context():
                with patch.object(actions,'send_account_message',return_value=False):
                    actions.request_link(user_id,'reset')
                with auth_service.database() as db:
                    pending=db.execute("SELECT * FROM account_mail WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
                    assert pending and pending['payload']
                    message=json.loads(actions.cipher().decrypt(pending['payload'].encode()))
                    queued_token=re.search(r'token=([A-Za-z0-9_-]+)',message['body']).group(1)
                    assert queued_token not in pending['payload']
                    db.execute("UPDATE account_tokens SET expires=0 WHERE user_id=? AND purpose='reset'",(user_id,))
                before=len(mails)
                actions.deliver_message(pending['id'])
                assert len(mails)==before
                with auth_service.database() as db:
                    assert db.execute('SELECT status FROM account_mail WHERE id=?',(pending['id'],)).fetchone()[0]=='expired'
            assert unknown.get('/reset-password?token='+queued_token).status_code==400
            print('PASS: failed emails stay encrypted and queued; expired reset tokens and stale queued emails cannot be used',flush=True)
            assert unknown.get('/verify-email?token=tampered').status_code==400
            assert 'data-password-toggle="password"' in app.test_client().get('/login').get_data(as_text=True)
            assert 'Forgot password?' in app.test_client().get('/login').get_data(as_text=True)
            print('PASS: Google accounts skip verification, Google-only reset excluded, expired/tampered links rejected, private history remains empty and password eye/reset links render',flush=True)
        app.extensions['account_mail_executor'].shutdown(wait=True)

if __name__=='__main__':main()
