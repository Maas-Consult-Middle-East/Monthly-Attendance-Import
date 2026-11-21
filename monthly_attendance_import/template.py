"""Serve the committed blank workbook with the selected period, without formulas."""
from calendar import month_abbr
from io import BytesIO
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET


def make_template(year, month, worksheet=None):
    if not 1900 <= year <= 2200 or not 1 <= month <= 12:
        raise ValueError("Invalid month/year")
    if worksheet and (len(worksheet) > 31 or re.search(r"[\\/*?:\[\]]", worksheet)):
        raise ValueError("Invalid Excel worksheet name")
    source = Path(__file__).parent / "public/templates/monthly-attendance-template.xlsx"
    output = BytesIO()
    # Only change the declared period in the packaged artifact. No workbook engine
    # or formula calculation is required on the web server.
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with ZipFile(source) as original, ZipFile(output, "w", ZIP_DEFLATED) as result:
        for item in original.infolist():
            content = original.read(item.filename)
            if item.filename == "xl/workbook.xml" and worksheet:
                tree = ET.fromstring(content)
                tree.find(f".//{{{ns}}}sheet").set("name", worksheet)
                content = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
            if item.filename == "xl/worksheets/sheet1.xml":
                tree = ET.fromstring(content)
                cell = tree.find(f".//{{{ns}}}c[@r='M3']")
                if cell is None:
                    raise ValueError("Template is missing period cell M3")
                cell.clear()
                cell.set("r", "M3")
                cell.set("t", "inlineStr")
                inline = ET.SubElement(cell, f"{{{ns}}}is")
                ET.SubElement(inline, f"{{{ns}}}t").text = f"MONTH OF -{month_abbr[month].upper()}- {year}"
                content = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
            result.writestr(item, content)
    return output.getvalue()
