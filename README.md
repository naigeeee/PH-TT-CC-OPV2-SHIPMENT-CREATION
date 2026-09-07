# Substrait starter

A minimal, working Substrait app used in the AI Champions programme. It deploys as-is —
deploy it once unchanged to confirm everything works, then describe the app you actually
want to your AI assistant and deploy again.

**Start here:** open this folder in Claude Desktop or TraeWork and say:

> Read SETUP.md and do what it says.

**If anything ever goes wrong**, you don't need to know any commands. Just say:

> Something's not working — check my setup.

Your assistant will run a full diagnostic and tell you in plain language what to do.

What's in here:

| File | What it is |
|---|---|
| `SETUP.md` | One-time check of your computer. Start with this. |
| `AGENTS.md` | Instructions your AI reads. You don't need to. |
| `backend/main.py` | The whole app — the web page and the API. |
| `substrait.yaml` | What the app is, in one sentence. |
| `cicd/Dockerfile.backend` | How Substrait builds it. Rarely changes. |
| `openapi.json` | Describes the API. Your AI keeps it current. |
| `substrait.sh` | Shortcut for Substrait's own commands. |

This app stores nothing — it has no database, so it forgets everything when it restarts.
That is deliberate: it makes the first deploy as reliable as possible. Ask your AI to add
a database when you need one.
