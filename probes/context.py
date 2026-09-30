"""ProbeContext: what a probe part uses to talk to Tally and record findings (S0 spec §5.2)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from agent.tally.client import TallyClient, TallyResponse
from agent.tally.envelopes import COMPANY_PLACEHOLDER, build_company_list, esc
from agent.tally.exceptions import TallyConnectionError, TallyResponseError, TallyTimeoutError
from agent.tally.xml_utils import parse_company_list, read_objects, sanitize_xml
from probes.actions import ASK_KINDS, PAUSE_KINDS, Action
from probes.capture import Capture
from probes.console import ProbeIO
from probes.core import Probe, ProbeBlocked
from probes.results import ResultsStore
from probes.safety import check_company as _check_company
from probes.safety import check_educational_dates, check_request

POPUP_HINT = "Check Tally for an open popup or modal and dismiss it (LESSONS §15 rule 10)."
ENVIRONMENT_SIDECAR_KEYS = ("wine", "tally_version", "edition", "licence")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def select_company(text: str, company: str, fields: list[str]) -> dict[str, str]:
    """The `fields` of `company`'s row in a Company collection response."""
    for row in read_objects(text, "COMPANY", ["Name", *fields]):
        if row["Name"] == company:
            return {field: row[field] for field in fields}
    raise ProbeBlocked(f"No row for {company!r} in the company response")


class ProbeContext:
    def __init__(self, *, probe: Probe, part: str, company_name: str, client: TallyClient,
                 store: ResultsStore, capture: Capture, io: ProbeIO, allow_risky: bool = False):
        self.probe = probe
        self.part = part
        self.company_name = company_name
        self.client = client
        self.store = store
        self.capture = capture
        self.io = io
        # `run … --allow-risky`: the operator has opted into the steps known to be able to freeze Tally (probe 16's
        # SVFROMDATE ledger read). Off by default — a default run must never send them.
        self.allow_risky = allow_risky
        self.company_guid: str | None = None
        self.observations: dict[str, Any] = {}
        self.fixtures: list[str] = []
        self.manual_steps: list[dict[str, Any]] = []
        self.abort_notes: list[str] = []
        self.last_response: TallyResponse | None = None
        self._used_steps: set[str] = set()

    @property
    def run_mode(self) -> str:
        """'auto' when the automated operator performs the pauses (S0-D9), else 'manual'."""
        return getattr(self.io, "run_mode", "manual")

    def on_abort(self, note: str) -> None:
        """Note something to clean up in Tally if this part ends up BLOCKED (F1)."""
        if note not in self.abort_notes:
            self.abort_notes.append(note)

    def resolve_abort(self, note: str) -> None:
        """The cleanup `note` is no longer needed (the step it guarded against completed)."""
        if note in self.abort_notes:
            self.abort_notes.remove(note)

    async def check_company(self) -> None:
        """Re-check the guard (S0 spec §4.5) — e.g. right after a pause that could have reopened a company."""
        _check_company(await self.company_names(), self.company_name, mutating=self.probe.mutating)

    async def try_send(self, step: str, xml: str, timeout: float | None = None) -> tuple[str | None, dict | None]:
        """Guard → send → capture. Returns (sanitized text, None) or (None, {"kind", "message"}) on a transport error."""
        if step in self._used_steps:
            raise ValueError(f"Duplicate step name in this part: {step!r}")
        self._used_steps.add(step)
        check_request(xml)
        check_educational_dates(xml, self.store.environment.get("licence"))
        env = self.store.environment
        common = dict(probe_id=self.probe.id, part=self.part, step=step, company_name=self.company_name,
                      company_guid=self.company_guid, request_xml=xml, sent_at=datetime.now().astimezone(),
                      environment={k: env[k] for k in ENVIRONMENT_SIDECAR_KEYS if k in env})
        try:
            response: TallyResponse = await self.client.post_xml(xml, timeout=timeout)
        except (TallyConnectionError, TallyResponseError) as exc:
            if isinstance(exc, TallyTimeoutError):
                kind = "timeout"
            elif isinstance(exc, TallyConnectionError):
                kind = "refused"
            else:
                kind = "http"
            error = {"kind": kind, "message": str(exc)}
            self.last_response = None
            self.fixtures.append(self.capture.save(**common, error=error) + ".json")
            return None, error
        self.last_response = response
        self.fixtures.append(self.capture.save(**common, response=response))
        return sanitize_xml(response.text), None

    async def send(self, step: str, xml: str, timeout: float | None = None) -> str:
        """Like try_send, but a transport error blocks the part."""
        text, error = await self.try_send(step, xml, timeout)
        if error is None:
            return text
        if error["kind"] == "timeout":
            raise ProbeBlocked(f"{step}: {error['message']}. {POPUP_HINT}")
        if error["kind"] == "refused":
            raise ProbeBlocked(f"{step}: Tally not reachable ({error['message']})")
        raise ProbeBlocked(f"{step}: {error['message']}")

    async def _post_uncaptured(self, xml: str, what: str) -> str:
        check_request(xml)
        check_educational_dates(xml, self.store.environment.get("licence"))
        try:
            response = await self.client.post_xml(xml)
        except TallyTimeoutError as exc:
            raise ProbeBlocked(f"{what}: {exc}. {POPUP_HINT}") from exc
        except TallyConnectionError as exc:
            raise ProbeBlocked(f"{what}: Tally not reachable ({exc})") from exc
        except TallyResponseError as exc:
            raise ProbeBlocked(f"{what}: {exc}") from exc
        return sanitize_xml(response.text)

    async def company_names(self) -> list[str]:
        """Loaded companies via the company list (guard use; not captured as a fixture)."""
        return parse_company_list(await self._post_uncaptured(build_company_list(), "company list"))

    async def counters(self, step: str | None = None) -> dict[str, str]:
        """Company counters via the request probe 1 confirmed. Captured only when `step` is given."""
        confirmed = self.store.confirmed("company_counters")
        if confirmed is None:
            raise ProbeBlocked("Company counters aren't confirmed yet. Run probe 1 first.")
        xml = confirmed["xml_template"].replace(COMPANY_PLACEHOLDER, esc(self.company_name))
        text = await self.send(step, xml) if step else await self._post_uncaptured(xml, "counters")
        return select_company(text, self.company_name, confirmed["fields"])

    def pause(self, instruction: str, action: Action | None = None) -> None:
        if action is not None and action.kind not in PAUSE_KINDS:
            raise ValueError(f"{action.kind!r} is not a pause action")
        if not self.io.interactive:
            raise ProbeBlocked(f"Needs a manual step: {instruction}")
        self.io.wait(instruction, action)
        step: dict[str, Any] = {"n": len(self.manual_steps) + 1, "instruction": instruction, "done_at": _now()}
        if action is not None:
            step["action"] = action.kind
        self.manual_steps.append(step)

    def ask(self, prompt: str, action: Action | None = None) -> str:
        if action is not None and action.kind not in ASK_KINDS:
            raise ValueError(f"{action.kind!r} is not an ask action")
        if not self.io.interactive:
            raise ProbeBlocked(f"Needs a manual step: {prompt}")
        answer = self.io.ask(prompt, action)
        step: dict[str, Any] = {"n": len(self.manual_steps) + 1, "instruction": prompt, "input": answer,
                                "done_at": _now()}
        if action is not None:
            step["action"] = action.kind
        self.manual_steps.append(step)
        return answer

    def observe(self, key: str, value: Any) -> None:
        self.observations[key] = value

    def confirm_request(self, name: str, xml_template: str, **extra: Any) -> None:
        self.store.confirm_request(name, self.probe.id, xml_template, **extra)
