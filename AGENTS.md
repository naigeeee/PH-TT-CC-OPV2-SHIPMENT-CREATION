# AGENTS.md — how to work in this project

## READ THIS FIRST — the five rules that matter most

1. **Anything failing? Run `bash substrait.sh doctor` before guessing.** It checks the
   folder, line endings, git config and network, changes nothing, and ends in READY /
   OK WITH WARNINGS / BLOCKED. If the user says *"something's not working"*, *"check my
   setup"* or anything like it, that is your cue — run it first, then act on its
   `PROBLEM:` lines.
2. **Never disable TLS verification** — no `http.sslVerify false`, no `curl -k`. A
   certificate error means the company network inspects traffic; fix with
   `git config --global http.sslBackend schannel`.
3. **Never create or edit project files with PowerShell** (`>`, `Out-File`,
   `Set-Content`). It writes UTF-16 or a byte-order mark that silently breaks
   `requirements.txt`, `openapi.json` and every `.sh`. Use your own file-editing tool.
4. **On Windows the scripts need Git Bash, not PowerShell:**
   `& "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe" substrait.sh <command> 2>&1`
   **Always append `2>&1`.** The wrapper prints its errors and guidance to stderr, and
   some IDE runners (TraeWork among them) show you only stdout — without `2>&1` a
   failing command looks like "exit 1 with no output" and you are debugging blind.
   If any substrait.sh run ever exits non-zero with nothing printed, re-run it with
   `2>&1` before concluding anything.
5. **Deploying takes THREE steps, and pushing is only the first two.**
   ```bash
   git add -A && git commit -m "..." && git push
   bash substrait.sh deploy
   ```
   The snippet above is bash. **In the IDE runner (PowerShell) use `;` not `&&`** to chain
   commands — Windows PowerShell does not support `&&` and will error with *"not a valid
   statement separator"*.
   **`git push` alone does NOT publish anything.** Substrait's portal shows the label
   "auto-redeploys on push" — ignore it, it is wrong. The build is triggered by the deploy
   command, which tells Substrait to go and pull the branch you pushed. If you stop after
   pushing, the user's app will not change and you will have told them it did.
   This means **linking is required, not optional** — see *Linking* below.

Everything below is detail. The user is not a developer — do the work, then explain it in
plain language, and never ask them to open a terminal and type.

---

## Setting up a new app from the starter — the whole job is yours

When the user says anything like *"set up a new Substrait app in this folder from the
starter"*, do ALL of the following without sending them anywhere:

1. **Delete `.git` and start fresh — this is not optional.**
   The starter's `.git` folder points at `XB-AI-Champions/substrait-starter`.
   If you keep it, every push sends this user's code into the shared starter
   repo. **Always run this, even if you think it's already clean:**
   ```bash
   rm -rf .git && git init -b main
   ```
   Also delete `SUBSTRAIT-CONTRACT.md` if the starter brought one — it records
   the STARTER project's own link ("Linked app: substrait-starter") and is stale
   here. Linking this project recreates it with the right app.
2. **The app is named after this folder.** The folder's name is the repo name and the
   app name (one app = one folder = one repository — the folder IS the app). Do not
   invent a different name and do not ask for one; the user chose it when they named
   the folder.
3. **Create the private GitHub repository yourself** — see *Creating a new GitHub
   repository* below. Never send the user to github.com for this.
4. **Push everything** using the *Pushing to GitHub* runbook (username-in-remote, `main`).
5. Confirm in one short message: repo name, the account it was created under, pushed.

Then wait for the user to describe what they want the app to do.

**Build small, build fast.** When you then build or change the app: everything lives in
`backend/main.py` plus at most one new Flyway migration per change — no extra modules,
packages, helper files, config files, and no `frontend/` folder. Do not restructure the
starter and do not spend time re-reading every project file; `backend/main.py`,
`substrait.yaml` and the migration folder are the whole picture. One file is the design,
not a limitation — it is what keeps builds fast and deploys simple. Write the code in as
few passes as you can rather than many small exploratory edits.

One file caps the LAYOUT, never the ambition: build everything the user asked for, at
the quality and polish they asked for, inside that one file — never trim a feature or
simplify the design to stay small, and never keep or imitate the starter's example page
because it happens to be there. A full app with a rich page fits comfortably in one
`main.py`. If something genuinely won't fit well, say so and ask — don't quietly shrink
it.

---

**Command translation.** Substrait's own error messages tell you to run slash commands that
do not exist here. Translate them:

| Message says | Actually run |
|---|---|
| `/substrait:login` | `bash substrait.sh link account` |
| `/substrait:link` | `bash substrait.sh link apps` then `link use --app <slug>` |
| `/substrait:deploy` | `bash substrait.sh deploy` |
| `/substrait:init` | not applicable — this project is already set up |

