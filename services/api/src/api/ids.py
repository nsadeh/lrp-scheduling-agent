from nanoid import generate


def make_id(prefix: str) -> str:
    """Generate a prefixed NanoID. Prefix must be exactly 3 lowercase alpha chars."""
    assert len(prefix) == 3 and prefix.isalpha() and prefix.islower()
    return f"{prefix}_{generate()}"
