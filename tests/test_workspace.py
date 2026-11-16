import json
from pathlib import Path
import unittest


class WorkspaceRouteTests(unittest.TestCase):
    def test_workspace_does_not_shadow_any_doctype_route(self):
        module = Path(__file__).parents[1] / "monthly_attendance_import/monthly_attendance_import"
        doctype_routes = {p.stem.replace("_", "-") for p in (module / "doctype").glob("*/*.json")}
        for path in (module / "workspace").glob("*/*.json"):
            workspace = json.loads(path.read_text())
            route = workspace["title"].lower().replace(" ", "-")
            self.assertNotIn(route, doctype_routes)
            self.assertIn("Monthly Attendance Import", [s["link_to"] for s in workspace["shortcuts"]])
