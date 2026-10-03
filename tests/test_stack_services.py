"""The stack editor lists every service of the repo in one filterable checkbox."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from pdms_cli.commands import stacks


def test_ask_stack_services_lists_the_whole_repo(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    candidates = [root / name for name in ("lead/b", "lead/a", "lead/running", "broker/sqs", "lead/current")]
    monkeypatch.setattr(stacks, "running_by_service",
                        lambda: {str(root / "lead/running"): [8090], str(root / "broker/sqs"): [0],
                                 str(root / "lead/current"): [8080]})
    seen = {}

    def checkbox(message, choices, **kwargs):
        bindings = KeyBindings()
        for key in (Keys.ControlI, Keys.ControlA):
            bindings.add(key)(lambda event: None)
        seen.update(choices=choices, kwargs=kwargs, bindings=bindings)
        picked = ["lead/a", "lead/current", "gone/old"]
        return SimpleNamespace(application=SimpleNamespace(key_bindings=bindings), unsafe_ask=lambda: picked)

    monkeypatch.setattr(stacks.questionary, "checkbox", checkbox)
    result = stacks.ask_stack_services(root, candidates, ["lead/current", "gone/old"])

    assert [c.value for c in seen["choices"]] == ["lead/current", "gone/old", "broker/sqs", "lead/running",
                                                  "lead/a", "lead/b"]
    assert [c.value for c in seen["choices"] if c.checked] == ["lead/current", "gone/old"]
    titles = {c.value: c.title for c in seen["choices"]}
    assert titles["lead/current"] == "lead/current"  # already in the stack: no tag
    assert titles["lead/running"].endswith(":8090)") and ":0" not in titles["broker/sqs"]
    assert seen["kwargs"]["use_search_filter"] and seen["kwargs"]["validate"]([]) is not True
    assert not seen["bindings"].bindings  # Tab / Ctrl+A would act on every service, not only the filtered ones
    assert result == ["lead/current", "gone/old", "lead/a"]
