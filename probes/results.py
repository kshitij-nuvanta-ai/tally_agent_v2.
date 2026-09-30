"""results.json: environment, confirmed requests and per-probe results (S0 spec §5.7)."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from probes.core import Outcome, PartResult, combine


class ResultsStore:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"environment": {}, "confirmed_requests": {}, "probes": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
        )
        tmp.replace(self.path)

    @property
    def environment(self) -> dict[str, Any]:
        return self.data["environment"]

    def update_environment(self, **values: Any) -> None:
        self.data["environment"].update(values)
        self.save()

    def confirmed(self, name: str) -> dict[str, Any] | None:
        return self.data["confirmed_requests"].get(name)

    def confirm_request(self, name: str, probe_id: int, xml_template: str, **extra: Any) -> None:
        self.data["confirmed_requests"][name] = {"probe": probe_id, "xml_template": xml_template, **extra}
        self.save()

    def probe_entry(self, probe_id: int) -> dict[str, Any] | None:
        return self.data["probes"].get(str(probe_id))

    def outcome(self, probe_id: int) -> Outcome | None:
        entry = self.probe_entry(probe_id)
        return Outcome(entry["outcome"]) if entry else None

    def part_outcome(self, probe_id: int, part: str) -> Outcome | None:
        entry = self.probe_entry(probe_id)
        part_entry = entry["parts"].get(part) if entry else None
        return Outcome(part_entry["outcome"]) if part_entry else None

    def record_part(
        self,
        probe_id: int,
        part: str,
        result: PartResult,
        *,
        all_parts: list[str],
        fixtures: list[str],
        manual_steps: list[dict[str, Any]],
        ran_at: datetime,
    ) -> Outcome:
        """Store one part's result (the previous run of that part moves to history); return the probe outcome."""
        entry = self.data["probes"].setdefault(
            str(probe_id), {"outcome": Outcome.PARTIAL.value, "parts": {}, "history": []}
        )
        previous = entry["parts"].get(part)
        if previous is not None:
            entry["history"].append({"part": part, **previous})
        entry["parts"][part] = {
            "outcome": result.outcome.value,
            "summary": result.summary,
            "observations": result.observations,
            "spec_impact": result.spec_impact,
            "fixtures": fixtures,
            "manual_steps": manual_steps,
            "ran_at": ran_at.isoformat(timespec="seconds"),
        }
        outcome = combine({
            label: Outcome(entry["parts"][label]["outcome"]) if label in entry["parts"] else None
            for label in all_parts
        })
        entry["outcome"] = outcome.value
        entry["remaining"] = [label for label in all_parts if label not in entry["parts"]]
        self.save()
        return outcome

    def record_anchor_check(self, *, when: str, label: str, ok: bool, problems: list[str],
                            receivable: Decimal | None, payable: Decimal | None, ran_at: datetime) -> None:
        """One anchors check from an ordered run (S0 spec §4.2), appended to `anchor_checks`."""
        self.data.setdefault("anchor_checks", []).append({
            "when": when, "label": label, "ok": ok, "problems": list(problems),
            "receivable": receivable, "payable": payable, "ran_at": ran_at.isoformat(timespec="seconds"),
        })
        self.save()

    @property
    def anchor_checks(self) -> list[dict[str, Any]]:
        return self.data.get("anchor_checks", [])