---

## The rules Substrait enforces

| Rule | Detail |
|---|---|
| Backend port | Must listen on **8000** (`cicd/Dockerfile.backend`) |
| Health check | `GET /health` must return HTTP 200 |
| API location | Every JSON endpoint starts with **`/api`** |
| Backend Dockerfile | `cicd/Dockerfile.backend` must exist and `EXPOSE 8000` |
| Description | `substrait.yaml` needs a real `description:` — placeholders are rejected |
| No Kubernetes | Never create `k8s/`. The platform owns deployment. |
| No app slug | Never reference the platform-minted app slug. (A display name in the code is fine.) |
| DDL | All schema changes in Flyway migrations — **never** `CREATE TABLE` from application code |

**No `frontend/` folder** means Substrait routes *all* traffic — including `/` — to the
backend, so `backend/main.py` serves the page and the API. Keep it that way unless asked:
it removes a build step and a class of failure.

**Build context matters.** `cicd/Dockerfile.backend` is built with the **repo root** as
context, so its `COPY` paths are repo-root-relative (`COPY backend/ ./`). If you ever move
it to `backend/Dockerfile`, the context becomes `backend/` and every `COPY` path changes.

**Never `FROM nginx` in the backend Dockerfile.** Containers run with all Linux
capabilities dropped and stock nginx crashloops on its startup chown. Use
`nginxinc/nginx-unprivileged` with `listen 8000` if you need nginx.

---

## Files you will edit

```
backend/main.py           the entire app — the web page AND the API
backend/requirements.txt  Python packages
substrait.yaml            description, and database/services if needed
openapi.json              the published API description — keep it in step with the routes
cicd/Dockerfile.backend   rarely needs touching (see build context above)
```

Do **not** hand-edit `scaffold_version` in `substrait.yaml` — the deploy stamps it.

---

## House rules

**0. When anything fails, run the doctor before guessing.**

```bash
bash substrait.sh doctor
```

It checks the folder location, line endings, git settings and network reachability, changes
nothing, and ends in `READY`, `OK WITH WARNINGS` or `BLOCKED`. Act on its `PROBLEM:` lines
before forming your own theory.

**0a. Never disable TLS verification.** Not `git config http.sslVerify false`, not
`curl -k`, not `NODE_TLS_REJECT_UNAUTHORIZED=0`. A certificate error on a corporate laptop
means the network inspects traffic; the fix is
`git config --global http.sslBackend schannel`. If that doesn't work, stop and say so.

**0b. Never create or edit project files with PowerShell** — no `>`, `Out-File` or
`Set-Content` for anything in this project. Windows PowerShell writes UTF-16 or adds a
byte-order mark, which silently breaks `requirements.txt`, `openapi.json` and every `.sh`
file, and surfaces much later as an unexplained build failure. Use your own file-editing
tool.

**0c. Never write a `.ps1` file and run it.** Corporate policy commonly blocks script files.
Pass PowerShell as a single `-Command` string instead.

**0d. Never rename a file by changing only its capitalisation** — git on Windows won't
record it and the change never reaches the build. And never name a file `aux`, `con`, `nul`
or `prn`; Windows reserves those.

**1. On Windows, Substrait's scripts need Git Bash, not PowerShell.** `bash` is not on PATH:

```powershell
& "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe" substrait.sh deploy
```

Try these in order — the last one derives it from wherever `git.exe` actually is:

```
$env:LOCALAPPDATA\Programs\Git\bin\bash.exe
$env:ProgramFiles\Git\bin\bash.exe
${env:ProgramFiles(x86)}\Git\bin\bash.exe
(Join-Path (Split-Path (Split-Path (Get-Command git).Source)) 'bin\bash.exe')
```

It must be `Git\bin\bash.exe`, **never** `Git\usr\bin\bash.exe` — only the `bin`
wrapper sets up the PATH that `grep` and `head` need. On macOS just
`bash substrait.sh deploy`.

**2. Run every Substrait command from the project root** — the folder containing `backend/`
and `cicd/`. From anywhere else, deploy fails with "no backend/ here" and a stray
`.substrait/` folder gets created.

**3. Never print `.substrait/config.json`.** It holds a live deploy token in plain text.
Don't `cat` it while debugging.

**4. Never put secrets in code.** Custom config goes in `backend/.env.example` as
`NAME=value` lines, with a trailing `# secret` on anything sensitive. You can set real
values yourself:

```bash
bash substrait.sh env set MY_API_KEY --secret    # value piped on stdin, never as an argument
bash substrait.sh env list
```

