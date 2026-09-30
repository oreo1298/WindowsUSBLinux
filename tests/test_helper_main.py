import os

from rufux.helper.main import _StdinLines


def test_stdin_lines_reads_the_job_then_commands():
    r, w = os.pipe()
    try:
        os.write(w, b'{"action": "write"}\ncancel\n')  # a cancel can arrive with the job
        lines = _StdinLines(r)
        assert lines.readline(4096) == b'{"action": "write"}\n'
        assert lines.readline(4096) == b"cancel\n"
        os.write(w, b"partial")
        os.close(w)
        w = -1
        assert lines.readline(4096) == b"partial"  # end of input without a newline
        assert lines.readline(4096) == b""
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)


def test_stdin_lines_respects_the_limit():
    r, w = os.pipe()
    try:
        os.write(w, b"0123456789\nnext\n")
        lines = _StdinLines(r)
        assert lines.readline(4) == b"0123"
        assert lines.readline(100) == b"456789\n"
        assert lines.readline(100) == b"next\n"
    finally:
        os.close(r)
        os.close(w)
