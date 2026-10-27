"""Standalone test fixtures; no Avalokita source is distributed."""

from pathlib import Path
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def canonical_temporary_root():
    # System temp aliases (/var on macOS, Windows 8.3 names) are not test links.
    original = tempfile.tempdir
    tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())
    try:
        yield
    finally:
        tempfile.tempdir = original


@pytest.fixture(autouse=True)
def standalone_foreign_import_namespace(request):
    if (request.node.path.name != "test_sidecar_registry.py"
            or getattr(request.node, "originalname", "")
            != "test_actual_enable_and_product_import_orders_do_not_collide"):
        return
    temporary = request.getfixturevalue("tmp_path")
    monkeypatch = request.getfixturevalue("monkeypatch")
    foreign = temporary / "synthetic-foreign-app"
    (foreign / "adapters").mkdir(parents=True)
    for relative in ("server.py", "mcp_server.py", "adapters/__init__.py"):
        (foreign / relative).write_text(
            '"""Synthetic foreign import namespace; not a real app or native delivery."""\n',
            encoding="utf-8",
        )
    monkeypatch.setattr(request.module, "PRODUCT", foreign)