**Never list `DATABASE_URL`, `JWT_SECRET`, `REDIS_URL`, `KAFKA_BROKERS`, `QDRANT_URL` or
`OBJECT_STORAGE_BUCKET`** in `.env.example` or set them via `env` — the platform injects
them and the server rejects those names.

---

## Knowing who the user is

With Google SSO on, the platform injects `X-Forwarded-Email` and `X-Forwarded-User` headers
into every backend request. **Never build a login page, OAuth flow or session handling** —
just read the header:

```python
email = request.headers.get("X-Forwarded-Email")
```

The browser never sees these headers, so a frontend must ask a backend endpoint such as
`/api/me`. Headers are stripped on `/health` and on any public paths, and are spoofable if
SSO is off — so don't trust them for anything sensitive when SSO isn't enabled.

---

## Adding a database

Declare it in `substrait.yaml`, or nothing is provisioned:

```yaml
database: oceanbase   # shared HA cluster, MySQL wire protocol, backed up — the default
# database: postgres  # or mysql — the app's OWN single-node pod, 10Gi, no HA, no backups
```

**The engine cannot be changed once deployed** — changing this value fails the deploy.
Choose deliberately the first time.

With `oceanbase` you are writing **MySQL**, not PostgreSQL: no `SERIAL`, no `RETURNING`, no
`ILIKE`, no `$1` placeholders. Use `BIGINT AUTO_INCREMENT`, `%s` placeholders. Never
substitute SQLite, not even for local testing — different driver, different placeholders,
different dialect.

**NEVER use Python `%` string formatting or f-strings to build SQL queries.** Always use
parameterized queries — `cursor.execute("... %s ...", (value,))`. This avoids SQL injection
AND prevents format-character clashes (e.g. `%Y` in a date format being misread as a Python
format specifier, which crashes the app with `unsupported format character`).

**The driver is pinned — do not choose one freely.** Async database code (SQLAlchemy
asyncio): `asyncmy==0.2.14`, exactly — never an older pin (0.2.9 has no wheel for the
Dockerfile's Python 3.12, so the deploy build fails at pip). Sync database code:
`PyMySQL` plus `cryptography`. General rule for ANY package you add: deploys build with
`--only-binary=:all:` on Python 3.12 and can never compile from source, so a compiled
package must ship a prebuilt cp312 wheel — check PyPI before pinning if you are not
certain.

**Parse `DATABASE_URL` with percent-decoding.** The injected URL percent-encodes special
characters in the username and the password (e.g. `%40` for `@`). Parse it with a real URL
parser and `unquote` BOTH the username and the password — a hand-rolled split fails at
runtime with "Access denied", which looks like a platform problem and isn't. While you're
there: give the app an exception handler that returns a readable error message instead of a
blank 500, so runtime failures can be diagnosed from the page.

**Two DDL shapes wedge the app permanently. Both are rejected at validation, and if one
ever lands it leaves a failed row in Flyway history that makes *every later deploy* fail:**

- Never add a column and its foreign key in one `ALTER TABLE` — split into two statements.
- Never use a self-referencing foreign key with `ON DELETE CASCADE`.

If a migration has already failed, fixing the SQL is not enough: the user must go to the
portal → the app's **Database** tab → **Repair migration history** first.

### No database connected? Use temporary memory

Every app you build MUST also run on a machine with no `DATABASE_URL` — that is how the
user tests locally before deploying (there is no local database and never will be; never
substitute SQLite).

- At startup, check for `DATABASE_URL`. Present → use the real database as normal.
  Absent → use a plain in-process store (dicts/lists) behind the SAME data-access
  functions, so every feature works identically.
- Make the mode visible: when running on temporary memory, log it at startup and show a
  small notice in the page (e.g. "Local test mode — data is not saved").
- Never write "temporary" data to files as a workaround, and never skip features in
  local mode — the point is that the user can try everything before it goes live.
- **The database driver must never block a local run.** Import it only where a real
  `DATABASE_URL` is used (create the engine lazily), so the app starts without it. If the
  driver fails to pip-install on the user's machine — typically no prebuilt wheel for
  their Python version, "needs build tools" — do NOT debug it, do NOT switch drivers,
  and do NOT change the pin: skip that package locally, run on temporary memory, and
  tell the user in one sentence that this is normal and the deployed app is unaffected
  (the deploy installs it inside Docker, where the pinned version is guaranteed to work).

### Where logging goes

**All logging goes to stdout** — `print` or Python's `logging` to the console — **never to
a file.** The container's filesystem is wiped on every restart and redeploy, and nothing
can read a file inside it, so a log file is silently useless.

Know where stdout is actually visible: **locally**, live in the dev-server window while
the user tests — that is where logging earns its keep. **Deployed, it is NOT visible**:
the portal has no logs view (verified — the app page has no Logs tab), so never tell the
user to "check the logs" on a live app. This makes readable error responses mandatory,
not optional (see the `DATABASE_URL` note above): when something fails at the live URL,
the page itself must say what went wrong in plain language, because the page is the only
surface anyone can see. Diagnose live problems by reproducing them locally, where the
logs exist.

---

## Adding Redis, Kafka, vector search or file storage

Declaring it in `substrait.yaml` is the **only** trigger. Installing a client library does
nothing — the service won't exist and the app will crash at runtime with no build warning.

```yaml
services:
  - object-storage   # durable private bucket -> OBJECT_STORAGE_BUCKET
  - redis            # -> REDIS_URL
  - kafka            # -> KAFKA_BROKERS
  - qdrant           # -> QDRANT_URL
```

**For file uploads use `object-storage`.** Files written to the container filesystem are
lost on every restart and redeploy.

---

## Heavy Python packages

The build has a disk ceiling. `torch` — often pulled in by `sentence-transformers` or
`transformers` — defaults to the CUDA build and drags in ~6 GB of NVIDIA wheels that don't
fit, and the cluster has no GPUs anyway. Pin CPU-only:

```
--index-url https://download.pytorch.org/whl/cpu
torch
```

---

## Linking this project to its Substrait app — REQUIRED

`bash substrait.sh deploy` cannot work until this machine is linked and this folder is
bound to the app. Do this once per machine and once per project, before the first deploy.

**The app must exist before the first deploy. Create it yourself — the CLI creation
ladder, in this order.** The app name is this folder's name (see *Setting up a new app
from the starter*). A successful create also binds this folder, so step 2 below is then
already done.

