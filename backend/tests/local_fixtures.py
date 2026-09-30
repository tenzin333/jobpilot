"""Local-only browser integration fixtures. No model or employer requests."""
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import pytest
from playwright.sync_api import sync_playwright


@pytest.fixture(scope="module")
def local_ats_origin():
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(["node", str(frontend / "node_modules/vite/bin/vite.js"),
        "--config", "vite.test-ats.config.ts", "--host", "127.0.0.1", "--port", str(port)],
        cwd=frontend, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                with urlopen(origin + "/test-ats/basic", timeout=1):
                    break
            except OSError:
                if process.poll() is not None:
                    pytest.fail("Local fixture server exited")
                time.sleep(.1)
        else:
            pytest.fail("Local fixture did not start")
        yield origin
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def browser_page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


@pytest.fixture
def candidate(tmp_path):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="Synthetic candidate document")
    resume = tmp_path / "resume.pdf"
    pdf.output(str(resume))
    return dict(first_name="Alex", last_name="Candidate", email="alex@example.com", phone="+12025550123",
        resume=str(resume), work_authorization="Authorized to work", authorized="Yes", achievement="Built a local test tool.",
        interest="Local testing", linkedin_url="https://example.com/profile", work_mode="Remote", country="India",
        city="Pune", sponsorship="Yes", visa_type="Synthetic visa answer", privacy_acknowledged=True)
