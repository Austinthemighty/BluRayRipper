"""Windows Job Object helper: safe no-op off Windows, never raises."""

import sys

from blurayripper.tools.winjob import enable_kill_on_close


def test_enable_is_noop_off_windows():
    result = enable_kill_on_close()
    if not sys.platform.startswith("win"):
        assert result is False   # no-op, no exception
    else:
        assert isinstance(result, bool)
