"""Фиксированный ASCII-пример: генератор не принимает пользовательский текст."""

from pathlib import Path


def make_demo_pdf(path: Path) -> None:
    texts = [
        "Alpha-7 equipment manual. Cover page.",
        "Warranty period: 18 months. Store below 45 C.",
        "Warranty is void if the case is opened. Support: Mon-Fri 09:00-18:00.",
    ]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R 6 0 R 8 0 R] /Count 3 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, text in enumerate(texts):
        content_id = 5 + index * 2
        objects.append((
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 900 400] "
            "/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii"))
        content = f"BT /F1 14 Tf 40 320 Td ({text}) Tj ET".encode("ascii")
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
            + content + b"\nendstream"
        )
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend((
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output)


if __name__ == "__main__":
    project = Path(__file__).resolve().parents[1]
    make_demo_pdf(project / "data" / "demo-manual.pdf")
