"""Entry point for the Workflow C referral bot. Used by systemd on the VM."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.telegram_bot import run_bot

if __name__ == "__main__":
    run_bot()
