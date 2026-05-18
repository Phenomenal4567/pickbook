ALLOWED_LICENSES = [
    "Public Domain",
    "CC BY",
    "CC BY-SA",
    "CC0",
    "CC BY-NC",
]

# Use a frozenset for O(1) lookups and immutability.
_ALLOWED_SET = frozenset(ALLOWED_LICENSES)


def audit_license(license_name: str) -> dict:
    """
    Returns approval status for a given license string.

    Bug fix: original used a mutable list for membership checks (O(n)).
    Input is now type-checked and stripped to prevent whitespace bypass.
    """
    if not isinstance(license_name, str):
        return {
            "license": str(license_name),
            "approved": False,
            "reason": "Invalid input type",
        }

    cleaned = license_name.strip()
    approved = cleaned in _ALLOWED_SET

    return {
        "license": cleaned,
        "approved": approved,
        "reason": "Valid license" if approved else "Blocked or unknown license",
    }
