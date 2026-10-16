
import getpass
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SESSION_PATH = REPO_ROOT / "data" / "ig_session.json"


def _load_dotenv():
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except Exception:
        pass


def _prompt(label: str, secret: bool = False) -> str:
    if secret:
        try:
            return getpass.getpass(f"{label}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
    try:
        return input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)


def main() -> int:
    _load_dotenv()

    try:
        from instagrapi import Client
        from instagrapi.exceptions import (
            BadPassword,
            ChallengeRequired,
            TwoFactorRequired,
        )
    except ImportError:
        print("instagrapi is not installed. Run: venv/bin/pip install instagrapi")
        return 1

    username = os.getenv("IG_LOGIN_USERNAME") or _prompt("Instagram username")
    password = os.getenv("IG_LOGIN_PASSWORD") or _prompt("Instagram password", secret=True)
    two_factor = os.getenv("IG_LOGIN_2FA") or ""

    if not username or not password:
        print("Username and password are required.")
        return 1

    if SESSION_PATH.exists():
        print(f"An existing session is present at {SESSION_PATH}.")
        answer = _prompt("Overwrite it with a fresh login? [y/N]").lower()
        if answer not in ("y", "yes"):
            print("Keeping existing session. Nothing to do.")
            return 0

    cl = Client()
    cl.set_device({
        "android_version": 33,
        "android_release": "13",
        "dpi": "440dpi",
        "resolution": "1080x2400",
        "manufacturer": "Xiaomi",
        "device": "star",
        "model": "M2102K1C",
        "cpu": "qcom",
    })
    cl.delay_range = [1, 3]

    cl.challenge_code_handler = lambda _u, _c=None: (
        two_factor or _prompt("Instagram 2FA / security code (6 digits)")
    )

    print(f"Logging in as {username} from a simulated Android device...")
    try:
        cl.login(username, password, verification_code=two_factor or "")
    except TwoFactorRequired:
        code = _prompt("Instagram 2FA code (6 digits)")
        cl.login(username, password, verification_code=code)
    except BadPassword:
        print("Login failed: wrong password.")
        return 2
    except ChallengeRequired as exc:
        print("Login hit a security challenge (checkpoint).")
        print("Open Instagram on your phone, approve the login attempt, then retry.")
        print(f"Detail: {exc}")
        return 3
    except Exception as exc:
        print(f"Login failed: {type(exc).__name__}: {exc}")
        return 4

    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    cl.dump_settings(str(SESSION_PATH))
    try:
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass

    who = None
    try:
        who = cl.account_info()
    except Exception:
        pass

    print()
    print("=" * 60)
    print("Session saved:", SESSION_PATH)
    if who is not None:
        print(f"Logged in as: @{who.username} (pk={who.pk})")
    print("device_id/phone_id/uuid + mobile cookies are inside the JSON.")
    print("The bot reuses this file; it never calls login() again.")
    print("=" * 60)

    probe = os.getenv("IG_LOGIN_PROBE_SHORTCODE", "").strip()
    if probe:
        print(f"\nProbing media/{probe} via the private mobile API...")
        try:
            pk = cl.media_pk_from_code(probe)
            info = cl.media_info(pk)
            print(f"  media_pk={pk} type={info.media_type} "
                  f"video={'yes' if info.video_url else 'no'}")
        except Exception as exc:
            print(f"  probe failed: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
