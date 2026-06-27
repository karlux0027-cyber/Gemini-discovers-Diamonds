"""
KRLX Remote Agent v2.0 — Web-Enabled Command Bridge
=====================================================
Runs on your Windows RTX 5090 rig. Exposes an HTTP API on port 8742
that accepts commands over Tailscale. Manus POSTs commands, agent
executes them, returns results immediately.

Security Model (5-90-5):
  - Karl's 5%: Start this agent, set approval level
  - Agent's 90%: Execute commands autonomously within approved categories
  - Karl's 5%: Review results, override when needed

Approval Levels:
  - LEVEL 1 (Auto): File reads, health checks, status queries, transcription
  - LEVEL 2 (Auto): Install packages, run scripts in KRLX directories, git ops
  - LEVEL 3 (Prompt): System changes, new services, anything outside KRLX dirs
  - LEVEL 4 (Block): Destructive ops (format, delete system files, etc.)

Endpoints:
  POST /command       — Submit a command for execution
  GET  /health        — Agent health check
  GET  /results       — Get recent results
  GET  /results/{id}  — Get specific result by ID
  POST /approve/{id}  — Approve a pending Level 3 command
"""

import os
import sys
import json
import uuid
import asyncio
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ─────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────

AGENT_PORT = int(os.environ.get("KRLX_AGENT_PORT", "8742"))
AGENT_DIR = Path(os.path.expanduser("~/Desktop/KRLX_Agent"))
RESULTS_FILE = AGENT_DIR / "results.json"
LOG_FILE = AGENT_DIR / "agent.log"
PENDING_FILE = AGENT_DIR / "pending_approval.json"

# Tailscale IP range (100.x.x.x) — only allow connections from your mesh
TAILSCALE_PREFIX = "100."
LOCALHOST_PREFIXES = ["127.0.0.1", "::1", "localhost"]

# Working directories the agent is allowed to operate in freely (Level 1-2)
SAFE_DIRS = [
    Path(os.path.expanduser("~/Desktop/KRLX_Inbox")),
    Path(os.path.expanduser("~/Desktop/KRLX_Agent")),
    Path(os.path.expanduser("~/Desktop/KRLX_Ingestion_Server")),
    Path(os.path.expanduser("~/Gemini-discovers-Diamonds")),
    Path(os.path.expanduser("~/Desktop")),
]

# Commands that are always auto-approved (Level 1)
AUTO_APPROVE_COMMANDS = [
    "health", "status", "transcribe", "list_files", "read_file",
    "get_result", "python_version", "gpu_status", "tailscale_status",
    "pip_list", "dir", "type", "echo", "cat", "ls", "pwd",
    "git status", "git log", "git diff", "git pull",
    "nvidia-smi", "where", "whoami", "hostname",
]

# Commands that require user confirmation (Level 3)
PROMPT_COMMANDS = [
    "install", "service", "registry", "netsh", "new_service",
    "Set-ExecutionPolicy", "Enable-", "Disable-",
]

# Commands that are NEVER allowed (Level 4)
BLOCKED_COMMANDS = [
    "format", "del /s", "rmdir /s", "rm -rf", "shutdown",
    "diskpart", "bcdedit", "Remove-Item -Recurse -Force C:",
]

# ─────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────

AGENT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("krlx-agent")

# ─────────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────────

class CommandRequest(BaseModel):
    """Incoming command from Manus."""
    type: str = "shell"  # shell | python | builtin
    cmd: str
    working_dir: Optional[str] = None
    timeout: int = 60
    id: Optional[str] = None


class CommandResult(BaseModel):
    """Result of command execution."""
    id: str
    command: str
    type: str
    timestamp: str
    status: str  # success | error | blocked | denied | pending_approval | timeout
    output: str = ""
    error: str = ""


# ─────────────────────────────────────────────────────────────────────
# SECURITY
# ─────────────────────────────────────────────────────────────────────

def classify_command(cmd: str) -> int:
    """Classify command into approval levels: 1 (auto), 2 (auto-safe), 3 (prompt), 4 (blocked)."""
    cmd_lower = cmd.lower().strip()

    for blocked in BLOCKED_COMMANDS:
        if blocked.lower() in cmd_lower:
            return 4

    for prompt_cmd in PROMPT_COMMANDS:
        if prompt_cmd.lower() in cmd_lower:
            return 3

    for safe_cmd in AUTO_APPROVE_COMMANDS:
        if cmd_lower.startswith(safe_cmd.lower()):
            return 1

    return 2


def is_in_safe_dir(working_dir: Optional[str]) -> bool:
    """Check if the working directory is within approved paths."""
    if not working_dir:
        return True
    wd = Path(working_dir).resolve()
    for safe_dir in SAFE_DIRS:
        try:
            resolved = safe_dir.resolve()
            if wd == resolved or resolved in wd.parents:
                return True
        except Exception:
            continue
    return False


def is_tailscale_or_local(client_ip: str) -> bool:
    """Only allow Tailscale mesh IPs and localhost."""
    if client_ip.startswith(TAILSCALE_PREFIX):
        return True
    for prefix in LOCALHOST_PREFIXES:
        if client_ip.startswith(prefix) or client_ip == prefix:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
