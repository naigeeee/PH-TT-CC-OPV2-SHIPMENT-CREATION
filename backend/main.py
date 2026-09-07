"""
Substrait starter app — a single backend that serves both its web page and its API.

Substrait requires three things of this file:
  1. the server listens on port 8000          (set in cicd/Dockerfile.backend)
  2. GET /health returns 200                  (Substrait's readiness check)
  3. the JSON API lives under /api            (Substrait routes /api here)

Because this project has no frontend/ folder, Substrait sends ALL traffic to this
backend — including "/" — so this file also serves the page you see in the browser.

To change the app, describe what you want to your AI assistant. It will edit this file.
"""

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

APP_NAME = "My First Substrait App"

app = FastAPI(title=APP_NAME, docs_url="/api/docs")


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.get("/api/info")
def info():
    return {
        "app": APP_NAME,
        "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__APP_NAME__</title>
<link href="https://api.fontshare.com/v2/css?f[]=satoshi@400;500;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; margin: 0; }
  body {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Satoshi', ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #f5f5f4;
    padding: 40px 20px;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 440px; width: 100%; text-align: center; }
  .header { margin-bottom: 40px; }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #f0fdfa;
    padding: 6px 14px;
    border-radius: 20px;
    margin-bottom: 24px;
  }
  .pill-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: #b0b0b0;
  }
  .pill-dot.ok { background: #10b981; }
  .pill-dot.err { background: #ef4444; }
  .pill-dot.wait { animation: blink 1.3s ease-in-out infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.2} }
  .pill-text {
    font-size: 12px;
    font-weight: 500;
    color: #0d9488;
    letter-spacing: 0.02em;
  }
  h1 {
    font-size: 32px;
    font-weight: 700;
    color: #1c1917;
    letter-spacing: -0.025em;
    line-height: 1.2;
  }
  .card {
    background: #fff;
    border-radius: 12px;
    padding: 24px;
    text-align: left;
    box-shadow: 0 0 0 1px rgba(0,0,0,0.04), 0 2px 8px rgba(0,0,0,0.03);
  }
  .card-status {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 20px;
  }
  .card-icon {
    width: 36px; height: 36px;
    border-radius: 10px;
    background: #f5f5f4;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .card-icon.ok { background: #f0fdf4; }
  .card-icon.err { background: #fef2f2; }
  .card-label {
    font-size: 14px;
    font-weight: 600;
    color: #1c1917;
  }
  .card-sub {
    font-size: 12px;
    color: #a8a29e;
    margin-top: 2px;
  }
  .card-rule {
    height: 1px;
    background: #f3f4f6;
    margin-bottom: 20px;
  }
  .card-hint {
    font-size: 13.5px;
    color: #78716c;
    line-height: 1.65;
  }
  .card-hint code {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 12.5px;
    background: #f0fdfa;
    color: #0d9488;
    padding: 2px 8px;
    border-radius: 5px;
  }
  .foot {
    font-size: 12px;
    color: #c4c4cc;
    margin-top: 28px;
  }
  @media (max-width: 480px) {
    h1 { font-size: 27px; }
    .card { padding: 20px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="pill">
      <div class="pill-dot wait" id="pdot"></div>
      <span class="pill-text" id="ptxt">Checking&hellip;</span>
    </div>
    <h1>__APP_NAME__</h1>
  </div>

  <div class="card">
    <div class="card-status">
      <div class="card-icon" id="cico">
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
          <circle cx="9" cy="9" r="4" fill="#d4d4d4" id="cdot"/>
        </svg>
      </div>
      <div>
        <p class="card-label" id="clbl">Connecting&hellip;</p>
        <p class="card-sub" id="csub">Waiting for backend</p>
      </div>
    </div>
    <div class="card-rule"></div>
    <p class="card-hint">
      Served from <code>backend/main.py</code> &mdash;
      describe what you want and your AI will rebuild this page.
    </p>
  </div>

  <p class="foot">Powered by Substrait</p>
</div>

<script>
  fetch("/api/info")
    .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function(d){
      document.getElementById("pdot").className = "pill-dot ok";
      document.getElementById("ptxt").textContent = "Deployed on Substrait";
      document.getElementById("cico").className = "card-icon ok";
      document.getElementById("cdot").setAttribute("fill", "#10b981");
      document.getElementById("clbl").textContent = "Online";
      document.getElementById("csub").textContent = "All systems operational";
    })
    .catch(function(){
      document.getElementById("pdot").className = "pill-dot err";
      document.getElementById("ptxt").textContent = "Backend unreachable";
      document.getElementById("cico").className = "card-icon err";
      document.getElementById("cdot").setAttribute("fill", "#ef4444");
      document.getElementById("clbl").textContent = "Offline";
      document.getElementById("csub").textContent = "Backend not responding";
    });
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def homepage():
    return PAGE.replace("__APP_NAME__", APP_NAME)
