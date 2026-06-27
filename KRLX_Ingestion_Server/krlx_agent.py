"""
KRLX Remote Agent — The Bridge Between Manus and Your Rig
==========================================================
This script runs on your Windows machine and polls a shared command queue.
Manus writes commands → this agent executes them → results go back.

Architecture:
  - Uses a simple file-based queue on GitHub (commands.json / results.json)
  - Agent polls every 5 seconds for new commands
  - Executes approved commands locally on your RTX 5090 rig
  - Posts results back for Manus to read

Security Model (5-90-5):
  - Karl's 5%: Start this agent, set approval level
  - Agent's 90%: Execute commands autonomously within approved categories
  - Karl's 5%: Review results, override when needed

Approval Levels:
  - LEVEL 1 (Auto): File reads, health checks, status queries, transcription
  - LEVEL 2 (Auto): Install packages, run scripts in KRLX directories, git operations
  - LEVEL 3 (Prompt): System changes, new services, anything outside KRLX dirs
  - LEVEL 4 (Block): Destructive ops (format, delete system files, etc.)
"""

import os
import sys
import json
import time
import uuid
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────

# Where the agent stores its queue and results
AGENT_DIR = Path(os.path.expanduser("~/Desktop/KRLX_Agent"))
COMMANDS_FILE = AGENT_DIR / "commands.json"
RESULTS_FILE = AGENT_DIR / "results.json"
LOG_FILE = AGENT_DIR / "agent.log"

# Poll interval (seconds)
POLL_INTERVAL = 5

# Working directories the agent is allowed to operate in freely (Level 1-2)
SAFE_DIRS = [
    Path(os.path.expanduser("~/Desktop/KRLX_Inbox")),
    Path(os.path.expanduser("~/Desktop/KRLX_Agent")),
    Path(os.path.expanduser("~/Desktop/KRLX_Ingestion_Server")),
    Path(os.path.expanduser("~/Gemini-discovers-Diamonds")),
]

# Commands that are always auto-approved (Level 1)
AUTO_APPROVE_COMMANDS = [
    "health", "status", "transcribe", "list_files", "read_file",
    "get_result", "python_version", "gpu_status", "tailscale_status",
    "pip_list", "dir", "type", "echo",
]

# Commands that require user confirmation (Level 3)
PROMPT_COMMANDS = [
    "install", "service", "registry", "netsh", "new_service",
]

