import os,time,shutil,sys,tempfile,py_compile
from pathlib import Path
from unittest.mock import patch,MagicMock
from urllib.parse import urlparse,parse_qs
ROOT=Path(__file__).resolve().parents[1]
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
        import auth_service
        app.config.update(TESTING=True,ACCOUNT_MAIL_INLINE_TEST=True)
        app.config.update(GOOGLE_CLIENT_ID='test-client',GOOGLE_CLIENT_SECRET='test-secret',GOOGLE_ENABLED=True,SMTP_USERNAME='sender@example.com',SMTP_PASSWORD='dummy-app-password')
        google=app.extensions['synaptiforge_google']
        google.client_id='test-client';google.client_secret='test-secret'
        with app.app_context():
            user_id=auth_service.create_account('Test Person','person@example.com',auth_service.generate_password_hash('A-long-test-password-2026'))
            with auth_service.database() as db:
                db.execute('UPDATE users SET email_verified=1 WHERE id=?',(user_id,))
                user=db.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        client=app.test_client();client.get('/login')
        with client.session_transaction() as state:csrf=state['csrf_token']
        client.post('/login',data={'csrf_token':csrf,'email':'person@example.com','password':'A-long-test-password-2026'})
        smtp=MagicMock();smtp.__enter__.return_value=smtp;smtp.send_message.return_value={}
        # Exercise real Authlib state, nonce, signature, issuer and audience validation.
        from joserfc.jwk import RSAKey
        from joserfc import jwt
        key=RSAKey.generate_key(2048,parameters={'kid':'test'},private=True)
        google=app.extensions['synaptiforge_google']
        metadata={'issuer':'https://accounts.google.com','authorization_endpoint':'https://accounts.google.com/o/oauth2/v2/auth','token_endpoint':'https://oauth2.googleapis.com/token','jwks_uri':'https://www.googleapis.com/oauth2/v3/certs','id_token_signing_alg_values_supported':['RS256']}
        def oauth_run(client,subject,email,**overrides):
            with patch.object(google,'load_server_metadata',return_value=metadata):
                response=client.get('/auth/google')
                query=parse_qs(urlparse(response.location).query)
                assert query['redirect_uri']==['http://127.0.0.1:5000/auth/google/callback']
                assert 'code_challenge' in query
                claims={'iss':metadata['issuer'],'aud':'test-client','sub':subject,'email':email,'email_verified':True,'name':'Google Person','iat':int(time.time()),'exp':int(time.time())+600,'nonce':query['nonce'][0]}
                claims.update(overrides)
                encoded=jwt.encode({'alg':'RS256','kid':'test'},claims,key)
                with patch.object(google,'fetch_access_token',return_value={'id_token':encoded,'access_token':'mock-token','token_type':'Bearer'}),patch.object(google,'fetch_jwk_set',return_value={'keys':[key.as_dict(private=False)]}),patch.object(auth_service.smtplib,'SMTP',return_value=smtp):
                    return client.get('/auth/google/callback?code=test-code&state='+query['state'][0])
        google_client=app.test_client()
        assert oauth_run(google_client,'google-user','google@example.com').location=='/'
        count=smtp.send_message.call_count
        returning=app.test_client()
        assert oauth_run(returning,'google-user','google@example.com').location=='/'
        assert smtp.send_message.call_count==count
        for changes in ({'nonce':'wrong'},{'aud':'other-client'},{'iss':'https://evil.invalid'},{'exp':int(time.time())-600},{'email_verified':False}):
            bad=app.test_client()
            assert oauth_run(bad,'bad-user','bad@example.com',**changes).location=='/login'
            assert bad.get('/api/jobs').status_code==401
        assert oauth_run(app.test_client(),'different-google-user','person@example.com').location=='/login'
        with patch.object(google,'load_server_metadata',return_value=metadata):
            assert app.test_client().get('/auth/google/callback?code=x&state=wrong').location=='/login'
        print('PASS: signed Google callback, new/returning accounts, PKCE, OAuth state, nonce, issuer, audience, expiry, verified-email checks and no automatic account linking',flush=True)
        def link_run(link_client, email='person@example.com', **overrides):
            with link_client.session_transaction() as state:
                csrf = state['csrf_token']
            with patch.object(google,'load_server_metadata',return_value=metadata):
                response = link_client.post('/auth/google/link',data={'csrf_token':csrf})
                query=parse_qs(urlparse(response.location).query)
                claims={'iss':metadata['issuer'],'aud':'test-client','sub':'linked-person','email':email,'email_verified':True,'name':'Linked Person','iat':int(time.time()),'exp':int(time.time())+600,'nonce':query['nonce'][0]}
                claims.update(overrides)
                encoded=jwt.encode({'alg':'RS256','kid':'test'},claims,key)
                with patch.object(google,'fetch_access_token',return_value={'id_token':encoded,'access_token':'mock-token','token_type':'Bearer'}),patch.object(google,'fetch_jwk_set',return_value={'keys':[key.as_dict(private=False)]}):
                    return link_client.get('/auth/google/callback?code=test-code&state='+query['state'][0])
        assert app.test_client().post('/auth/google/link').status_code==302
        assert client.post('/auth/google/link').status_code==302
        assert link_run(client,'different@example.com').location=='/'
        with app.app_context(),auth_service.database() as db:
            assert db.execute('SELECT google_sub FROM users WHERE id=?',(user_id,)).fetchone()[0] is None
        assert link_run(client,nonce='invalid-nonce').location=='/login'
        assert link_run(client).location=='/'
        with app.app_context(),auth_service.database() as db:
            linked = db.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
            assert linked['google_sub']=='linked-person' and linked['password_hash']==user['password_hash']
            assert db.execute('SELECT count(*) FROM mail_outbox WHERE user_id=?',(user_id,)).fetchone()[0]==1
        new_login=app.test_client()
        assert oauth_run(new_login,'linked-person','person@example.com').location=='/'
        with new_login.session_transaction() as state:
            assert state['user_id']==user_id
        print('PASS: authenticated CSRF-protected linking, matching email, verified nonce, original account/password preserved, Google login to same user, no duplicate welcome email',flush=True)
        app.extensions['account_mail_executor'].shutdown(wait=True)

if __name__=='__main__':main()
