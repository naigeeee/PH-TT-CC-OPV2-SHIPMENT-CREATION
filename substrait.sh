#!/usr/bin/env bash
# Wrapper around Substrait's official tooling. Clones their plugin on first use and
# refreshes it after, so you always run their current scripts — nothing is forked.
#
#   bash substrait.sh doctor    check this machine and this project, change nothing
#   bash substrait.sh check     audit the project against Substrait's deploy contract
#   bash substrait.sh link      authorise this machine (opens your browser)
#   bash substrait.sh deploy    build and ship, streaming the log
#   bash substrait.sh env ...   manage the app's env vars and secrets
#   bash substrait.sh library   browse the internal API catalogue (needs the account link)
set -uo pipefail

# ── Always run from the project root, whatever the caller's working directory ─────
cd "$(dirname "$0")" 2>/dev/null || { echo "Error: cannot enter the script's folder." >&2; exit 1; }
if [ ! -f substrait.yaml ] || [ ! -d backend ]; then
  echo "Error: this doesn't look like the project folder (no substrait.yaml / backend/)." >&2
  echo "       I am in: $(pwd)" >&2
  echo "       Open the folder that contains substrait.yaml and try again." >&2
  exit 1
fi

# ── Pipe-safe output ────────────────────────────────────────────────────────────
# IDE runners (TraeWork, OpenCode) sometimes close stdout before the script
# finishes, causing "echo: write error: Bad file descriptor". We capture all
# output to a log file so nothing is lost even when the pipe breaks. The AI
# reads the log file; the user sees live output in a real terminal.
_SUBSTRAIT_LOG="$(pwd)/.substrait-last-run.log"
if [ "${_SUBSTRAIT_INNER:-}" != "1" ]; then
  export _SUBSTRAIT_INNER=1
  if [ -t 1 ]; then
    # Real terminal — stream live AND keep a log
    "$0" "$@" 2>&1 | tee "$_SUBSTRAIT_LOG"
    exit ${PIPESTATUS[0]}
  else
    # Piped (IDE runner) — capture to file, immune to broken pipes
    "$0" "$@" > "$_SUBSTRAIT_LOG" 2>&1
    _rc=$?
    cat "$_SUBSTRAIT_LOG" 2>/dev/null
    exit $_rc
  fi
fi

# ── Where the tooling lives ──────────────────────────────────────────────────────
# Prefer LOCALAPPDATA on Windows: $HOME can be a redirected or mapped network drive on
# corporate images, which makes the clone fail or crawl.
TOOLS="${SUBSTRAIT_TOOLS:-}"
if [ -z "$TOOLS" ] && [ -n "${LOCALAPPDATA:-}" ] && command -v cygpath >/dev/null 2>&1; then
  TOOLS="$(cygpath -u "$LOCALAPPDATA")/substrait-tools"
fi
TOOLS="${TOOLS:-$HOME/.substrait-tools}"
SCRIPTS="$TOOLS/substrait-plugin/scripts"
REPO="https://github.com/substrait-build/substrait-claudecode-plugin.git"

# Some editor sandboxes (TraeWork) block writes to ~/.substrait, which silently breaks
# saving the account link: the browser OAuth succeeds but the credential can't be stored,
# and the link reports failure. Probe writability; if blocked, keep the credential in the
# tooling directory instead — the same location the sandbox already allows us to clone into.
# Machine-level either way, so one link covers every project. An existing ~/.substrait link
# is left alone, and a pre-set SUBSTRAIT_GLOBAL_CONFIG always wins.
if [ -z "${SUBSTRAIT_GLOBAL_CONFIG:-}" ] && [ ! -f "$HOME/.substrait/config.json" ]; then
  if ! ( mkdir -p "$HOME/.substrait" && : > "$HOME/.substrait/.wtest" && rm -f "$HOME/.substrait/.wtest" ) 2>/dev/null; then
    mkdir -p "$TOOLS" 2>/dev/null
    export SUBSTRAIT_GLOBAL_CONFIG="$TOOLS/account-config.json"
    echo "Note: this editor blocks ~/.substrait — keeping the Substrait account link in"
    echo "      $SUBSTRAIT_GLOBAL_CONFIG instead. This is normal and machine-wide."
  fi
fi

# Substrait ships no default portal URL; setting this means no subcommand needs --portal-url.
export SUBSTRAIT_PORTAL_URL="${SUBSTRAIT_PORTAL_URL:-https://api.substrait.build}"
# Linking appends the platform's own contract block to a memory file. Keep it out of
# AGENTS.md — its text describes the zip-upload path and would contradict ours.
export SUBSTRAIT_MEMO_FILE="${SUBSTRAIT_MEMO_FILE:-SUBSTRAIT-CONTRACT.md}"
# On a network that intercepts TLS, point this at the corporate root CA .pem and curl/git
# will trust it, instead of anyone reaching for --insecure.
if [ -n "${SUBSTRAIT_CA_BUNDLE:-}" ] && [ -f "${SUBSTRAIT_CA_BUNDLE}" ]; then
  export CURL_CA_BUNDLE="$SUBSTRAIT_CA_BUNDLE" SSL_CERT_FILE="$SUBSTRAIT_CA_BUNDLE" \
         GIT_SSL_CAINFO="$SUBSTRAIT_CA_BUNDLE"
