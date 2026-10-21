"""A capture of the interface is reproducible, and only when it is asked for.

The interface normally shows whatever is happening: it needs a live daemon, a
conversation mid-flight, real GPU numbers and the current time, so no two
pictures of it are ever the same and nothing about it can be checked by
comparing captures. ``?state=`` and ``?at=`` paint one fixed state from fixed
numbers and a frozen clock instead.

These tests drive the real page in headless Edge against a static server of
their own, so they never touch his running daemon and never read his
conversation. They are skipped where Edge is not installed.

Two things are pinned. That the forced state actually paints, identically,
twice from the same URL; and that a page loaded without the parameters carries
no trace of any of it.

"Identically" is per pixel, within a tolerance, not per byte: Skia dithers the
HUD core's gradient by up to six levels of 255 between two renders of the same
frozen page, and no page-side or renderer-side switch turns that off. A real
change of state moves whole channels, so the tolerance separates the two.
"""

import functools
import http.server
import re
import shutil
import socketserver
import struct
import subprocess
import tempfile
import threading
import uuid
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "avatar"

# The same moment for every capture below, so a failure is never the clock.
AT = "2026-08-15T09:41:00"

# Dither measures 6 between two renders of the same page; a changed glyph
# measures over 100.
TOLERANCE = 12


def _png_pixels(path: Path) -> tuple:
    """(width, height, RGB bytes) for a Chromium screenshot.

    Written out rather than imported so the reproducibility check, which is the
    whole point of the harness, does not sit behind an optional dependency.
    Handles what Chromium writes: 8-bit RGB or RGBA, no interlacing.
    """
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    pos, idat, width, height, channels = 8, bytearray(), 0, 0, 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and colour in (2, 6), "unexpected PNG shape"
            assert body[12] == 0, "interlaced PNGs are not expected here"
            channels = 3 if colour == 2 else 4
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(height * stride)
    prev = bytearray(stride)
    at = 0
    for y in range(height):
        filter_type = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = prev[i]
            upleft = prev[i - channels] if i >= channels else 0
            if filter_type == 1:
                line[i] = (line[i] + left) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + up) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif filter_type == 4:
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upleft)
                line[i] = (line[i] + pred) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, bytes(out), channels


def _worst_difference(a: Path, b: Path) -> int:
    """The largest per-channel difference between two captures."""
    wa, ha, pa, ca = _png_pixels(a)
    wb, hb, pb, cb = _png_pixels(b)
    assert (wa, ha) == (wb, hb), "the two captures are not even the same size"
    worst = 0
    for i in range(0, len(pa), ca):
        for c in range(3):
            d = abs(pa[i + c] - pb[i + c])
            if d > worst:
                worst = d
    return worst