**Rung 1 — GitHub-connected from birth** (best when it works: pushes become the deploy
source). With the machine linked (step 1) and the repo pushed:

```bash
bash substrait.sh link create --name <app-name> --repo USERNAME/REPO
```

**When rung 1 is refused ("not installed" / "repo isn't reachable"), the usual cause is
Substrait's registry, not GitHub — and the fix takes the user 30 seconds.** Substrait
only learns about a GitHub App installation when GitHub sends it an event, and that
record can be missing or stale even while github.com shows the app installed with "All
repositories". Have the user do this (it works even when the setting ALREADY says All
repositories — clicking Save re-sends the registration): github.com → Settings →
Applications → Installed GitHub Apps → Substrait → **Repository access** → switch to
"Only select repositories", switch straight back to "**All repositories**", click
**Save**. Then retry rung 1 once — the repo should now appear in `link repos`. Never
suggest uninstalling the GitHub App, and never debug accounts: a repo missing from
`link repos` is not evidence of a wrong account, and you must never propose recreating
the repo under a different account.

**Rung 2 — if rung 1 is still refused (or the user isn't there to click), create the
app WITHOUT the repo:**

```bash
bash substrait.sh link create --name <app-name>
```

This makes an **upload-mode app**: `bash substrait.sh deploy` then packages this folder
and uploads it directly — no GitHub App involvement at all, which is exactly why this
rung works even when `link repos` can't see the repo. Keep pushing to GitHub exactly as
before (the repo stays the master copy); only the deploy transport differs, and the app
can be switched to GitHub deploys later with `link set-mode` once the installation is
visible. Do not treat rung 1's failure as a problem to solve first — a repo missing from
`link repos` is not evidence of a wrong account or a broken install; never debug
accounts, never loop asking which account is right, and never propose recreating the
repo under a different account. Go straight to rung 2 and ship.

**Rung 3 — only if rung 2 is ALSO refused** (a workspace with zip uploads disabled):
tell the user to open app.substrait.build → **Build** → **Connect GitHub** → pick the
repo (goes straight to the picker after the first time, ~45 seconds), then bind with
step 2 and deploy.

**"Not linked" at deploy time is THIS ladder, not a login problem.** If
`substrait.sh deploy` reports the folder isn't linked to an app (or `link status` shows
the machine linked but no app bound), the machine link from the pre-work is fine — the
project simply has no app yet. Run the creation ladder above immediately, yourself; do
NOT start a browser "link"/login flow, and do NOT ask the user to link anything.
And never take `SUBSTRAIT-CONTRACT.md` as evidence the folder is linked: the real
binding is the gitignored `.substrait/config.json`, which never arrives with a clone.
A `SUBSTRAIT-CONTRACT.md` naming `substrait-starter` is a stale copy from the starter —
delete it; linking rewrites it correctly.

### Step 1 — link this machine (browser). This is the normal way.

```bash
bash substrait.sh link status     # already linked? then skip to step 2
bash substrait.sh link            # authorise this machine — once per machine
```

**Run it so the user can see the output — never redirect it to a file, never pipe it,
never hide it.** It prints a URL and a verification code, opens the browser, then blocks
while it waits for approval. The blocking is expected; do not kill it.