# RESULTS STORAGE
# ─────────────────────────────────────────────────────────────────────

def save_result(result: dict):
    """Append a result to the results file."""
    results = load_results()
    results.append(result)
    results = results[-200:]  # Keep last 200
    RESULTS_FILE.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")


def load_results() -> list:
    """Load all results."""
    if not RESULTS_FILE.exists():
        return []
    try:
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_pending(command: dict):
    """Save a command awaiting approval."""
    pending = load_pending()
    pending.append(command)
    PENDING_FILE.write_text(json.dumps(pending, indent=2, default=str), encoding="utf-8")


def load_pending() -> list:
    """Load pending approval commands."""
    if not PENDING_FILE.exists():
        return []
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────
# COMMAND EXECUTION
# ─────────────────────────────────────────────────────────────────────

async def execute_command(cmd_req: CommandRequest) -> dict:
    """Execute a command and return the result."""
    cmd_id = cmd_req.id or str(uuid.uuid4())[:8]
    result = {
        "id": cmd_id,
        "command": cmd_req.cmd,
        "type": cmd_req.type,
        "timestamp": datetime.now().isoformat(),
        "status": "pending",
        "output": "",
        "error": "",
    }

    # Security classification
    level = classify_command(cmd_req.cmd)

    if level == 4:
        result["status"] = "blocked"
        result["error"] = "Command blocked by security policy (Level 4)."
        logger.warning(f"BLOCKED: {cmd_req.cmd}")
        save_result(result)
        return result

    if level == 3:
        result["status"] = "pending_approval"
        result["error"] = "Requires user approval (Level 3). Use POST /approve/{id} after user confirms."
        logger.info(f"PENDING APPROVAL: {cmd_req.cmd}")
        save_pending({"id": cmd_id, **cmd_req.model_dump()})
        save_result(result)
        return result

    if level == 2 and not is_in_safe_dir(cmd_req.working_dir):
        result["status"] = "pending_approval"
        result["error"] = "Command targets directory outside safe zones. Requires approval."
        logger.info(f"PENDING APPROVAL (unsafe dir): {cmd_req.cmd}")
        save_pending({"id": cmd_id, **cmd_req.model_dump()})
        save_result(result)
        return result

    # Execute
    try:
        logger.info(f"EXECUTING [{cmd_req.type}]: {cmd_req.cmd}")

        if cmd_req.type == "builtin":
            output = handle_builtin(cmd_req.cmd)
            result["status"] = "success"
            result["output"] = output

        elif cmd_req.type == "python":
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", cmd_req.cmd],
                capture_output=True, text=True,
                timeout=cmd_req.timeout,
                cwd=cmd_req.working_dir,
            )
            result["status"] = "success" if proc.returncode == 0 else "error"
            result["output"] = proc.stdout
            result["error"] = proc.stderr

        else:  # shell
            proc = await asyncio.to_thread(
                subprocess.run,
                ["powershell", "-NoProfile", "-Command", cmd_req.cmd],
                capture_output=True, text=True,
                timeout=cmd_req.timeout,
                cwd=cmd_req.working_dir,
            )
            result["status"] = "success" if proc.returncode == 0 else "error"
            result["output"] = proc.stdout
            result["error"] = proc.stderr

        logger.info(f"COMPLETED [{result['status']}]: {cmd_req.cmd[:80]}")

    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["error"] = f"Command timed out after {cmd_req.timeout}s"
        logger.error(f"TIMEOUT: {cmd_req.cmd}")

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        logger.error(f"ERROR: {cmd_req.cmd} — {e}")

    save_result(result)
    return result


def handle_builtin(cmd: str) -> str:
    """Handle built-in agent commands."""
    cmd_lower = cmd.lower().strip()

    if cmd_lower == "health":
        return json.dumps({
            "agent": "krlx-agent-v2",
            "status": "operational",
            "timestamp": datetime.now().isoformat(),
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "agent_dir": str(AGENT_DIR),
            "ports": {"agent": AGENT_PORT, "whisper": 8741},
        })

    elif cmd_lower == "status":
        results = load_results()
        pending = load_pending()
        return json.dumps({
            "completed": len(results),
            "pending_approval": len(pending),
            "last_result": results[-1] if results else None,
        })

    elif cmd_lower == "gpu_status":
        try:
            proc = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10
            )
            return proc.stdout.strip()
        except Exception as e:
            return f"GPU query failed: {e}"

    elif cmd_lower == "tailscale_status":
        try:
            proc = subprocess.run(
                ["tailscale", "status"],
                capture_output=True, text=True, timeout=10
            )
            return proc.stdout.strip()
        except Exception as e:
            return f"Tailscale query failed: {e}"

    else:
        return f"Unknown builtin: {cmd}"


# ─────────────────────────────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"KRLX Agent v2.0 starting on port {AGENT_PORT}")
    logger.info(f"Agent directory: {AGENT_DIR}")
    logger.info("Security: Tailscale + localhost only")
    yield
    logger.info("KRLX Agent shutting down.")


