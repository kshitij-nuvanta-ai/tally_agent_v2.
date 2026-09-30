from tally_bridge.client import TallyClient
from probes import p02_active_company_guid as p02
from probes import p05_voucher_month_bounds as p05
from probes import p24_secured_company as p24
from probes.capture import Capture
from probes.companies import COMPANIES
from probes.results import ResultsStore
from probes.runner import run_probe
from tests.probes.fake_books import FakeBooks, seed_company_c
from tests.probes.fakes import ScriptedIO, mark_done, ready_store

C = COMPANIES["C"]
# Ruling S8: the throwaway credentials the fake person "types into TallyPrime". The harness never sees them; the
# credential test searches every output for these exact values (a word search for "password" can't catch a leak).
DUMMY_USERNAME = "s0dummy-user-7Qk2"
DUMMY_PASSWORD = "s0dummy-login-Zp91x"
DUMMY_VAULT_PASSWORD = "s0dummy-vault-Hr48w"
ALL_PAUSES = [p24.SECURITY_ON, p24.SECURITY_RESELECT, p24.LOGIN, p24.VAULT_ON, p24.VAULT_RESELECT, p24.VAULT_OPEN]


def _books() -> FakeBooks:
    books = FakeBooks(name=C, educational=True)
    seed_company_c(books)
    return books


def operator(books: FakeBooks, *, prompt: str = "modal", after_vault=None, after_login=None, early=None):
    """Plays the person at TallyPrime: a prompt either blocks the XML server (modal), leaves no company open (closed),
    or lists company C while every named read fails (listed, review I1). The credentials go into Tally (the fake's
    state) only. `early` = the pause (LOGIN / VAULT_OPEN) at which the person
    presses Enter before clearing the prompt (Ruling S3)."""
    def act(text: str) -> None:
        if text == p24.SECURITY_ON:
            books.edit_state(lambda s: s.__setitem__("security", {"user": DUMMY_USERNAME, "pw": DUMMY_PASSWORD}))
        elif text == p24.VAULT_ON:
            books.edit_state(lambda s: s.__setitem__("vault", DUMMY_VAULT_PASSWORD))
        elif text in (p24.SECURITY_RESELECT, p24.VAULT_RESELECT):
            if prompt == "modal":
                books.popup = True
            elif prompt == "listed":
                books.popup_listed = True
            else:
                books.loaded = False
        elif text in (p24.LOGIN, p24.VAULT_OPEN):
            if text == early:
                return                                   # Enter pressed while the prompt is still up
            books.popup, books.popup_listed, books.loaded = False, False, True
            hook = after_vault if text == p24.VAULT_OPEN else after_login
            if hook:
                hook(books)
    return act


async def _run(tmp_path, books, io, *, confirm_active=True):
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    if confirm_active:
        store.confirm_request("active_company", 2, p02.CANDIDATES["a"], candidate="a")
    mark_done(store, 5, part="B")
    store.confirm_request("voucher_month", 5, p05.svdates_template(), form="svdates_typed")
    await run_probe(p24.PROBE, labels=None, client=TallyClient(transport=books.transport(), trust_env=False), store=store,
                    capture=Capture(tmp_path / "fixtures"), io=io)
    return store.probe_entry(24)["parts"]["C"]


async def test_security_and_vault_leave_export_unchanged(tmp_path):
    books = _books()
    io = ScriptedIO(on_wait=operator(books))
    part = await _run(tmp_path, books, io)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert io.waits == ALL_PAUSES
    shapes = part["observations"]["gate_shapes"]
    assert shapes["security_login_pending"]["company_list"]["transport"] == "timeout"
    assert shapes["vault_prompt_pending"]["active_company"]["transport"] == "timeout"
    assert "No credentials" in part["spec_impact"]
    # both halves CONFIRMED: the whole-probe sentence, once, and no per-half repeats (C47 review I1)
    assert part["spec_impact"].count(p24.CONFIRMED_IMPACT) == 1 and " on: " not in part["spec_impact"]
    assert "p24_C_baseline_vouchers.xml" in part["fixtures"]
    assert any(f.startswith("p24_C_security_login_pending_company_list") for f in part["fixtures"])


async def test_a_prompt_that_answers_with_no_company_is_recorded(tmp_path):
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, prompt="closed")))
    assert part["outcome"] == "CONFIRMED", part["summary"]
    shape = part["observations"]["gate_shapes"]["security_login_pending"]["company_list"]
    assert shape == {"transport": "answered", "body": {"companies": []}}