**Relay the URL and the code to the user as text the moment they appear**, even though the
browser should open by itself:

> Open this link now and approve it — I'm waiting for you.

Nothing secret changes hands in this flow, which is why it's the default.

**Run it in a launched window, not from here** — see *Interactive steps* below. This
editor's runner kills long waits, and the browser cannot open from it. If you have already
tried it here and got *"link expired or was not approved in time"*, that is the runner
killing it, not a real expiry — open a window and run it there instead. Only if that also
fails, fall back to step 1b.

### Step 1b — fallback: link with a token

Only if the browser flow above was killed or keeps reporting expiry.

**Ask the user to mint the right kind of token.** There are two kinds and only one works:

> In app.substrait.build, use **Access tokens in the left sidebar** — *not* the Access
> tokens section inside an app. Click **Create token**, give it any name, and copy what it
> shows you. It's only shown once.

| Where they get it | Starts with | Scope |
|---|---|---|
| Left sidebar → **Access tokens** ✅ | `sbt_` | Every app they own — this is the one you need |
| An app's **Deploy** tab ❌ | `sbd_` | That single app only — `save-account` rejects it |

**Check the prefix.** If it starts with `sbd_`, tell them they took it from the app's Deploy
tab and need the sidebar page instead.

**Have them put it in a file, not in this chat:**

> Save it into a file called `token.txt` in this project folder — paste it into Notepad and
> save. Don't paste it into our conversation.

This keeps the secret out of the chat transcript, which the editor's vendor may store.
`token.txt` is already in `.gitignore`.

```bash
bash substrait.sh link save-account --token "$(cat token.txt)" --portal-url https://api.substrait.build
rm token.txt
```

Never print the token, never echo it back, never `cat token.txt` on its own.

### Step 2 — bind this folder to the app

```bash
bash substrait.sh link apps               # lists "slug<TAB>name" — show the names to the user
bash substrait.sh link use --app <slug>
```

**Reading `link status`:** its first line says "No account link on this machine" whenever
there's no personal token. **Read the last line**, not the first.

**Linking writes files** — `SUBSTRAIT-CONTRACT.md`, and a line in `.gitignore`. That dirties
the tree, so commit before deploying (the deploy runbook below covers it).

Use `link account` for authorisation, not `link login`. `login` mints an app-scoped token
that cannot run `apps`, `repos` or `set-mode`.

---

## Run everything inside this editor. A separate window is a last resort.

**Run every command here, in your own command runner.** Not because it looks tidier —
because **a separate window blinds you.** You cannot read its output, so you cannot see
`! [rejected] ... fetch first`, a merge conflict, or a failed build, and you cannot recover
from any of them. The user ends up relaying error text they don't understand, badly. Run it
here and you read the error yourself and fix it.

**These NEVER need a separate window** — no exceptions:

| Command | Why it's fine here |
|---|---|
| `bash substrait.sh doctor` | prints and exits |
| `bash substrait.sh check` | prints and exits |
| `bash substrait.sh deploy` | streams the build log for ~40s, then exits |
| `bash substrait.sh link` | opens the browser itself; you relay the code |
| `git add` / `commit` / `push` | no interaction once signed in — and you need to see the errors |

### Push failures you should fix yourself, here, without asking

| Git says | What it means | Do this |
|---|---|---|
| `! [rejected] ... (fetch first)` or `(non-fast-forward)` | GitHub has commits you don't | `git pull --rebase origin main` then push again |
| `Updates were rejected because the remote contains work` | same | as above |
| `divergent branches` / `need to specify how to reconcile` | no pull strategy set | `git config pull.rebase true`, then pull and push |
| a rebase stops on a conflict | two edits to the same lines | resolve it properly (below), `git add` the file, `git rebase --continue` |

**Never leave conflict markers in a file.** If you see `<<<<<<< HEAD`, `=======` or
`>>>>>>> origin/main`, the file is broken until you remove them and the unwanted side.
`substrait.sh`, `AGENTS.md` and `SETUP.md` are tooling, not the user's work — when they
conflict, take the newer copy wholesale rather than merging line by line:

```bash
git checkout --theirs substrait.sh   # during a rebase this is the incoming version
git add substrait.sh
```

`bash substrait.sh doctor` reports any file still containing markers.

**Never `git push --force`.** If a rebase can't resolve, stop and explain — force-pushing
can destroy work that someone else, or another copy of this folder, already pushed.

**The one case that may need a window:** the very first `git push` on a machine that has
never signed in to GitHub. Git Credential Manager tries to show a sign-in window and cannot
do so from a sandboxed runner, so the push fails silently with no prompt.

**Escalate only after an in-editor attempt has actually failed.** Do not pre-emptively open
a window because you think one might be needed. The sequence is:

