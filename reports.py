"""Excel + PDF report generation. All local, free (openpyxl, reportlab)."""
import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet


def _style_header(ws, row=1):
    for cell in ws[row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4C4CFF")
        cell.alignment = Alignment(horizontal="center")


def alumni_excel(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Alumni"
    headers = ["Name", "Email", "Phone", "Batch", "Department", "Company", "Designation", "City", "LinkedIn"]
    ws.append(headers)
    for a in rows:
        ws.append([a["name"], a["email"], a["phone"], a["batch_year"], a["department"],
                   a["current_company"], a["designation"], a["city"], a["linkedin"]])
    _style_header(ws)
    for col in ws.columns:
        width = max(len(str(c.value)) if c.value else 0 for c in col) + 3
        ws.column_dimensions[col[0].column_letter].width = min(width, 40)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def donations_excel(rows, total):
    wb = Workbook()
    ws = wb.active
    ws.title = "Donations"
    ws.append(["Alumni", "Amount", "Purpose", "Date"])
    for d in rows:
        ws.append([d["alumni_name"], d["amount"], d["purpose"], d["donation_date"]])
    ws.append([])
    ws.append(["", "Total", "", total])
    _style_header(ws)
    for col in ws.columns:
        width = max(len(str(c.value)) if c.value else 0 for c in col) + 3
        ws.column_dimensions[col[0].column_letter].width = min(width, 40)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def alumni_pdf(rows):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=1.5 * cm)
    styles = getSampleStyleSheet()
    elements = [Paragraph("Alumni Directory Report", styles["Title"]), Spacer(1, 12)]

    data = [["Name", "Email", "Batch", "Dept", "Company", "City"]]
    for a in rows:
        data.append([a["name"], a["email"], str(a["batch_year"] or "-"), a["department"] or "-",
                     a["current_company"] or "-", a["city"] or "-"])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4C4CFF")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(table)
    doc.build(elements)
    buf.seek(0)
    return buf


def donations_pdf(rows, total):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=1.5 * cm)
    styles = getSampleStyleSheet()
    elements = [Paragraph("Donations Report", styles["Title"]),
                Paragraph(f"Total collected: Rs. {total:,.2f}", styles["Heading3"]), Spacer(1, 12)]

    data = [["Alumni", "Amount", "Purpose", "Date"]]
    for d in rows:
        data.append([d["alumni_name"], f"Rs. {d['amount']:,.2f}", d["purpose"] or "-", d["donation_date"]])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4C4CFF")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
    ]))
    elements.append(table)
    doc.build(elements)
    buf.seek(0)
    return buf