fi

# ── Fetch / refresh the tooling ──────────────────────────────────────────────────
fetch_tools() {
  if [ ! -d "$TOOLS/.git" ]; then
    # A part-finished clone leaves a non-repo directory that blocks every future attempt.
    rm -rf "$TOOLS"
    echo "First run: downloading Substrait's tooling (this can take a few minutes behind"
    echo "corporate antivirus). Please wait…"
    # autocrlf=false is essential: with Git for Windows' default, every .sh below is
    # checked out with CRLF and dies with "$'\r': command not found".
    if ! git clone --depth 1 --config core.autocrlf=false --config core.eol=lf \
         "$REPO" "$TOOLS" 2>&1; then
      rm -rf "$TOOLS"
      echo "Error: could not download the Substrait tooling from GitHub." >&2
      echo "       Usually a proxy, firewall or VPN issue rather than a broken repo." >&2
      echo "       Check that github.com opens in your browser, then try again." >&2
      exit 1
    fi
    echo "Tooling ready."
  else
    # The plugin moved from the developer's personal account to substrait-build.
    # A clone made before the move still pulls from the old URL; re-point it so
    # refreshes keep tracking the maintained repo.
    if [ "$(git -C "$TOOLS" remote get-url origin 2>/dev/null)" != "$REPO" ]; then
      git -C "$TOOLS" remote set-url origin "$REPO" 2>/dev/null || true
    fi
    git -C "$TOOLS" pull --ff-only --quiet 2>/dev/null \
      || git -C "$TOOLS" fetch --depth 1 -q origin 2>/dev/null \
      && git -C "$TOOLS" reset --hard -q '@{u}' 2>/dev/null || true
  fi
  if [ ! -d "$SCRIPTS" ]; then
    rm -rf "$TOOLS"
    echo "Error: the tooling download was incomplete. Run this command again." >&2
    exit 1
  fi
  # An older clone (made before we forced LF) can hold CRLF scripts that fail with
  # "$'"'"'\r'"'"': command not found". Detect and re-clone rather than hand-patching.
  if LC_ALL=C grep -qU $'"'"'\r'"'"' "$SCRIPTS/substrait-deploy.sh" 2>/dev/null; then
    echo "Tooling had Windows line endings — re-downloading it cleanly…"
    rm -rf "$TOOLS"
    git clone --depth 1 --config core.autocrlf=false --config core.eol=lf "$REPO" "$TOOLS" >/dev/null 2>&1 \
      || { echo "Error: re-download failed." >&2; exit 1; }
  fi
}

# ── Open a URL in the real browser ───────────────────────────────────────────────
# explorer.exe first: a shell opening a URL is not an EDR signature, whereas
# bash -> powershell -> Start-Process is a classic one and gets blocked silently.
# MSYS_NO_PATHCONV is set for THIS command only — setting it globally breaks the path
# conversion curl needs for its CA bundle, and every API call then fails.
# ── Make the platform's own browser-opener work ──────────────────────────────────
# substrait-link.sh calls substrait_open_url(), which on Windows runs `explorer.exe "$url"`.
# Git Bash mangles the URL on the way, so Explorer gets something it can't parse and falls
# back to opening the user's Documents folder. Exporting a shell function with that exact
# name shadows the real binary inside the child script, so THEIR call lands here and we
# open the browser properly. Fixing it at the source beats racing it with a second opener,
# which is what produced a Documents window every time regardless.
#
# MSYS_NO_PATHCONV is scoped to this one command. Setting it globally breaks the path
# conversion curl needs for its CA bundle, and every API call then fails with
# "could not reach".
explorer.exe() {
  local url="${1:-}"
  [ -n "$url" ] || return 1
  MSYS_NO_PATHCONV=1 powershell.exe -NoProfile -NonInteractive \
    -Command "Start-Process '"'"'$url'"'"'" >/dev/null 2>&1 && return 0
  return 1
}
export -f explorer.exe 2>/dev/null || true

