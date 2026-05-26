import re
from urllib.parse import urlparse

from app.core.database import SessionLocal
from app.models.book import Book


BAD_PREFIX = "List of "
BAD_SUFFIX = " Novels to Read Online"


def title_from_url(url: str) -> str | None:
    parts = [
        part
        for part in urlparse(url or "").path.split("/")
        if part
    ]

    if "book" not in parts:
        return None

    index = parts.index("book")

    if index + 1 >= len(parts):
        return None

    slug = parts[index + 1]
    words = [
        word
        for word in slug.replace("-", " ").split()
        if word
    ]

    if not words:
        return None

    title = " ".join(words).title()
    title = re.sub(r"\bS\b", "'s", title)
    title = re.sub(r"\s+'s\b", "'s", title)
    title = re.sub(r"\bCeo\b", "CEO", title)
    title = re.sub(r"\bMr\b", "Mr.", title)

    return title


def is_bad_title(title: str | None) -> bool:
    title = title or ""
    return title.startswith(BAD_PREFIX) and title.endswith(BAD_SUFFIX)


def main() -> None:
    db = SessionLocal()
    repaired = 0

    try:
        books = (
            db.query(Book)
            .filter(Book.source == "anystories")
            .all()
        )

        for book in books:
            changed = False

            title = title_from_url(book.download)

            if title and (
                is_bad_title(book.title)
                or " S " in (book.title or "")
                or (book.title or "").endswith(" S")
                or " 's" in (book.title or "")
            ):
                book.title = title[:255]
                changed = True

            if (
                book.genre == "Paranormal"
                and "/werewolf_" in (book.download or "")
            ):
                book.genre = "Werewolf"
                changed = True

            if changed:
                repaired += 1

        db.commit()
    finally:
        db.close()

    print(f"repaired={repaired}")


if __name__ == "__main__":
    main()
