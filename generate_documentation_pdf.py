"""Generate a simple PDF from the project technical documentation using only Python's standard library."""

from __future__ import annotations

import textwrap
from pathlib import Path


SOURCE = Path("docs/TECHNICAL_USER_DOCUMENTATION.md")
OUTPUT = Path("docs/FHIR_Retriever_Documentation.pdf")
PAGE_WIDTH, PAGE_HEIGHT = 595, 842
LEFT, TOP, LINE_HEIGHT = 42, 800, 12


def escape_pdf(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def page_stream(lines: list[str]) -> bytes:
    commands = ["BT", "/F1 9 Tf", f"{LEFT} {TOP} Td"]
    for index, line in enumerate(lines):
        if index:
            commands.append(f"0 -{LINE_HEIGHT} Td")
        commands.append(f"({escape_pdf(line)}) Tj")
    commands.append("ET")
    return "\n".join(commands).encode("latin-1", "replace")


def build_pdf(pages: list[list[str]]) -> bytes:
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    page_object_numbers = []
    font_number = 3 + len(pages) * 2
    for page in pages:
        stream = page_stream(page)
        content_number = len(objects) + 1
        page_number = content_number + 1
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font_number, content_number))
        page_object_numbers.append(page_number)
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    objects[1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{number} 0 R" for number in page_object_numbers), len(page_object_numbers))).encode()
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, object_data in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode())
        payload.extend(object_data)
        payload.extend(b"\nendobj\n")
    cross_reference = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{cross_reference}\n%%EOF\n".encode())
    return bytes(payload)


def main() -> None:
    lines = []
    for source_line in SOURCE.read_text(encoding="utf-8").splitlines():
        for line in textwrap.wrap(source_line or " ", width=88, replace_whitespace=False) or [" "]:
            lines.append(line)
    page_length = 62
    pages = [lines[index : index + page_length] for index in range(0, len(lines), page_length)]
    OUTPUT.write_bytes(build_pdf(pages))
    print(OUTPUT)


if __name__ == "__main__":
    main()