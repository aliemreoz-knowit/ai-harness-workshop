import os
from pathlib import Path

import pytest

from harness import state as state_mod


def test_save_retries_while_a_reader_holds_state_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = state_mod.RunState.__new__(state_mod.RunState)
    st.path = tmp_path / "state.json"
    st.data = {"run_id": "r1"}
    real, calls = os.replace, []

    def busy_twice(src: object, dst: object) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(state_mod.os, "replace", busy_twice)
    monkeypatch.setattr(state_mod, "REPLACE_PAUSE_S", 0)
    st.save()
    assert len(calls) == 3 and '"run_id": "r1"' in st.path.read_text(encoding="utf-8")


def test_save_gives_up_after_the_last_try(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = state_mod.RunState.__new__(state_mod.RunState)
    st.path = tmp_path / "state.json"
    st.data = {"run_id": "r1"}

    def always_busy(src: object, dst: object) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(state_mod.os, "replace", always_busy)
    monkeypatch.setattr(state_mod, "REPLACE_PAUSE_S", 0)
    with pytest.raises(PermissionError):
        st.save()