1. Run the command here.
2. If it succeeds — and it usually will, because the credential is cached after the first
   time — you are done. Say nothing about windows.
3. Only if it fails with `Repository not found` or `could not read Username` — the two
   errors that mean a credential prompt could not be shown — and you have already checked
   the four causes in *Pushing to GitHub*, open a window. **Then immediately re-run the
   command here** so you can see the result yourself rather than relying on what the user
   reports:

```powershell
Start-Process powershell -WorkingDirectory '<FULL PATH TO THIS FOLDER>' -ArgumentList '-NoExit','-Command','git push -u origin main'
```

Use `-WorkingDirectory` rather than building a `cd '<path>';` string — a folder name with an
apostrophe, `&` or `$` breaks the quoting and the window opens on a syntax error.

**Never promise a browser window.** It only appears if they aren't already signed in. Say
what done looks like instead:

> I've opened a window. Either a sign-in page opens in your browser — approve it — or it
> just finishes, meaning you were already signed in. Either way, when the window shows
> `main -> main` it's done.

**Verify it yourself** with `git ls-remote origin` rather than waiting to be told.

---

## Deploying — commit, push, THEN deploy

**If the deploy reports this folder isn't linked to an app**, fix it yourself before
asking anything: run `bash substrait.sh link status`, then `bash substrait.sh link apps`.
If exactly one listed app matches this project's repo, bind it
(`bash substrait.sh link use --app <slug>`) and continue the deploy. If none or several
match, show the user the list and ask which one — never guess between two apps.

Substrait builds the **pushed** branch, but it does not notice the push by itself. Three
steps, every time, in this order:

```bash
git add -A && git commit -m "describe the change" && git push
bash substrait.sh deploy
```

**Never stop after the push.** The portal's "auto-redeploys on push" label is misleading —
the deploy command is what triggers the build. Reporting "deployed" after only pushing is
the single worst mistake you can make here, because the user reloads their app, sees no
change, and has no idea why.

**Expect the deploy to refuse the first time with "uncommitted changes … scaffold_version".**
The deploy stamps `substrait.yaml` itself *before* it checks the tree was clean, so its own
edit dirties it. This is normal. Recover without asking:

```bash
git add substrait.yaml && git commit -m "stamp scaffold version" && git push
bash substrait.sh deploy
```

**Keep `openapi.json` current.** The deploy warns when it is older than your latest
`backend/` change. It is only a warning, but the file ships as the app's published API
description — so when you add, remove or rename a route, update `openapi.json` in the same
edit.

**Fallback with no terminal:** the user can open the app in the portal and click
**Redeploy** in the header.

### Checking it worked

The user can see build state in the portal under the app's **Overview → Recent
deployments**. You can confirm the push landed with:

```bash
git ls-remote origin main
git rev-parse HEAD
```

If those two SHAs match, Substrait has what it needs.

### Optional: watching the build from here

Only if the user wants live build logs in this conversation. It requires the one-time
machine link described above, which is genuinely optional:

```bash
bash substrait.sh deploy
```

Don't set this up unless asked — pushing is enough.

### How it refuses, and what to do

| Message | Cause | Fix |
|---|---|---|
| "uncommitted changes ... scaffold_version stamp" | The deploy stamped `substrait.yaml` itself, before checking the tree was clean | Commit and push it, deploy again. Expected once after any tooling update. |
| "uncommitted changes" after linking | `SUBSTRAIT-CONTRACT.md` / `.gitignore` were just written | Commit and push, deploy again |
| "deploys from branch 'X' but you're on 'Y'" | Branch name must match exactly | `git branch -M X` or `git checkout X` |
| "local HEAD doesn't match the pushed tip" | Unpushed commits | `git push`, deploy again |
| "isn't a git checkout" | Wrong folder | Run from the repo root |
| "chose GitHub deploys but the app isn't connected" | Recorded mode vs server disagree | `bash substrait.sh link set-mode --mode connect --repo OWNER/REPO` (needs the account link) |
| HTTP 409 | Server-side SHA mismatch | Push, then deploy again |

A sign-in window during **deploy** (not push) is the `git fetch` the freshness check runs.

### After changing any API route

Update `openapi.json` in the same edit, to match what `backend/main.py` now serves. The
deploy warns when it's older than your latest `backend/` change, and warns if it's missing —
currently advisory, but slated to become a hard requirement.

### `bash substrait.sh check`

Run before every deploy. Exit 0 = compliant, exit 1 = problems. It reports all of these:

- no backend Dockerfile
- `frontend/` exists but ships no frontend Dockerfile
- no `substrait.yaml`, or no `description:`, or the placeholder description
- Flyway migrations exist but no `database:` declared
- a `k8s/` directory is present

