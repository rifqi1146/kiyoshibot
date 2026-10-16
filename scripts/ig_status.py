
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SESSION_PATH = REPO_ROOT / "data" / "ig_session.json"


def main() -> int:
    if not SESSION_PATH.exists():
        print(f"No session file found at {SESSION_PATH}.")
        print("Run: venv/bin/python scripts/ig_login.py")
        return 1

    try:
        from instagrapi import Client
    except ImportError:
        print("instagrapi is not installed. Run: venv/bin/pip install instagrapi")
        return 1

    cl = Client()
    try:
        cl.load_settings(str(SESSION_PATH))
    except Exception as exc:
        print(f"Failed to parse {SESSION_PATH}: {exc}")
        return 2

    settings = cl.get_settings()
    auth_user = settings.get("authorization_data", {}).get("ds_user_id") or ""
    cookies = settings.get("cookies", {})
    sessionid = cookies.get("sessionid", "")
    has_session = bool(sessionid)

    print("=" * 60)
    print("Instagram Stored Session Status")
    print("=" * 60)
    print(f"Session file: {SESSION_PATH}")
    print(f"User ID:      {auth_user or cookies.get('ds_user_id', '<unknown>')}")
    print(f"Device UUID:  {settings.get('uuids', {}).get('android_device_id', '<unknown>')}")
    print(f"Has session:  {'yes' if has_session else 'no'}")

    print("\nPinging Instagram Private Mobile API...")
    try:
        info = cl.account_info()
        print(f"✓ Session is ACTIVE! Logged in as: @{info.username} (name: {info.full_name})")
    except Exception as exc:
        print(f"✗ Session check failed: {type(exc).__name__}: {exc}")
        print("The session may have expired or triggered a checkpoint.")
        print("Re-run: venv/bin/python scripts/ig_login.py")
        return 3

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        shortcode = args[0]
        print(f"\nProbing post '{shortcode}' via private mobile API...")
        try:
            pk = cl.media_pk_from_code(shortcode)
            media = cl.media_info(pk)
            print("✓ Post unlocked successfully!")
            print(f"  Media PK:   {pk}")
            print(f"  Type:       {media.media_type}")
            print(f"  Caption:    {(media.caption_text or '')[:80]}...")
            print(f"  Video URL:  {'found (unlock OK)' if media.video_url else 'none'}")
            if media.video_url:
                print(f"  URL sample: {str(media.video_url)[:70]}...")
        except Exception as exc:
            print(f"✗ Failed to fetch post: {type(exc).__name__}: {exc}")
            return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