app = FastAPI(
    title="KRLX Remote Agent",
    version="2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def tailscale_guard(request: Request, call_next):
    """Only allow Tailscale mesh IPs and localhost."""
    client_ip = request.client.host if request.client else "unknown"
    if not is_tailscale_or_local(client_ip):
        logger.warning(f"REJECTED connection from {client_ip}")
        raise HTTPException(status_code=403, detail="Access denied. Tailscale only.")
    return await call_next(request)


@app.get("/health")
async def health():
    """Quick health check."""
    return {
        "agent": "krlx-agent-v2",
        "status": "operational",
        "timestamp": datetime.now().isoformat(),
        "platform": sys.platform,
        "ports": {"agent": AGENT_PORT, "whisper": 8741},
    }


@app.post("/command")
async def submit_command(cmd: CommandRequest):
    """Submit a command for execution. Returns result immediately or pending status."""
    result = await execute_command(cmd)
    return result


@app.get("/results")
async def get_results(limit: int = 20):
    """Get recent command results."""
    results = load_results()
    return {"results": results[-limit:], "total": len(results)}


@app.get("/results/{cmd_id}")
async def get_result_by_id(cmd_id: str):
    """Get a specific result by command ID."""
    results = load_results()
    for r in reversed(results):
        if r.get("id") == cmd_id:
            return r
    raise HTTPException(status_code=404, detail=f"Result {cmd_id} not found")


@app.get("/pending")
async def get_pending():
    """Get commands awaiting approval."""
    return {"pending": load_pending()}


@app.post("/approve/{cmd_id}")
async def approve_command(cmd_id: str):
    """Approve a pending Level 3 command for execution."""
    pending = load_pending()
    target = None
    remaining = []
    for p in pending:
        if p.get("id") == cmd_id:
            target = p
        else:
            remaining.append(p)

    if not target:
        raise HTTPException(status_code=404, detail=f"No pending command with id {cmd_id}")

    # Save remaining pending
    PENDING_FILE.write_text(json.dumps(remaining, indent=2, default=str), encoding="utf-8")

    # Execute the approved command (bypass security since user approved)
    cmd_req = CommandRequest(
        type=target.get("type", "shell"),
        cmd=target.get("cmd", ""),
        working_dir=target.get("working_dir"),
        timeout=target.get("timeout", 60),
        id=cmd_id,
    )

    # Direct execution (skip classification)
    try:
        if cmd_req.type == "python":
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", cmd_req.cmd],
                capture_output=True, text=True,
                timeout=cmd_req.timeout,
                cwd=cmd_req.working_dir,
            )
        else:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["powershell", "-NoProfile", "-Command", cmd_req.cmd],
                capture_output=True, text=True,
                timeout=cmd_req.timeout,
                cwd=cmd_req.working_dir,
            )

        result = {
            "id": cmd_id,
            "command": cmd_req.cmd,
            "type": cmd_req.type,
            "timestamp": datetime.now().isoformat(),
            "status": "success" if proc.returncode == 0 else "error",
            "output": proc.stdout,
            "error": proc.stderr,
        }
    except Exception as e:
        result = {
            "id": cmd_id,
            "command": cmd_req.cmd,
            "type": cmd_req.type,
            "timestamp": datetime.now().isoformat(),
            "status": "error",
            "output": "",
            "error": str(e),
        }

    save_result(result)
    logger.info(f"APPROVED & EXECUTED: {cmd_req.cmd[:80]}")
    return result


@app.post("/deny/{cmd_id}")
async def deny_command(cmd_id: str):
    """Deny a pending command."""
    pending = load_pending()
    remaining = [p for p in pending if p.get("id") != cmd_id]
    PENDING_FILE.write_text(json.dumps(remaining, indent=2, default=str), encoding="utf-8")

    result = {
        "id": cmd_id,
        "timestamp": datetime.now().isoformat(),
        "status": "denied",
        "error": "User denied this command.",
    }
    save_result(result)
    return result


# ─────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║         KRLX REMOTE AGENT v2.0 (Web-Enabled)            ║
    ║         5% - 90% - 5% Autonomy Model                    ║
    ╠══════════════════════════════════════════════════════════╣
    ║  HTTP API:  http://0.0.0.0:8742                         ║
    ║  Security:  Tailscale + localhost only                   ║
    ╠══════════════════════════════════════════════════════════╣
    ║  POST /command     — Execute a command                   ║
    ║  GET  /health      — Health check                        ║
    ║  GET  /results     — Recent results                      ║
    ║  GET  /pending     — Commands awaiting approval          ║
    ║  POST /approve/id  — Approve a Level 3 command           ║
    ╠══════════════════════════════════════════════════════════╣
    ║  Level 1 (Auto):   reads, status, health checks          ║
    ║  Level 2 (Auto):   scripts in KRLX dirs, git ops         ║
    ║  Level 3 (Prompt): system changes → queued for approval  ║
    ║  Level 4 (Block):  destructive operations → rejected     ║
    ╠══════════════════════════════════════════════════════════╣
    ║  Press Ctrl+C to stop                                    ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=AGENT_PORT,
        log_level="info",
    )