async def test_a_new_guid_after_the_vault_is_different_and_points_at_relink(tmp_path):
    books = _books()
    rekey = lambda b: b.edit_state(lambda s: s.__setitem__("guid", "vaulted-guid"))
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, after_vault=rekey)))
    assert part["outcome"] == "DIFFERENT" and "Q25" in part["spec_impact"]


async def test_an_empty_export_after_the_vault_fails(tmp_path):
    books = _books()
    empty = lambda b: b.edit_state(lambda s: (s["ledgers"].clear(), s["vouchers"].clear()))
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, after_vault=empty)))
    assert part["outcome"] == "FAILED" and "tallyvault" in part["summary"].lower()


def _other_company(b: FakeBooks) -> None:
    """Another company (its own name, GUID and data) open instead of C: an operator slip."""
    b.edit_state(lambda s: (s.__setitem__("name", "Other Co"), s.__setitem__("guid", "other"),
                            s["ledgers"].pop(p24.COMPANY_C_LEDGER), s["vouchers"].clear()))


async def test_another_company_open_after_login_blocks(tmp_path):
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, after_login=_other_company)))
    assert part["outcome"] == "BLOCKED" and "Other Co" in part["summary"]


async def test_a_blocked_slip_keeps_what_each_stage_measured(tmp_path):
    """Review I2(a): every stage is observed before `_slip`, so a BLOCKED record keeps the evidence."""
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, after_login=_other_company)))
    assert part["outcome"] == "BLOCKED", part["summary"]
    obs = part["observations"]
    assert obs["baseline"]["ok"] and obs["security_on"]["companies"] == ["Other Co"]
    assert obs["security_on"]["active_guid"] == "other"


async def test_a_slip_after_the_vault_keeps_the_security_and_vault_stages(tmp_path):
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, after_vault=_other_company)))
    assert part["outcome"] == "BLOCKED", part["summary"]
    obs = part["observations"]
    assert obs["security_on"]["ok"] and obs["vault_on"]["companies"] == ["Other Co"]
    assert "vault_on" in part["summary"] and "re-keyed" in part["summary"]


async def test_a_vault_rename_with_a_new_guid_and_cs_data_is_different_not_blocked(tmp_path):
    """Review I2(b): only C is open, its data equals the baseline, but name AND GUID changed (vault rename + re-key).
    Blocking would ask for 'only company C' forever; it is recorded as DIFFERENT (like S9's same-GUID rename)."""
    books = _books()
    rekey = lambda b: b.edit_state(lambda s: (s.__setitem__("name", "Probe Vault Encrypted"),
                                              s.__setitem__("guid", "vaulted-guid")))
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, after_vault=rekey)))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert part["observations"]["sub_verdicts"] == {"security": "CONFIRMED", "TallyVault": "DIFFERENT"}
    assert "vault renamed and re-keyed" in part["summary"] and "Probe Vault Encrypted" in part["summary"]
    assert "Q25" in part["spec_impact"]
    # C47 review I1: a mixed result must not also claim the whole probe is unchanged -- the CONFIRMED half's impact
    # is scoped to its own half.
    assert p24.CONFIRMED_IMPACT not in part["spec_impact"]
    assert "secured or vaulted" not in part["spec_impact"]
    assert "Security on:" in part["spec_impact"] and "TallyVault on:" not in part["spec_impact"]


async def test_a_failed_vault_half_does_not_claim_the_vault_is_unchanged(tmp_path):
    """C47 review I1: security CONFIRMED + TallyVault FAILED -> no whole-probe 'unchanged' claim beside 'fails'."""
    books = _books()
    empty = lambda b: b.edit_state(lambda s: (s["ledgers"].clear(), s["vouchers"].clear()))
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, after_vault=empty)))
    assert part["outcome"] == "FAILED", part["summary"]
    text = part["spec_impact"]
    assert "fails even with it open" in text and p24.CONFIRMED_IMPACT not in text
    assert "secured or vaulted" not in text
    assert text.index("fails even with it open") < text.index("Security on:")


def test_each_half_confirmed_impact_names_only_its_half():
    assert p24.half_confirmed_impact("security").startswith("Security on:")
    assert p24.half_confirmed_impact("TallyVault").startswith("TallyVault on:")
    for half in ("security", "TallyVault"):
        assert "secured or vaulted" not in p24.half_confirmed_impact(half)


async def test_company_c_without_setup_c_blocks(tmp_path):
    books = FakeBooks(name=C, educational=True)         # no seed_company_c: no S0 ledger, no voucher
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books)))
    assert part["outcome"] == "BLOCKED" and "setup-c" in part["summary"]