**A green check is not a deploy guarantee.** The server runs additional checks: an nginx
backend base image, unresolvable `COPY` paths, the two banned DDL shapes, and a changed
database engine are all rejected server-side.

---

## If you add a frontend later

Only when the user asks. Then: `cicd/Dockerfile.frontend` serving the built site on **port
80**; call the backend same-origin via relative `/api` paths; **never hardcode an API URL
and never set `VITE_API_URL`**. Public build-time values go in a committed
`frontend/.env.production` (already un-ignored in `.gitignore`).

---

## Running it locally — do this BEFORE every deploy

When the user says anything like *"run it on my computer so I can try it first"*, or after
any change worth checking, run the app locally and hand them the address. Treat local
testing as the default step before deploying, not an optional extra.

```bash
pip install -r backend/requirements.txt
cd backend && uvicorn main:app --reload --port 8000
```

- **If port 8000 is already in use, try the next port up** (8001, 8002, …) until you find
  a free one. Always tell the user which port the app is running on and give them the full
  clickable URL (`http://127.0.0.1:PORT`). Never silently fail with "address already in
  use" — just pick the next port.

- **The server runs until stopped, so it cannot live in this editor's runner** (the runner
  kills long waits — same reason as the link flow). Launch it in a window using the
  *Interactive steps* pattern above, or run it in the background if your runner supports
  that, then tell the user:

  > The app is running on your computer at **http://127.0.0.1:8000** — open that in your
  > browser and try it. Nobody else can see this address.

- Works with or without a database: locally there is no `DATABASE_URL`, so the app runs on
  temporary memory (see *No database connected? Use temporary memory* above) — everything
  works, but records vanish when the server restarts. Say so if the app stores data:

  > On your computer the app uses temporary memory — anything you add here disappears
  > when it restarts. Once deployed, records are kept permanently in the database.

- When the user is happy, stop the server and proceed to commit → push → deploy.
- If Python is missing, don't fight it — deploy instead and read the live URL, but say
  that's what you're doing.

---

## Creating a new GitHub repository — do it yourself

Never send the user to github.com to create a repository. Create it for them using the
Git credential already cached on this machine.

**Establish the username first** (`git config --global github.user`; ask and store it if
unset — same rule as *Pushing to GitHub* below).

**State the target account BEFORE creating, every time:**

> I'll create the private repository **REPO** under the GitHub account **USERNAME** —
> tell me now if it should go somewhere else.

Take the username from the credential you are about to use, not from guesswork. If the
credential's username and `github.user` disagree, stop and ask which account is intended.

**If the user chooses an account that has no cached credential on this machine**, get one
— two ways, in order:

1. **Git's own sign-in (preferred — nothing to install).** Put the chosen username in the
   remote URL (*Pushing to GitHub*, step 3) and run a remote operation — Git Credential
   Manager shows a sign-in window for that account, once (step 7 there). After the
   sign-in, re-run the credential-fill creation, adding `username=USERNAME` as a third
   line of the `git credential fill` input so it selects that account's credential.
2. **gh.** `gh auth login` for that account (install gh first if needed — see the
   fallback list below), then `gh repo create REPO --private`.

If both fail, have the user create the repo at github.com/new (private, no README) and
continue with the push runbook. Whichever path you take, keep the chosen username in the
remote URL so the two accounts never mix.

**Create it with the cached credential** (no new sign-in, no gh needed) — run through
Git Bash like every other bash snippet here. **Don't fight quoting:** on Windows,
passing this snippet inline through PowerShell (`bash -lc '...'`) mangles the `\n`
escapes and wastes turns. Instead, write the snippet to a temp `.sh` file OUTSIDE the
project (e.g. `$env:TEMP\substrait-mkrepo.sh`) with your file-write tool, run it with
Git Bash (`bash /path/to/substrait-mkrepo.sh`), and delete it afterwards. A `.sh` file
outside the project doesn't break the no-PowerShell-scripts rule — that rule is about
`.ps1` files and project files.

```bash
CRED=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill)
TOKEN=$(printf '%s' "$CRED" | sed -n 's/^password=//p')
LOGIN=$(printf '%s' "$CRED" | sed -n 's/^username=//p')
curl -sS -X POST https://api.github.com/user/repos \
  -H "Authorization: token $TOKEN" -H "Accept: application/vnd.github+json" \
  -d '{"name":"REPO","private":true}'
```

Rules for this flow:

- **Always `"private": true`.** App repos are private, in the user's personal account.
- **Never print, echo, or paste the token anywhere** — not in the conversation, not in a
  file, not in an error report. Use it and discard the variables.