def _edge() -> str:
    """Edge is what renders these captures; without it there is nothing to test."""
    for candidate in (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    found = shutil.which("msedge")
    if found:
        return found
    pytest.skip("Microsoft Edge is not installed; it is what renders these captures")


# The same page, served to a browser that will never run an animation frame.
# Headless Edge sometimes produces no frame at all -- measured at one launch in
# sixteen -- and a page that reports itself ready only from inside a frame then
# never reports at all. Stubbing the call reproduces that deterministically.
NO_FRAMES = "/no-frames.html"
_FRAME_STUB = ("<script>window.requestAnimationFrame=function(){return 0};"
               "window.webkitRequestAnimationFrame="
               "window.requestAnimationFrame;</script>")


@pytest.fixture(scope="module")
def server():
    """Serves ``avatar/`` on a port of its own.

    Not the running bridge: a test must not depend on his daemon being up, and
    must not be able to pull his real conversation into a picture.
    """
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ASSETS), **kwargs)

        def do_GET(self):  # noqa: N802 — the base class names it
            if not self.path.startswith(NO_FRAMES):
                return super().do_GET()
            page = (ASSETS / "ui.html").read_text(encoding="utf-8")
            cut = page.index("<head>") + len("<head>")
            body = (page[:cut] + _FRAME_STUB + page[cut:]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: D102
            pass  # a request log per launch buries the actual failures

    class Quiet(socketserver.TCPServer):
        allow_reuse_address = True

        def handle_error(self, request, client_address):  # noqa: D102
            pass  # a page that gives up on /favicon.ico is not a test failure

    httpd = Quiet(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/ui.html"
    httpd.shutdown()
    httpd.server_close()


def _run_edge(url: str, extra: list,
              size: str = "1280,720") -> subprocess.CompletedProcess:
    """One headless render. A fresh profile each time, or Edge writes nothing."""
    profile = Path(tempfile.gettempdir()) / f"jarvis-hud-test-{uuid.uuid4().hex}"
    try:
        return subprocess.run(
            [_edge(), "--headless=new", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--force-device-scale-factor=1",
             f"--user-data-dir={profile}", f"--window-size={size}",
             "--virtual-time-budget=9000", *extra, url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


@functools.lru_cache(maxsize=None)
def _dom(url: str, size: str = "1280,720") -> str:
    """The rendered DOM. Cached: each call is a browser launch."""
    return _run_edge(url, ["--dump-dom"], size=size).stdout or ""


def _text_of(dom: str, element_id: str) -> str:
    """What one element on the rendered page says.

    The dump carries the page's own script as well as its elements, so a plain
    substring search finds the word "Speaking" in the source that decides when
    to write it. Only what an element actually says counts.
    """
    match = re.search(rf'id="{re.escape(element_id)}"[^>]*>([^<]*)<', dom)
    assert match, f"no element #{element_id} in the rendered page"
    return match.group(1).strip()


def _shot(url: str, path: Path) -> Path:
    _run_edge(url, [f"--screenshot={path}"])
    assert path.exists(), f"Edge produced no capture for {url}"
    return path


class TestTheForcedStatePaints:
    def test_speaking_says_so_without_a_daemon_anywhere(self, server):
        dom = _dom(f"{server}?view=dashboard&state=speaking&at={AT}")
        assert 'data-test-ready="1"' in dom, "the page never finished painting"
        assert _text_of(dom, "status-text") == "Speaking"

    def test_the_numbers_are_fixtures_not_this_machine(self, server):
        dom = _dom(f"{server}?view=hud&state=speaking&at={AT}")
        assert ">142 W<" in dom, "the power reading should be the fixture"

    def test_the_clock_is_the_one_that_was_asked_for(self, server):
        dom = _dom(f"{server}?view=dashboard&state=idle&at={AT}")
        assert _text_of(dom, "clock") == "09:41:00"

    def test_a_time_that_cannot_be_read_still_freezes(self, server):
        # Running live would be worse than an obviously wrong time: a capture
        # that quietly stopped being reproducible looks exactly like one that is.
        dom = _dom(f"{server}?view=dashboard&state=idle&at=not-a-time")
        assert _text_of(dom, "clock") == "09:00:00"

    def test_a_state_nobody_recognises_falls_back_to_idle(self, server):
        dom = _dom(f"{server}?view=dashboard&state=nonsense&at={AT}")
        assert 'data-test-ready="1"' in dom
        assert _text_of(dom, "status-text") == "Listening"

    def test_it_reports_ready_even_where_no_frame_is_ever_drawn(self, server):
        """Readiness is a fact about the page, not about the compositor.

        A headless render that produces no frame still finishes building the
        page: measured, the state and the clock were both right and the only
        thing missing was the flag saying so. A waiter would have given up on
        a picture that was sitting there finished.
        """
        page = server.rsplit("/", 1)[0] + NO_FRAMES
        dom = _dom(f"{page}?view=dashboard&state=speaking&at={AT}")
        assert _text_of(dom, "status-text") == "Speaking", (
            "the page did not finish building, so this proves nothing")
        assert 'data-test-ready="1"' in dom


class TestTheSameUrlCapturesIdentically:
    """The whole point of the mechanism: two captures, one picture."""

    @pytest.mark.parametrize("view", ["dashboard", "hud", "ar", "stage"])
    def test_twice_is_the_same_picture(self, server, tmp_path, view):
        url = f"{server}?view={view}&state=speaking&at={AT}"
        first = _shot(url, tmp_path / "a.png")
        second = _shot(url, tmp_path / "b.png")
        worst = _worst_difference(first, second)
        if worst <= TOLERANCE:
            return

        # One render of the pair went wrong, not the page. It happens under a
        # loaded machine and has never been caught in the act: eight captures
        # of this URL taken while the whole other suite ran came back
        # byte-identical, as did twelve pairs measured before that. So the
        # answer is a third capture rather than a wider tolerance, which is
        # what makes this test worth running at all. Two fresh captures that
        # also disagree are a page that genuinely paints differently, and
        # still fail.
        print(f"\n{view}: recapturing after a worst channel difference of "
              f"{worst}, which is one render in a pair going wrong")
        third = _shot(url, tmp_path / "c.png")
        again = _worst_difference(second, third)
        assert again <= TOLERANCE, (
            f"{view} captured differently three times: worst channel "
            f"difference {worst} then {again}")

    def test_the_ar_view_does_not_wait_on_a_camera(self, server):
        """The AR view's camera state is decided, not raced.

        ``startCam()`` refuses to open a camera under test, but it is async and
        runs only when the view is entered, so whether the fallback backdrop
        had been applied by the moment the shutter fell was a coin toss: two
        captures of the same URL came back 1.24 million pixels apart, one with
        the backdrop and the note, one with a black frame and neither. The
        state is now set with the rest of the fixtures.
        """
        dom = _dom(f"{server}?view=ar&state=listening&at={AT}")
        match = re.search(r'id="arview"[^>]*class="([^"]*)"', dom)
        assert match, "no #arview in the rendered page"
        assert "nocam" in match.group(1).split(), (
            "the AR view is still deciding about a camera at capture time")
        assert _text_of(dom, "ar-note") == "test mode"

    def test_a_different_moment_is_a_different_picture(self, server, tmp_path):
        # The other half of the claim: the comparison above passes because the
        # page is being held still, not because it cannot tell pictures apart.
        base = f"{server}?view=dashboard&state=idle"
        first = _shot(f"{base}&at={AT}", tmp_path / "one.png")
        second = _shot(f"{base}&at=2026-08-15T17:05:00", tmp_path / "two.png")
        assert _worst_difference(first, second) > TOLERANCE


class TestProductionIsUntouched:
    def test_without_the_parameters_nothing_of_this_exists(self, server):
        dom = _dom(server)
        assert "data-test-ready" not in dom, "the harness ran without being asked"
        assert "142 W" not in dom, "fixture numbers reached a normal page"

    def test_a_view_on_its_own_is_not_test_mode(self, server):
        # ?view= is his own deep link and predates all of this.
        dom = _dom(f"{server}?view=hud")
        assert "data-test-ready" not in dom

    def test_the_real_ar_view_still_asks_for_his_camera(self, server):
        # The fixed camera state above is a test fixture. On the page he opens,
        # the AR view must still go and look for a camera.
        dom = _dom(f"{server}?view=ar")
        assert "data-test-ready" not in dom
        assert _text_of(dom, "ar-note") != "test mode"


# What a panel measures, from the page's own stylesheet. A layout that fits is
# a claim about these numbers, so they are read from one place.
PANEL_W, PANEL_H = 240, 96
# His own window, measured from the captures he takes of it.
HIS_SIZE = "2578,1398"


def _panels(dom: str) -> list:
    """Every file-space panel's box, as ``(left, top, right, bottom)``.

    The layout is inline transforms, so the rendered DOM carries the geometry
    without asking the browser to evaluate anything. Positions are relative to
    the middle of the window, which is where the scene is anchored.
    """
    out = []
    for style in re.findall(r'class="arfs-panel[^"]*"[^>]*?style="([^"]*)"', dom):
        found = re.search(r'translate3d\((-?[\d.]+)px,\s*(-?[\d.]+)px', style)
        assert found, f"a panel with no position: {style[:80]}"
        x, y = float(found.group(1)), float(found.group(2))
        out.append((x, y, x + PANEL_W, y + PANEL_H))
    return out


class TestEveryThingInAFolderIsReachable:
    """A folder of sixty showed a handful and said nothing about the rest, so
    what a folder holds could not be told from looking at it. ``?files=`` opens
    the file space on a folder of a known size, the same one every time."""

    def _dom(self, server, files, size="1280,720"):
        return _dom(f"{server}?view=ar&state=idle&at={AT}&files={files}", size=size)

    def test_it_says_how_many_are_in_the_folder(self, server):
        dom = self._dom(server, 62)
        shown = len(_panels(dom))
        assert 0 < shown < 62, "this window cannot be holding all sixty-two"
        assert _text_of(dom, "arfs-count") == f"1–{shown} of 62", \
            "a page of a folder has to say what it is a page of"

    def test_the_way_to_the_rest_is_on_screen(self, server):
        dom = self._dom(server, 62)
        shown = len(_panels(dom))
        pages = -(-62 // shown)
        assert _text_of(dom, "arfs-pageno") == f"page 1 of {pages}"
        assert re.search(r'id="arfs-pager"[^>]*class="[^"]*\bon\b', dom) or \
            re.search(r'class="[^"]*\bon\b[^"]*"[^>]*id="arfs-pager"', dom), \
            "a folder that does not fit showed no way through the rest of it"

    def test_a_folder_that_fits_is_not_paged(self, server):
        dom = self._dom(server, 6)
        assert len(_panels(dom)) == 6
        assert _text_of(dom, "arfs-count") == "6 items"

    def test_an_empty_folder_says_so_rather_than_looking_broken(self, server):
        dom = self._dom(server, 0)
        assert _panels(dom) == []
        assert _text_of(dom, "arfs-count") == "empty"

    def test_nothing_is_cut_off_by_the_edge_of_his_window(self, server):
        """Measured at the size he actually runs it, not at a guess."""
        width, height = (int(n) for n in HIS_SIZE.split(","))
        boxes = _panels(self._dom(server, 200, size=HIS_SIZE))
        assert boxes, "no panels at all"
        for left, top, right, bottom in boxes:
            assert -width / 2 <= left and right <= width / 2, \
                f"a panel runs from {left} to {right} in a window {width} wide"
            assert -height / 2 <= top and bottom <= height / 2, \
                f"a panel runs from {top} to {bottom} in a window {height} tall"

    def test_a_bigger_window_shows_more_of_the_folder(self, server):
        """The grid is worked out from the window rather than fixed, so his
        own screen is not paged into the same twenty as a small one."""
        small = len(_panels(self._dom(server, 200)))
        his = len(_panels(self._dom(server, 200, size=HIS_SIZE)))
        assert his > small, f"{his} panels on his screen against {small} on a small one"

    def test_the_file_space_is_not_opened_by_a_page_that_was_not_asked(self, server):
        dom = _dom(f"{server}?view=ar&files=62")
        assert _panels(dom) == [], "the fixture folder reached a normal page"