# Commands that are NEVER allowed (Level 4)
BLOCKED_COMMANDS = [
    "format", "del /s", "rmdir /s", "rm -rf", "shutdown",
    "diskpart", "bcdedit",
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
# COMMAND QUEUE
# ─────────────────────────────────────────────────────────────────────

def load_commands() -> list:
    """Load pending commands from the queue file."""
    if not COMMANDS_FILE.exists():
        return []
    try:
        data = json.loads(COMMANDS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, Exception):
        return []


def save_commands(commands: list):
    """Save remaining commands back to the queue."""
    COMMANDS_FILE.write_text(
        json.dumps(commands, indent=2, default=str),
        encoding="utf-8"
    )


def save_result(result: dict):
    """Append a result to the results file."""
    results = []
    if RESULTS_FILE.exists():
        try:
            results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            results = []
    results.append(result)
    # Keep last 100 results
    results = results[-100:]
    RESULTS_FILE.write_text(
        json.dumps(results, indent=2, default=str),
        encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────────────
# SECURITY / APPROVAL
# ─────────────────────────────────────────────────────────────────────

def classify_command(cmd: str) -> int:
    """
    Classify a command into approval levels.
    Returns: 1 (auto), 2 (auto-safe), 3 (prompt), 4 (blocked)
    """
    cmd_lower = cmd.lower().strip()

    # Level 4: Blocked
    for blocked in BLOCKED_COMMANDS:
        if blocked in cmd_lower:
            return 4

    # Level 3: Needs confirmation
    for prompt_cmd in PROMPT_COMMANDS:
        if prompt_cmd in cmd_lower:
            return 3

    # Level 1: Always safe
    for safe_cmd in AUTO_APPROVE_COMMANDS:
        if cmd_lower.startswith(safe_cmd):
            return 1

    # Level 2: Safe if in approved directories
    return 2


def is_in_safe_dir(working_dir: Optional[str]) -> bool:
    """Check if the working directory is within approved paths."""
    if not working_dir:
        return True  # Default dir is fine
    wd = Path(working_dir).resolve()
    for safe_dir in SAFE_DIRS:
        try:
            if wd == safe_dir.resolve() or safe_dir.resolve() in wd.resolve().parents:
                return True
        except Exception:
            continue
    return False


def get_user_approval(cmd: str) -> bool:
    """Ask the user for approval on Level 3 commands."""
    print("\n" + "=" * 60)
    print("⚠️  APPROVAL REQUIRED")
    print("=" * 60)
    print(f"Command: {cmd}")
    print("=" * 60)
    response = input("Allow this command? [y/N]: ").strip().lower()
    return response in ("y", "yes")


# ─────────────────────────────────────────────────────────────────────
# COMMAND EXECUTION
# ─────────────────────────────────────────────────────────────────────

def execute_command(command: dict) -> dict:
    """
    Execute a command and return the result.

    Command format:
    {
        "id": "unique-id",
        "type": "shell" | "python" | "builtin",
        "cmd": "the command string",
        "working_dir": "optional/path",
        "timeout": 60
    }
    """
    cmd_id = command.get("id", str(uuid.uuid4()))
    cmd_type = command.get("type", "shell")
    cmd_str = command.get("cmd", "")
    working_dir = command.get("working_dir")
    timeout = command.get("timeout", 60)

    result = {
        "id": cmd_id,
        "command": cmd_str,
        "type": cmd_type,
        "timestamp": datetime.now().isoformat(),
        "status": "pending",
        "output": "",
        "error": "",
    }

    # Security check
    level = classify_command(cmd_str)

    if level == 4:
        result["status"] = "blocked"
        result["error"] = "Command blocked by security policy."
        logger.warning(f"BLOCKED: {cmd_str}")
        return result

    if level == 3:
        if not get_user_approval(cmd_str):
            result["status"] = "denied"
            result["error"] = "User denied approval."
            logger.info(f"DENIED by user: {cmd_str}")
            return result

    if level == 2 and not is_in_safe_dir(working_dir):
        # Elevate to Level 3 if outside safe dirs
        if not get_user_approval(f"{cmd_str} (outside safe directories)"):
            result["status"] = "denied"
            result["error"] = "User denied — command targets unsafe directory."
            logger.info(f"DENIED (unsafe dir): {cmd_str}")
            return result

    # Execute
    try:
        logger.info(f"EXECUTING [{cmd_type}]: {cmd_str}")

        if cmd_type == "builtin":
            # Handle built-in agent commands
            output = handle_builtin(cmd_str)
            result["status"] = "success"
            result["output"] = output

        elif cmd_type == "python":
            # Execute Python code
            proc = subprocess.run(
                [sys.executable, "-c", cmd_str],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir,
            )
            result["status"] = "success" if proc.returncode == 0 else "error"
            result["output"] = proc.stdout
            result["error"] = proc.stderr

        else:  # shell
            # Execute shell command via PowerShell
            proc = subprocess.run(
                ["powershell", "-Command", cmd_str],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir,
            )
            result["status"] = "success" if proc.returncode == 0 else "error"
            result["output"] = proc.stdout
            result["error"] = proc.stderr

        logger.info(f"COMPLETED [{result['status']}]: {cmd_str[:80]}")

    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["error"] = f"Command timed out after {timeout}s"
        logger.error(f"TIMEOUT: {cmd_str}")

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        logger.error(f"ERROR: {cmd_str} — {e}")

    return result


def handle_builtin(cmd: str) -> str:
    """Handle built-in agent commands."""
    cmd_lower = cmd.lower().strip()

    if cmd_lower == "health":
        return json.dumps({
            "agent": "operational",
            "timestamp": datetime.now().isoformat(),
            "platform": sys.platform,
            "python": sys.version,
            "agent_dir": str(AGENT_DIR),
        })

    elif cmd_lower == "status":
        results = []
        if RESULTS_FILE.exists():
            try:
                results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return json.dumps({
            "pending_commands": len(load_commands()),
            "completed_results": len(results),
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
        return f"Unknown builtin command: {cmd}"


# ─────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────

def main():
    """Main agent loop — poll for commands, execute, save results."""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║           KRLX REMOTE AGENT v1.0                        ║
    ║           5% - 90% - 5% Autonomy Model                  ║
    ╠══════════════════════════════════════════════════════════╣
    ║  Status: ACTIVE                                         ║
    ║  Queue:  ~/Desktop/KRLX_Agent/commands.json             ║
    ║  Results: ~/Desktop/KRLX_Agent/results.json             ║
    ║  Log:    ~/Desktop/KRLX_Agent/agent.log                 ║
    ╠══════════════════════════════════════════════════════════╣
    ║  Level 1 (Auto):   reads, status, health checks         ║
    ║  Level 2 (Auto):   scripts in KRLX dirs, git ops        ║
    ║  Level 3 (Prompt): system changes, new services         ║
    ║  Level 4 (Block):  destructive operations               ║
    ╠══════════════════════════════════════════════════════════╣
    ║  Press Ctrl+C to stop                                   ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    logger.info("KRLX Agent started. Polling for commands...")

    # Initialize empty queue if needed
    if not COMMANDS_FILE.exists():
        save_commands([])

    while True:
        try:
            commands = load_commands()

            if commands:
                # Take the first command
                command = commands.pop(0)
                save_commands(commands)

                # Execute it
                result = execute_command(command)

                # Save result
                save_result(result)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Agent stopped by user (Ctrl+C).")
            print("\nAgent stopped. Goodbye.")
            break

        except Exception as e:
            logger.error(f"Agent loop error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
