"""Render the triage test documents into evals/triage/docs/.

python -m evals.triage.build
"""

import csv
from pathlib import Path

from evals.triage.cases import CASES, Doc

DOCS = Path(__file__).parent / "docs"


def _pdf(doc: Doc, path: Path) -> None:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    s = doc.spec
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    story = [Paragraph(f"<b>{escape(s['title'])}</b>", styles["Title"])]

    left = [Paragraph(f"<b>{escape(s['from'][0])}</b>", body)]
    left += [Paragraph(escape(x), body) for x in s["from"][1:]]
    right = [Paragraph(f"{escape(k)}: <b>{escape(v)}</b>", body) for k, v in s["meta"]]
    story.append(Table([[left, right]], colWidths=[3.6 * inch, 3.0 * inch]))
    story.append(Spacer(1, 12))
    if label := s.get("to_label", "Bill to"):
        story.append(Paragraph(f"<b>{label}</b>", body))
    story.extend(Paragraph(escape(x), body) for x in s["to"])
    story.append(Spacer(1, 12))

    table = Table([s["columns"], *s["rows"]], colWidths=[4.2 * inch, 0.8 * inch, 1.6 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8ECEF")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story.append(table)
    if s["totals"]:
        story.append(Spacer(1, 8))
        totals = Table(s["totals"], colWidths=[5.0 * inch, 1.6 * inch])
        totals.setStyle(
            TableStyle(
                [
                    ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ]
            )
        )
        story.append(totals)
    for note in s["notes"]:
        story.append(Spacer(1, 10))
        story.append(Paragraph(escape(note), body))

    SimpleDocTemplate(str(path), pagesize=LETTER, invariant=1, title=s["title"]).build(story)


def _png(doc: Doc, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    lines = doc.spec["lines"]
    font = ImageFont.load_default(size=22)
    img = Image.new("RGB", (460, 40 + 34 * len(lines)), "#FBFAF6")
    draw = ImageDraw.Draw(img)
    for i, text in enumerate(lines):
        draw.text((30, 20 + 34 * i), text, fill="#222222", font=font)
    img = img.rotate(1.5, expand=True, fillcolor="#D9D6CE")  # a slightly crooked photo
    img.save(path, optimize=True)


def _xlsx(doc: Doc, path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = doc.spec["sheet"]
    ws.append(doc.spec["columns"])
    for row in doc.spec["rows"]:
        ws.append(row)
    wb.properties.creator = "Business Central"
    wb.save(path)


def _csv(doc: Doc, path: Path) -> None:
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(doc.spec["rows"])


RENDERERS = {"pdf": _pdf, "png": _png, "xlsx": _xlsx, "csv": _csv}


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    seen: set[str] = set()
    for case in CASES:
        for doc in case.attachments:
            if doc.filename in seen:
                continue
            seen.add(doc.filename)
            RENDERERS[doc.kind](doc, DOCS / doc.filename)
    print(f"Rendered {len(seen)} documents into {DOCS}")


if __name__ == "__main__":
    main()
