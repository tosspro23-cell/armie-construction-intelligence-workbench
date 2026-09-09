"""Regression test for the container-crashing env_file defect (SPEC-M3 §6 addendum).

Found by actually running the built apps/api/Dockerfile image (docker build
+ docker run), not assumed: Settings.model_config computed env_file as
``Path(__file__).resolve().parents[3] / ".env"``, which raised IndexError
and crashed the API container at import time, because the container's
WORKDIR/COPY layout puts config.py only 3 directories below the filesystem
root, one short of local dev's 4.

The first test below reproduces the pre-fix expression's own failure mode
directly, against the container's exact path shape -- not the current
implementation -- so it stays a genuine defect reproduction rather than a
test of the fix testing itself. The second test proves the replacement
(app.config._default_env_file) resolves the same repo-root .env for the
local dev layout while never raising for the container's shallower one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import _default_env_file


def test_defect_reproduction_parents_indexing_crashes_at_container_depth() -> None:
    """This is the exact pre-fix expression, not app.config's current code:
    it must still raise here, proving the container crash was real and this
    is genuinely the failure mode being fixed, independent of the fix
    itself.
    """
    container_config_path = Path("/app/app/config.py")

    with pytest.raises(IndexError):
        container_config_path.resolve().parents[3]


def test_default_env_file_matches_local_dev_layout_and_never_raises_for_the_container() -> None:
    local_dev_config_path = Path("/repo/apps/api/app/config.py")
    container_config_path = Path("/app/app/config.py")

    assert _default_env_file(local_dev_config_path) == Path("/repo/.env")
    # Must not raise -- this is the container's actual on-disk depth.
    assert _default_env_file(container_config_path) == Path("/.env")
