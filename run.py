"""Single-command launcher for Regulation Radar.

    python run.py

Ensures dependencies are installed (convenience — installs from requirements.txt on a
cold machine), loads .env if present, then starts the FastAPI server which seeds the DB
from a live eCFR fetch and serves both the API and the dashboard at http://127.0.0.1:8000
"""
import importlib.util
import subprocess
import sys
import os

REQUIRED = ["fastapi", "uvicorn", "anthropic", "langgraph", "pydantic", "dotenv"]


def ensure_deps():
    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if missing:
        print(f"[run] installing missing deps: {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"]
        )


def main():
    ensure_deps()

    # Load .env if present (optional — app runs in stub mode without it).
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    if os.environ.get("ANTHROPIC_API_KEY"):
        mode = "Claude (" + os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6") + ")"
    elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        mode = "Gemini (" + os.environ.get("GEMINI_MODEL", "gemini-2.5-flash") + ")"
    else:
        mode = "STUB (no LLM key set)"
    print(f"[run] agent decide-step mode: {mode}")
    print("[run] open http://127.0.0.1:8000")

    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