- A **401/403 or an empty token** means the cached credential can't create repos on this
  machine. Fall back in order: (1) `gh repo create REPO --private` — if gh isn't
  installed, install it yourself (`winget install --scope user GitHub.cli`, no admin
  needed) and sign in with `gh auth login` (device-code flow: relay the code and URL to
  the user as text, same rules as the Substrait link flow); (2) only if gh also fails,
  walk the user through creating it at github.com/new (private, no README) — the one
  case where they open GitHub.
- A **422 "name already exists"** means the repo is already there — skip creation and
  continue to the push runbook.

Then push using the *Pushing to GitHub* runbook below (username-in-remote, `main` branch).

---

## Pushing to GitHub — follow this exactly, every time

Do not push a URL the user gave you verbatim. Normalise it first, and **act at each step —
don't stop and report a problem you can still fix.**

**1. Their GitHub username, once.** `git config --global github.user` — if blank, ask
*"What's your GitHub username?"* and save it: `git config --global github.user USERNAME`.

**2. Branch must be `main`** (or whatever the app is connected to): `git branch -M main`.

**3. Username in the remote URL** — this lets several GitHub accounts coexist:

```bash
git remote set-url origin https://USERNAME@github.com/ORG/REPO.git
```

**4. Verify you are NOT pointing at the starter.** Run `git remote -v` and check
the URL does NOT contain `XB-AI-Champions/substrait-starter`. If it does, you
skipped setup step 1 — go back and delete `.git`, reinit, create a new repo, and
set the remote to the user's own repo. **Never push to the starter.**

**5. Check the destination:** `git ls-remote origin`. Refs listed = good.

**6. "Repository not found"** — four causes, identical message. Work through all four
before reporting:

| Check | How | If so |
|---|---|---|
| Username missing from remote | `git remote -v` | Redo step 3 |
| Repo doesn't exist | Open `https://github.com/ORG/REPO` | 404 → create it yourself — see *Creating a new GitHub repository* above |
| Wrong account cached | Page loads, push still fails | `git ls-remote https://USERNAME@github.com/ORG/REPO` and let them sign in |
| Stale generic credential | `cmdkey /list \| findstr -i github` | `cmdkey /delete:git:https://github.com`, retry |

**7. The first push on a machine needs a real window.** Git Credential Manager's sign-in
cannot appear from inside this editor. **Launch a window for them** — see *Interactive
steps* above — never ask them to open a terminal and type. After that first success,
Windows caches the credential and every later push from here is silent.

**7b. Do not promise a sign-in window** — say what success looks like instead: "A GitHub sign-in window is about to open — that's
expected, it only happens once." No window plus instant failure means a credential problem
above, not a network one.

---

## Troubleshooting

**Browser authorisation succeeds but `link status` still says "No account link".** The
editor's sandbox blocked writing the credential to `~/.substrait`. `substrait.sh` detects
this and redirects the credential into the tooling folder automatically — make sure you ran
the link through `bash substrait.sh link` (never the plugin scripts directly), and just run
it once more. Do not ask the user to change editor permission settings; that path has been
tried and doesn't work reliably.

**First Substrait command seems to hang.** It's downloading the tooling into
`~/.substrait-tools`. Run it again.

**`link apps` errors with "no account link on this machine".** Run `bash substrait.sh link`
first — `apps`, `repos` and `set-mode` all need the account link.

**`$'\r': command not found` / `syntax error near unexpected token`.** Windows line
endings. Run `sed -i 's/\r$//' substrait.sh`, then `git config --global core.autocrlf input`
so it doesn't recur.

**`Permission denied` or `Unable to create '.git/index.lock'` on a file they can clearly
edit.** OneDrive is holding the file. The folder must be moved out of OneDrive — pausing
sync only helps until it resumes. See SETUP.md Step 0.

**`! [rejected] main -> main (non-fast-forward)`.** Another copy of this folder was pushed
first. Find the other copy — do **not** force-push, it will destroy their work.

**"link expired or was not approved in time".** Usually this editor's runner killing the
command mid-wait, but **check the machine clock too** — if it's more than a few minutes off,
the token really is rejected as expired, and no amount of retrying helps.

**Everything fails with "could not reach https://api.substrait.build".** Check whether they
need to be on the company VPN. Never work around it by disabling certificate checks.

**`echo: write error: Bad file descriptor` from any `substrait.sh` command.** The IDE runner
closed the output pipe before the script finished. The command **still ran** — the output
just didn't reach you. The script automatically saves a complete log every time it runs.
**Read `.substrait-last-run.log` in the project folder** — it has the full output. Act on
what the log says; do not retry the command just because the runner showed an error.

This is not a Substrait problem, not a linking problem, and not an auth problem. Don't
diagnose it, don't ask the user about it — just read the log file.

**Never fall back to running the scripts in PowerShell.** They are bash and will not work.
