import argparse
import sys
import time
from pathlib import Path

from requests.exceptions import RequestException

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal
from app.ingest.routes import _parse_as_details, fetch
from app.models.book import Book


def repair_anystories_covers(limit: int | None, dry_run: bool) -> None:
    updated = 0
    checked = 0

    with SessionLocal() as db:
        query = (
            db.query(Book)
            .filter(Book.source == "anystories")
            .order_by(Book.id.desc())
        )

        if limit:
            query = query.limit(limit)

        books = query.all()

        for book in books:
            checked += 1

            if not book.download:
                continue

            try:
                html = fetch(book.download)
                details = _parse_as_details(
                    html,
                    book.download,
                    max_public_chapters=0,
                )
            except RequestException as exc:
                print(f"[skip] {book.id} {book.title}: {exc}")
                continue

            new_cover = details.get("cover")

            if not new_cover or new_cover == book.cover:
                continue

            print(
                f"[cover] {book.id} {book.title}: "
                f"{book.cover or 'EMPTY'} -> {new_cover}"
            )

            book.cover = new_cover
            db.add(book)
            updated += 1

            if not dry_run:
                db.commit()

            time.sleep(0.25)

        if dry_run:
            db.rollback()

    print(f"checked={checked} updated={updated} dry_run={dry_run}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repair_anystories_covers(
        limit=args.limit,
        dry_run=args.dry_run,
    )
