# Task Tracker — Deployment Guide

Deploys the app to **Render** (free web service) backed by **Neon** (free Postgres).

Final URL: `https://powertheworld.onrender.com`

---

## Prerequisites
- A GitHub account (Render needs the repo)
- A Neon account: https://neon.tech (free, no card required)
- A Render account: https://render.com (free, no card required)
- Your existing Resend API key (already in `.env`)

---

## 1. Create a Postgres database on Neon

1. Sign up / log in at https://neon.tech
2. Click **New Project** → name `task-tracker` → region close to you (e.g. `US West`) → free tier
3. After creation, copy the **Pooled connection string**. It looks like:
   `postgres://user:pass@ep-xxx-pooler.us-west-2.aws.neon.tech/neondb?sslmode=require`
4. Save this — you'll paste it into Render shortly.

---

## 2. Push the repo to GitHub

If the workspace isn't already a Git repo:

```powershell
cd "C:\Users\v-bryanyiu\VS Vibe Coding\Task Tracker"
git init
git add .
git commit -m "Initial commit"
git branch -M main
# Create an empty repo on github.com first, then:
git remote add origin https://github.com/<your-username>/task-tracker.git
git push -u origin main
```

`.gitignore` already excludes `.env`, `tasks.db`, `*.bak`, and Python venv.

---

## 3. Migrate local data to Neon (one-time)

You have ~7 tasks locally that won't otherwise carry over. Port them up first:

```powershell
cd "C:\Users\v-bryanyiu\VS Vibe Coding\Task Tracker"
$env:DATABASE_URL = "postgres://...neon...connection...string..."
python migrate_to_postgres.py
```

You should see counts like `users: 1 rows`, `workspaces: 1 rows`, `tasks: 7 rows` etc.

The script aborts if Neon already has data — safe to re-run only on an empty DB.

---

## 4. Deploy to Render

### Option A: Blueprint (easiest)
1. https://dashboard.render.com/select-repo?type=blueprint
2. Pick the GitHub repo → Render reads `Task Tracker/render.yaml` automatically
3. Add the four secrets when prompted:
   | Name | Value |
   |---|---|
   | `RESEND_API_KEY` | `re_T7UGnRKL_pifDUxSfSPgzG2iwDoP5AkWT` |
   | `EMAIL_FROM` | `Task Tracker <onboarding@resend.dev>` |
   | `EMAIL_TEST_TO` | `powertheworldwithbryan@gmail.com` |
   | `DATABASE_URL` | the Neon pooled connection string from step 1 |
4. Click **Apply** — first build ~3 min.

### Option B: Manual web service
- New → Web Service → connect repo → root dir `Task Tracker`
- Runtime: Python 3 · Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT`
- Add the same env vars as above + `FLASK_ENV=production`, `APP_BASE_URL=https://powertheworld.onrender.com`, and let Render generate `FLASK_SECRET_KEY`.

---

## 5. Verify the deploy

1. Visit `https://powertheworld.onrender.com` — should redirect to `/login`
2. **First request after a deploy / sleep is slow** (~30 s cold start on free tier). Subsequent requests are instant.
3. Log in as Bryan with the password you set locally — your migrated tasks should appear.
4. From phone: open the URL in Chrome/Safari → menu → **Add to Home Screen**. The app installs as a PWA with the icon and launches without browser chrome.

---

## Caveats

- **Free tier sleeps** after 15 min of inactivity. First hit takes ~30 s while the worker spins up.
- **Resend free tier** can only email `powertheworldwithbryan@gmail.com` until a domain is verified at resend.com/domains. Other users joining via invite link works fine — they just won't get a welcome email until the domain is set up.
- **Neon free tier** auto-suspends after 5 min idle and pauses entirely after 7 days inactive. Auto-resumes on connection.
- **Disable Flask debug** is automatic in production via `FLASK_ENV=production`.
- **Cookies** are `Secure + HttpOnly + SameSite=Lax` in production.
- **Rate limits** on auth endpoints: 10/min login, 5/min signup, 3/min forgot-password.

---

## Day-2 ops

- **View logs**: Render dashboard → service → Logs
- **Push update**: `git push` — Render redeploys on every commit (auto-deploy enabled in `render.yaml`)
- **Rotate secrets**: change in Render dashboard → service redeploys
- **Backup Neon**: Neon dashboard → Backups (point-in-time restore included on free tier)
- **Switch Resend sender to your domain**: verify a domain at resend.com/domains, then update `EMAIL_FROM` env var in Render