# ── doctor: one command that tells you whether anything here can work ────────────
doctor() {
  local fail=0 warn=0
  echo "Substrait project doctor"
  echo "------------------------"
  printf 'folder          : %s\n' "$(pwd)"

  case "$(pwd)" in
    *OneDrive*|*SharePoint*)
      echo "  PROBLEM: this folder is inside OneDrive. Sync locks files while git is using"
      echo "           them, which corrupts the repository. Move it to C:\\dev first."
      fail=1 ;;
  esac
  if [ "$(pwd | LC_ALL=C wc -c)" -gt 120 ]; then
    echo "  WARNING: long path — Windows breaks at 260 characters. Prefer C:\\dev."
    warn=1
  fi
  if LC_ALL=C printf '%s' "$(pwd)" | LC_ALL=C grep -q '[^ -~]'; then
    echo "  WARNING: the path contains non-English characters, which some tools mishandle."
    warn=1
  fi

  # Unresolved merge conflict markers left in a file by a bad pull. Very common when an
  # AI resolves a conflict badly, and it silently breaks whatever file it lands in.
  local conflicted
  conflicted="$(LC_ALL=C grep -rlE '^<{7} |^>{7} ' --exclude-dir=.git . 2>/dev/null | head -5)"
  if [ -n "$conflicted" ]; then
    echo "merge conflicts : FOUND"
    printf '  PROBLEM: these files still contain unresolved conflict markers:\n'
    printf '           %s\n' $conflicted
    echo "           Fix them before anything else — the files are broken as they stand."
    fail=1
  else
    echo "merge conflicts : none"
  fi

  # CRLF is the single most common Windows breakage.
  if LC_ALL=C grep -qU $'\r' substrait.sh 2>/dev/null; then
    echo "line endings    : BROKEN (Windows CRLF)"
    echo "  FIX: sed -i 's/\r$//' substrait.sh   then run this again"
    fail=1
  else
    echo "line endings    : ok (LF)"
  fi

  printf 'bash            : %s\n' "${BASH_VERSION:-unknown}"
  if command -v git >/dev/null 2>&1; then printf 'git             : %s\n' "$(git --version)"
  else echo "git             : MISSING"; fail=1; fi
  if command -v curl >/dev/null 2>&1; then printf 'curl            : %s\n' "$(curl --version | head -1)"
  else echo "curl            : MISSING"; fail=1; fi

  printf 'autocrlf        : %s\n' "$(git config --get core.autocrlf 2>/dev/null || echo '(unset)')"
  printf 'longpaths       : %s\n' "$(git config --get core.longpaths 2>/dev/null || echo '(unset)')"
  printf 'credential help : %s\n' "$(git config --get credential.helper 2>/dev/null || echo '(unset)')"
  printf 'git remote      : %s\n' "$(git remote get-url origin 2>/dev/null || echo '(none)')"
  printf 'git branch      : %s\n' "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(no commits)')"

  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
          "$SUBSTRAIT_PORTAL_URL/api/link/start" 2>/dev/null)" || code=""
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    echo "substrait api   : reachable (HTTP $code)"
  else
    echo "substrait api   : UNREACHABLE"
    echo "  Usually a proxy, VPN or TLS-interception issue. Do NOT disable certificate"
    echo "  checks. Try: git config --global http.sslBackend schannel"
    fail=1
  fi

  echo "------------------------"
  if [ "$fail" -ne 0 ]; then echo "RESULT: BLOCKED — fix the PROBLEM lines above."; return 1
  elif [ "$warn" -ne 0 ]; then echo "RESULT: OK WITH WARNINGS"; return 0
  else echo "RESULT: READY"; return 0; fi
}

cmd="${1:-}"; shift 2>/dev/null || true

# ── Guard: refuse to deploy/check from the shared starter repo ──────────────
# If the git remote still points at the starter, the AI skipped setup step 1.
# Catch it here before code lands in the wrong repository.
_guard_starter() {
  local _origin
  _origin="$(git remote get-url origin 2>/dev/null)" || return 0  # no remote is fine
  case "$_origin" in
    *XB-AI-Champions/substrait-starter*|*xb-ai-champions/substrait-starter*)
      echo "" >&2
      echo "═══════════════════════════════════════════════════════════════════" >&2
      echo "  STOP — git remote still points at the shared STARTER repo:"     >&2
      echo "         $_origin"                                                 >&2
      echo ""                                                                  >&2
      echo "  You must create your OWN repository first."                      >&2
      echo "  See AGENTS.md → 'Setting up a new app from the starter'."       >&2
      echo ""                                                                  >&2
      echo "  Quick fix:"                                                      >&2
      echo "    rm -rf .git && git init -b main"                               >&2
      echo "    git remote add origin https://USER@github.com/ACCOUNT/REPO.git" >&2
      echo "    git add -A && git commit -m 'initial commit'"                  >&2
      echo "    git push -u origin main"                                       >&2
      echo "═══════════════════════════════════════════════════════════════════" >&2
      echo "" >&2
      exit 1 ;;
  esac
}

case "$cmd" in
  doctor) doctor ;;
  check)  _guard_starter; fetch_tools; bash "$SCRIPTS/substrait-deploy.sh" check ;;
  link)   fetch_tools
          if [ $# -eq 0 ]; then set -- account; fi
          bash "$SCRIPTS/substrait-link.sh" "$@" ;;
  deploy) _guard_starter; fetch_tools; bash "$SCRIPTS/substrait-deploy.sh" --watch "$@" ;;
  env)    fetch_tools; bash "$SCRIPTS/substrait-env.sh" "$@" ;;
  library) fetch_tools; bash "$SCRIPTS/substrait-library.sh" "$@" ;;
  *) echo "usage: bash substrait.sh {doctor|check|link|deploy|env|library}" >&2; exit 2 ;;
esac
