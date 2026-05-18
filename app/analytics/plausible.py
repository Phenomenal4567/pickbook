import bleach


def plausible_script(domain: str) -> str:
    """
    Returns a Plausible analytics <script> tag.

    Bug fix: raw `domain` was interpolated directly into HTML, enabling XSS
    if the caller passed untrusted input. Domain is now sanitised first.
    """
    safe_domain = bleach.clean(domain, tags=[], strip=True)
    return (
        f'<script defer data-domain="{safe_domain}" '
        f'src="https://plausible.io/js/script.js"></script>'
    )
