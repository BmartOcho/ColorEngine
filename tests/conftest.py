import json
import os
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", message=".*related API features are not available")

from proofing_engine.pipeline import analyze, run  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
BRIGHTSPOT = Path(os.environ.get("BRIGHTSPOT_PDF", FIXTURES / "brightspot_badges_64.pdf"))


class Job:
    def __init__(self, path: Path):
        self.path = path
        self.job = json.loads((path / "job.json").read_text())
        self.inventory = json.loads((path / "inventory.json").read_text())
        self.findings = json.loads((path / "findings.json").read_text())
        self.report = json.loads((path / "report.json").read_text())

    def rule(self, rule_id):
        return [f for f in self.findings if f["rule"] == rule_id]

    def page_cmyk(self, page):
        return {tuple(r["cmyk"]): r for r in self.inventory if r["page"] == page}


def run_job(pdf, tmp, **kw) -> Job:
    return Job(analyze([pdf], tmp, **kw))


@pytest.fixture(scope="session")
def brightspot_pdf():
    if not BRIGHTSPOT.exists():
        pytest.skip(f"Brightspot fixture not present at {BRIGHTSPOT} (customer file, kept out of git; "
                    "set BRIGHTSPOT_PDF or copy it there)")
    return BRIGHTSPOT


@pytest.fixture(scope="session")
def brightspot(brightspot_pdf, tmp_path_factory):
    return run_job(brightspot_pdf, tmp_path_factory.mktemp("jobs"))


@pytest.fixture(scope="session")
def brightspot_swop(brightspot_pdf, tmp_path_factory):
    return run_job(brightspot_pdf, tmp_path_factory.mktemp("jobs_swop"), profile="swop2013")


def run_full(pdf, tmp, **kw) -> Job:
    return Job(run([pdf], tmp, **kw))


@pytest.fixture(scope="session")
def brightspot_run(brightspot_pdf, tmp_path_factory):
    return run_full(brightspot_pdf, tmp_path_factory.mktemp("runs"), job_id="run")
