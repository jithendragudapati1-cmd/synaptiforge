# Synaptiforge Login and Email Setup

## What is ready

The app now has email/password sign-up, login, logout, protected dashboard/API routes, Google OpenID Connect sign-in, and one queued welcome email per new account. Passwords are stored as salted hashes. Existing project datasets, training jobs, and saved models remain in place.

Each account has private training jobs, uploads, saved models, checkpoints, and prediction-model selection. Historical jobs created before accounts were introduced belong to the original account. Email/password sign-ups must verify their email before accessing the workspace. Google sign-ups are verified automatically. Verification links expire in 24 hours; password reset links expire in 30 minutes. Both are single-use. Password reset signs out all existing sessions. Welcome mail is sent after verification. Existing Google-linked accounts remain verified; older password-only accounts verify on their next login.

## Try email/password sign-up now

1. Restart the server in its terminal: press **Control+C**, then run:

```bash
cd ~/synaptiforge
source venv/bin/activate
python run_local.py
```

2. Open [the login page](http://127.0.0.1:5000/login).
3. Click **Create an account**.
4. Enter your name, email, and a password of at least 12 characters.
5. Create the account. You will be signed in to the dashboard.

Without Gmail credentials, the welcome email is stored as pending. The app says it is queued rather than claiming it was sent. Tests use a mocked sender and do not send real messages.

## Configure credentials locally

In the project folder, copy `.env.example` to `.env` and edit `.env`:

```bash
cp .env.example .env
```

Only run that copy command when `.env` does not already exist. Keep passwords and OAuth secrets in this local file; do not share them in chat or commit them. The file is excluded by `.gitignore`.

## Enable Google sign-in

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Choose or create a project.
3. Configure the Google Auth Platform consent screen/branding and audience. For an external app in testing, add your own Google account as a test user.
4. Create an OAuth client with application type **Web application**.
5. Add this exact authorized redirect URI:

```text
http://127.0.0.1:5000/auth/google/callback
```

6. Put the client ID and secret into `.env`:

```text
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
```

7. Restart the app. **Continue with Google** will become available.

Use `127.0.0.1:5000` consistently for the local app. A localhost URL with a different host or port requires matching `APP_BASE_URL` and its own authorized redirect URI. Google requires the redirect URI to match. See [Google's web-server OAuth guide](https://developers.google.com/identity/protocols/oauth2/web-server) and [OpenID Connect guide](https://developers.google.com/identity/openid-connect/openid-connect).

Google sign-in validates OAuth state and the signed ID token through [Authlib's Flask OpenID Connect integration](https://docs.authlib.org/en/latest/oauth2/client/web/flask.html). Only Google profiles with a verified email can create an account. Existing accounts are identified by Google's subject ID. Matching an existing password-account email does not automatically merge accounts. Log in with your password, click **Link Google Account** at the top of the dashboard, and choose the Google account with the same email. Once linked, either sign-in method works.

## Enable Gmail welcome emails

1. Turn on **2-Step Verification** for the sender's Google account.
2. Create an [app password](https://myaccount.google.com/apppasswords), if your account's policies allow it.
3. Set these values in `.env`:

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-sender-address@gmail.com
SMTP_PASSWORD=your-app-password
```

Use the sender's actual address and app password. Do not use the normal Google account password. SMTP uses STARTTLS with certificate verification. Google Workspace policies may restrict app passwords; ask your administrator if unavailable. See [Google's app-password instructions](https://support.google.com/accounts/answer/185833) and [Gmail SMTP settings](https://support.google.com/mail/answer/7104828).

4. Restart the app.
5. Create a genuinely new account using an email address you control. Account creation queues one welcome email and immediately attempts delivery.

Existing-account logins do not generate another welcome message. SMTP acceptance is not a guarantee of inbox delivery; check Spam if necessary.

## Retry previously queued messages

After configuring Gmail, run from the project folder:

```bash
source venv/bin/activate
python -m flask --app app retry-welcome-emails
```

This sends pending welcome messages to the email addresses recorded during account creation. There is no continuously running email worker. Failed delivery stays queued for a later retry. The outbox reduces duplicate sends, but SMTP cannot guarantee exactly-once delivery if the process stops after sending and before recording success.

## Account storage and deployment

- `data/auth/accounts.sqlite3`: users, rate-limit records, and welcome-email status.
- `data/auth/session.key`: generated persistent local signing key, unless `SECRET_KEY` is configured.
- `.env`: OAuth and email credentials.

These files contain private account information or secrets. They are excluded from version control. Protect any future full-project backups that include them. The backup created before login was added does not contain these new accounts or secrets.

The app defaults to debug mode off after this change. For public deployment, set an HTTPS `APP_BASE_URL`, set `COOKIE_SECURE=true`, configure an explicit strong `SECRET_KEY`, register the HTTPS Google callback, and use a production server. Account storage is isolated; this remains a single-computer development application.

## Verification and password recovery

Use **Forgot password?** on login to request a reset link. Google-only users use Google sign-in instead. Unverified accounts have a **Resend verification email** button. If email delivery fails, run `python -m flask --app app retry-account-emails` after fixing the sender settings. Links are encrypted in the email queue and token hashes are stored separately. The latest resend replaces earlier verification/reset links. Email links use APP_BASE_URL and work on this laptop while the app is running.

## Hosting and background delivery

The launcher now runs a persistent email-retry worker and training queue alongside the web app. See DEPLOYMENT.md for HTTPS URLs, OAuth callback changes, and persistent private storage. Account pages now appear under Account in the dashboard.
