from __future__ import annotations

import sys
from pathlib import Path


DROP_SOURCES = {"moboreader"}


def compact_books_dump(input_path: Path, output_path: Path) -> dict:
    kept = 0
    dropped = 0
    compacted = 0
    in_books_copy = False

    with input_path.open("r", encoding="utf-8", errors="replace") as source:
        with output_path.open("w", encoding="utf-8", newline="") as target:
            for line in source:
                if line.startswith("COPY public.books "):
                    in_books_copy = True
                    target.write(line)
                    continue

                if in_books_copy and line == "\\.\n":
                    in_books_copy = False
                    target.write(line)
                    continue

                if not in_books_copy:
                    target.write(line)
                    continue

                columns = line.rstrip("\n").split("\t")
                if len(columns) < 11:
                    target.write(line)
                    kept += 1
                    continue

                source_name = columns[1].strip().lower()
                if source_name in DROP_SOURCES:
                    dropped += 1
                    continue

                if columns[10] != r"\N":
                    columns[10] = r"\N"
                    compacted += 1

                target.write("\t".join(columns) + "\n")
                kept += 1

    return {
        "kept": kept,
        "dropped": dropped,
        "compacted": compacted,
        "input_bytes": input_path.stat().st_size,
        "output_bytes": output_path.stat().st_size,
    }


def main() -> None:
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("books.sql")
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("books.compact.sql")
    stats = compact_books_dump(input_path, output_path)
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