async def test_auto_mode_is_refused(tmp_path):
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(run_mode="auto"))
    assert part["outcome"] == "BLOCKED" and "manual" in part["summary"]


async def test_without_probe_2s_request_it_blocks(tmp_path):
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books)), confirm_active=False)
    assert part["outcome"] == "BLOCKED" and "probe 2" in part["summary"]


async def test_probe_24_never_asks_for_or_sends_a_credential(tmp_path):
    """Ruling S8: search every output for the ACTUAL throwaway values the person typed into Tally."""
    books = _books()
    io = ScriptedIO(on_wait=operator(books))
    part = await _run(tmp_path, books, io)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert books.state["security"]["pw"] == DUMMY_PASSWORD and books.state["vault"] == DUMMY_VAULT_PASSWORD
    assert io.asks == []
    outputs = [f for f in (tmp_path / "fixtures").glob("p24_C_*") if f.is_file()] + [tmp_path / "results.json"]
    assert len(outputs) > 10
    texts = {f.name: f.read_text(encoding="utf-8") for f in outputs}
    texts["console"] = "\n".join(io.said + io.waits + io.asks)
    for name, text in texts.items():
        for secret in (DUMMY_USERNAME, DUMMY_PASSWORD, DUMMY_VAULT_PASSWORD):
            assert secret not in text, (name, secret)
    for request in books.requests:
        for secret in (DUMMY_USERNAME, DUMMY_PASSWORD, DUMMY_VAULT_PASSWORD):
            assert secret not in request
        assert "<PASSWORD" not in request.upper() and "<USERNAME" not in request.upper()


async def test_enter_at_login_with_the_prompt_still_open_blocks_not_fails(tmp_path):
    """Ruling S3: a person's timing is never recorded as a Tally finding."""
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, early=p24.LOGIN)))
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert "prompt still open" in part["summary"] and "security_on" in part["summary"]


async def test_enter_at_vault_open_with_no_company_open_blocks_not_fails(tmp_path):
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, prompt="closed", early=p24.VAULT_OPEN)))
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert "prompt still open" in part["summary"] and "vault_on" in part["summary"]


async def test_a_vault_rename_with_the_same_guid_is_different(tmp_path):
    """Ruling S9 (plan Ambiguity 16): a rename that keeps the GUID is recorded as DIFFERENT, not FAILED."""
    books = _books()
    rename = lambda b: b.edit_state(lambda s: s.__setitem__("name", "Probe Vault Renamed"))
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, after_vault=rename)))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert part["observations"]["sub_verdicts"] == {"security": "CONFIRMED", "TallyVault": "DIFFERENT"}
    assert "renamed" in part["summary"] and "Probe Vault Renamed" in part["summary"]
    assert "GUID" in part["spec_impact"]


async def test_listed_prompt_shape_is_recorded_and_a_real_login_confirms(tmp_path):
    """Review I1: the prompt lists company C while every named read fails. A proper login still confirms."""
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, prompt="listed")))
    assert part["outcome"] == "CONFIRMED", part["summary"]
    shape = part["observations"]["gate_shapes"]["security_login_pending"]["company_list"]
    assert shape == {"transport": "answered", "body": {"companies": [C]}}


async def test_enter_at_login_with_a_listed_prompt_still_open_blocks_not_fails(tmp_path):
    """Review I1: Tally lists C but the prompt is still up — the post-Enter reads equal the prompt state."""
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, prompt="listed", early=p24.LOGIN)))
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert "prompt still open" in part["summary"] and "security_on" in part["summary"]
    assert any(f.startswith("p24_C_security_on_ready_company_list") for f in part["fixtures"])


async def test_enter_at_vault_open_with_a_listed_prompt_still_open_blocks_not_fails(tmp_path):
    books = _books()
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, prompt="listed", early=p24.VAULT_OPEN)))
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert "prompt still open" in part["summary"] and "vault_on" in part["summary"]
    assert part["observations"]["security_on"]["ok"]


async def test_a_genuine_failure_after_login_with_a_listed_prompt_still_fails(tmp_path):
    """Review I1: once the prompt is gone (the active company answers), a broken export is a real FAILED."""
    books = _books()
    empty = lambda b: b.edit_state(lambda s: (s["ledgers"].clear(), s["vouchers"].clear()))
    part = await _run(tmp_path, books, ScriptedIO(on_wait=operator(books, prompt="listed", after_vault=empty)))
    assert part["outcome"] == "FAILED", part["summary"]
    assert "tallyvault" in part["summary"].lower()
