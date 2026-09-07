# SETUP.md — read this and do what it says

**To the AI assistant reading this file: follow it in order. Do not skip ahead to
installing things.** The person you are helping is not a developer. Explain what you find
in plain language, never ask them to open a terminal and type, and never ask them for a
URL, endpoint, token or any other technical value — everything you need is in this file or
in AGENTS.md. The only things you may ask them for are their **name**, **work email** and
**GitHub username**.

---

## Step 0 — where this folder lives. Check before anything else.

Most Windows failures in this setup are caused by *where* the folder is, not by what's
installed. Run this one command and read the result:

```powershell
$p=(Get-Location).Path
"path      : $p"
"length    : $($p.Length)"
"onedrive  : $(if($p -match 'OneDrive|SharePoint'){'YES - MUST MOVE'}else{'no'})"
"nonascii  : $(if($p -match '[^\x20-\x7E]'){'YES - SHOULD MOVE'}else{'no'})"
"isproject : $(if(Test-Path substrait.yaml){'yes'}else{'NO - wrong folder'})"
```

**If `onedrive` says YES, stop and move the folder.** OneDrive locks files while syncing
them, which corrupts git repositories and produces "Permission denied" errors on files the
user can clearly edit in Explorer. It is not fixable in place:

```powershell
New-Item -ItemType Directory -Force C:\dev | Out-Null
Move-Item -LiteralPath (Get-Location).Path -Destination C:\dev\substrait-starter
```

Then tell them to **open `C:\dev\substrait-starter` in this editor** and start again from
Step 0. Same move if `length` is over ~120 (Windows breaks at 260) or `nonascii` says YES.

**If `isproject` says NO**, you are in the wrong folder — Windows' "Extract All" often
creates `substrait-starter\substrait-starter\`. Look one level down:

```powershell
Get-ChildItem -Directory | Where-Object { Test-Path (Join-Path $_ 'substrait.yaml') }
```

**If they downloaded a zip**, files may be marked as from the internet, which makes Windows
block or slow them. Clear it once: `Get-ChildItem -Recurse | Unblock-File`

---

## Step 1 — check the machine. Change nothing.

**Run ONE command, not several.** This editor's terminal sometimes returns nothing for a
command that actually succeeded, and seven separate checks give that seven chances to
happen. This writes results to a file as well as printing them.

```powershell
$L=@(); $L+="os: Windows PowerShell $($PSVersionTable.PSVersion)"
$g=(Get-Command git -EA SilentlyContinue); $L+= if($g){"git: "+(git --version)}else{"git: MISSING"}
$c=@("$env:LOCALAPPDATA\Programs\Git\bin\bash.exe","$env:ProgramFiles\Git\bin\bash.exe",
     "${env:ProgramFiles(x86)}\Git\bin\bash.exe","$env:ProgramW6432\Git\bin\bash.exe")
