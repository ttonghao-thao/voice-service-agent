"""Generate a password hash for LOCAL_USERS_JSON without printing the password."""

from getpass import getpass

from app.api.auth import hash_password


def main() -> None:
    password = getpass("Test account password: ")
    if len(password) < 16:
        raise SystemExit("Use at least 16 characters for a restricted test account")
    print(hash_password(password))


if __name__ == "__main__":
    main()
