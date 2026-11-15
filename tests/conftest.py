"""Shared fixtures: synthetic worlds for orchestrator-level tests."""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gps_sync.options import Options
from gps_sync.orchestrator import run
from gps_sync.transport import MarkedBlock, Transport, WriteBatchResult
from tests.assets import Assets, build_assets, require_environment
from tests.support import FakeTransport, check_corpus, clone, gpx_text, raf_json

FIXED_NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)


def track_text(seconds: int = 600, step: int = 10) -> str:
    """A north-bound track from 04:00:00Z for ``seconds`` seconds, one point every ``step``."""
    start = datetime(2026, 10, 26, 4, 0, 0, tzinfo=UTC)
    points = [
        (
            (start + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{42.0 + 0.00001 * t:.6f}",
            "77.000000",
            f"{1000 + t * 0.1:.1f}",
        )
        for t in range(0, seconds + 1, step)
    ]
    return gpx_text(points)


@dataclass
class World:
    """A temp media tree of dummy stills, a tracks tree and an isolated HOME."""

    root: Path
    records: dict[str, Mapping[str, object]] = field(default_factory=dict)

    @property
    def media(self) -> Path:
        return self.root / "media"

    @property
    def tracks(self) -> Path:
        return self.root / "tracks"

    @property
    def home(self) -> Path:
        return self.root / "home"

    def add(self, relative: str, record: Mapping[str, object] | None = None) -> str:
        path = self.media / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"dummy")
        self.records[str(path.resolve())] = record if record is not None else raf_json()
        return str(path.resolve())

    def put(self, relative: str, source: Path) -> Path:
        """Clone a real asset into the media tree (never touches the source)."""
        return clone(source, self.media / relative)

    def transport(self, **kwargs: object) -> FakeTransport:
        return FakeTransport(lambda p: self.records.get(p, {}), **kwargs)  # type: ignore[arg-type]

    def applying(self) -> FakeTransport:
        """A transport whose write reports every path in the argfile as applied."""

        def on_write(argfile: Path) -> WriteBatchResult:
            lines = argfile.read_text().splitlines()
            paths = [lines[i + 1] for i, line in enumerate(lines) if line == "-echo3"]
            return WriteBatchResult(
                tuple(MarkedBlock(p, ("1 image files updated",)) for p in paths),
                tuple(MarkedBlock(p, ()) for p in paths),
                0,
            )

        return self.transport(on_write=on_write)

    def options(self, **kwargs: object) -> Options:
        defaults: dict[str, object] = {"media": self.media, "tracks": self.tracks}
        defaults.update(kwargs)
        return Options(**defaults)  # type: ignore[arg-type]

    def review_root(self) -> Path:
        [root] = list((self.home / "gps-sync").iterdir())
        return root

    def run(
        self,
        options: Options | None = None,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        stdout: io.StringIO | None = None,
        **kwargs: object,
    ) -> RunResult:
        out, err = stdout or io.StringIO(), io.StringIO()
        code = run(
            options or self.options(),
            transport=transport or self.transport(),
            clock=clock or (lambda: FIXED_NOW),
            stdout=out,
            stderr=err,
            home=self.home,
            **kwargs,  # type: ignore[arg-type]
        )
        return RunResult(code, out.getvalue(), err.getvalue())


@dataclass
class RunResult:
    code: int
    out: str
    err: str


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.media.mkdir()
    w.tracks.mkdir()
    (w.tracks / "walk.gpx").write_text(track_text())
    w.home.mkdir()
    return w


@pytest.fixture(scope="session")
def assets(tmp_path_factory: pytest.TempPathFactory) -> Assets:
    """Real-container assets, built once per session for every package that asks for them."""
    require_environment()
    check_corpus()
    return build_assets(tmp_path_factory.mktemp("assets"))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark tests by the directory that holds them so the levels run separately."""
    for item in items:
        path = str(item.fspath)
        if "/tests/integration/" in path:
            item.add_marker(pytest.mark.integration)
        elif "/tests/e2e/" in path:
            # An end-to-end test is also an integration test, so `-m "not integration"` is the
            # unit pass and no selector has to remember to exclude e2e separately.
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.e2e)
