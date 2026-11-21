import re


def normalize(name: str) -> str:
    """The key AnyList names are matched on: case- and whitespace-insensitive."""
    return re.sub(r"\s+", " ", name).strip().casefold()
