"""Generate an owner Argon2id hash locally; never accepts a password as an argument."""

from getpass import getpass

from argon2 import PasswordHasher


def main():
    password = getpass("Owner password (at least 12 characters): ")
    if len(password) < 12 or password != getpass("Repeat password: "):
        raise SystemExit("Passwords must match and contain at least 12 characters")
    print(PasswordHasher().hash(password))


if __name__ == "__main__":
    main()