if($g){$c+=(Join-Path (Split-Path (Split-Path $g.Source)) 'bin\bash.exe')}
$b=$c | Where-Object {Test-Path $_} | Select-Object -First 1
$L+= if($b){"bash: $b"}else{"bash: MISSING"}
$p=(Get-Command python -EA SilentlyContinue)
if($p -and $p.Source -like '*\WindowsApps\python*' -and (Get-Item $p.Source).Length -eq 0){
  $L+="python: MISSING (Microsoft Store placeholder, not real Python)"
} elseif($p){$L+="python: "+(& $p.Source --version 2>&1)}
elseif(Get-Command py -EA SilentlyContinue){$L+="python: "+(py -3 --version 2>&1)}
else{$L+="python: MISSING"}
$L+= if(Get-Command winget -EA SilentlyContinue){"winget: "+(winget --version)}else{"winget: MISSING"}
$L+="net-github: $(git ls-remote https://github.com/substrait-build/substrait-claudecode-plugin.git HEAD 2>&1 | Select-Object -First 1)"
$L+="net-substrait: $(try{"reachable (HTTP "+(Invoke-WebRequest https://api.substrait.build -UseBasicParsing -TimeoutSec 15).StatusCode+")"}catch{if($_.Exception.Response){"reachable (HTTP "+[int]$_.Exception.Response.StatusCode+")"}else{"FAIL: "+$_.Exception.Message}})"
$L+="proxy: $env:HTTPS_PROXY | $((Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -EA SilentlyContinue).ProxyServer)"
$L+="clock: $(Get-Date -Format o)"
$out="$env:TEMP\setup-check.txt"; $L | Set-Content -LiteralPath $out; Get-Content -LiteralPath $out
```

`net-substrait: reachable (HTTP 404)` is **fine** — 404 means the server answered. Only
`FAIL:` means it could not be reached at all.

**If you get no output back**, that does not mean anything is missing. Read
`%TEMP%\setup-check.txt` — it was written even if nothing printed. If that doesn't exist
either, run `echo hello`. If even that returns nothing, **stop** and tell them: *"This
editor's command runner isn't responding. Check that command auto-run is enabled in
settings, then start a new task."*

**Never record a tool as missing because a command returned nothing.** Missing means it was
looked for and isn't there. Unknown means you couldn't check.

---

## Step 2 — report it

| What | Needed for | Found? |
|---|---|---|
| Git | Required — deploying | |
| Git Bash | Required — deploying | |
| Network to GitHub and Substrait | Required | |
| Python | Optional — previewing locally | |

Use ✅ found, ❌ genuinely absent, ❓ couldn't check. Then exactly one of:

- `READY — this computer can build and deploy Substrait apps.`
- `BLOCKED — this computer is missing: <list>`
- `COULD NOT CHECK — the command runner did not respond. Nothing is wrong with this computer yet.`

Use BLOCKED **only** when a check actually ran. Don't send someone to IT for software they
already have.

---

## Step 3 — install only what's missing

**Ask permission first.** Use exactly these commands. Do not substitute Chocolatey, Scoop or
WSL, and do not modify the system PATH.

**Windows — Git (this also provides Git Bash):**
```powershell
winget install --id Git.Git --scope user --silent --accept-source-agreements --accept-package-agreements
```

**Windows — Python** (only if they want to preview apps locally):
```powershell
winget install --id Python.Python.3.12 --scope user --silent --accept-source-agreements --accept-package-agreements
```

**macOS — Git:** `xcode-select --install`. Python is already there as `python3`.

**If a "do you want to allow this app to make changes" box appears**, tell them to cancel it
— they don't have admin, and the user-scope install should not need it.

**If `winget` is missing or blocked**, don't hunt for another installer. Tell them:
*"Your computer blocks the installer. Download Git from https://git-scm.com/download/win and
choose the option to install 'for me only' — it doesn't need admin rights."*

After installing, tell them to close and reopen this editor, then run Step 1 again.

---

## Step 4 — configure Git

Run these **through Git Bash**, not PowerShell — on some corporate machines the two write to
different config files, and settings made in one are invisible to the other.

```bash
git config --global core.autocrlf input     # stops Windows line endings breaking scripts
git config --global core.longpaths true     # survives long folder paths
git config --global credential.helper manager
git config --global user.name
git config --global user.email
```

If the last two print nothing, ask for their **name** and **work email** — the only personal
details you need — and set them. Without these the first commit fails.

**If anything says `SSL certificate problem`**, the company network inspects encrypted
traffic. Fix it by telling Git to use the Windows certificate store:

```bash
git config --global http.sslBackend schannel
```

**Never disable certificate checking** — not `http.sslVerify false`, not `curl -k`. If
schannel doesn't fix it, stop and tell them to ask IT for the company root certificate file.

---

## Step 5 — check the project

```bash
bash substrait.sh doctor
```

This checks the folder, line endings, git settings and network, and ends in `READY`,
`OK WITH WARNINGS` or `BLOCKED`. It changes nothing and needs no login. Act on any
`PROBLEM:` lines before going further.

**If it fails with `$'\r': command not found` or `syntax error near unexpected token`**, the
file has Windows line endings. Fix it once:

```bash
sed -i 's/\r$//' substrait.sh
```

Then run the doctor again.

**The first Substrait command downloads the tooling** and can take several minutes on a
machine with corporate antivirus. That is not a hang — let it finish.

---

## Known issue — "Connect GitHub" doesn't come back

If the Substrait GitHub App is **already installed** on the account or organisation,
clicking **Connect GitHub** strands you on GitHub and never returns. The redirect is fired
by GitHub's **Save** button, and Save is greyed out unless something changes. So:

1. On the GitHub page, find **Repository access**
2. Click **Only select repositories**
3. Click **All repositories** again
4. Click **Save**

GitHub then redirects to Substrait and the connection completes. Leaving it on **All
repositories** means future repos are already covered.

### ⚠ Do not uninstall the app

Uninstalling also works, but on a **shared organisation** it disconnects everyone else's
apps and their deploys start failing. Use the Save trick. If it doesn't work in a shared
org, stop and ask the organisation owner.

---

## Step 6 — what happens next

When the doctor passes, say this and stop:

> Your computer is ready. The rest happens in a web browser and needs you to click through
> it: request Builder access on Substrait, then connect your GitHub repo. Follow the
> pre-work instructions for those — then come back and tell me what you want to build.
>
> One thing worth remembering: if anything ever stops working, you don't need to know any
> commands. Just say **"something's not working — check my setup"** and I'll run a full
> diagnostic and tell you what to do.

**Do not attempt the browser steps yourself.** Requesting access, creating accounts and
authorising GitHub are for the person to do.
