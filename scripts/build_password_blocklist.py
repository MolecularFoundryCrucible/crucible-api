"""Build app/auth/data/blocked_passwords.txt, the common passwords refused at password changes.

Source: SecLists (MIT license), Passwords/Common-Credentials/Pwdb_top-1000000.txt at a pinned
commit, kept to entries of 12 code points or more (CRUCIBLE_AUTH_PASSWORD_MIN_LENGTH can go down to
12), NFC-normalized and casefolded, plus predictable long phrases. Rerun after changing the commit:

    uv run python -m scripts.build_password_blocklist
"""
import unicodedata
import urllib.request
from pathlib import Path

COMMIT = "2f31b22e1f9963ec2fd2b12606c00f0fb04910f1"
SOURCE = f"https://raw.githubusercontent.com/danielmiessler/SecLists/{COMMIT}/Passwords/Common-Credentials/Pwdb_top-1000000.txt"
SHORTEST = 12
PHRASES = (
    "correct horse battery staple", "correcthorsebatterystaple", "passwordpassword", "password password",
    "123456789012", "1234567890123456", "qwertyuiopasdfghjkl", "letmeinletmein", "iloveyouiloveyou",
    "crucible password", "cruciblepassword", "molecular foundry", "molecularfoundry", "berkeley lab", "berkeleylab",
)
TARGET = Path(__file__).resolve().parents[1] / "app" / "auth" / "data" / "blocked_passwords.txt"


def normalized(word: str) -> str:
    return unicodedata.normalize("NFC", word).casefold()


def main() -> None:
    with urllib.request.urlopen(SOURCE, timeout=60) as response:
        lines = response.read().decode("utf-8", errors="replace").splitlines()
    words = {normalized(line) for line in lines} | {normalized(phrase) for phrase in PHRASES}
    kept = sorted(word for word in words if len(word) >= SHORTEST and word.isprintable() and "�" not in word)
    header = [
        "# Common passwords refused by Crucible, built by scripts/build_password_blocklist.py.",
        f"# Source: SecLists, MIT license, {SOURCE}",
        f"# Kept: {SHORTEST} or more code points, NFC-normalized and casefolded, plus predictable phrases.",
    ]
    TARGET.write_text("\n".join(header + kept) + "\n", encoding="utf-8")
    print(f"{len(kept)} entries written to {TARGET}")


if __name__ == "__main__":
    main()
