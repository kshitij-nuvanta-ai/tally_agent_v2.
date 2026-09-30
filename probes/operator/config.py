"""Where the automated operator finds Wine, TallyPrime and the probe data folder (S0 spec §5.8; live 2026-09-22)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_MARKER = "s0probe"
WINE_BIN = Path("/Applications/Wine Stable.app/Contents/Resources/wine/bin/wine")
TALLY_DIR = Path.home() / ".wine/drive_c/Program Files/TallyPrimeEditLog"
DATA_DIR = Path.home() / ".wine/drive_c/users/Public/TallyPrimeEditLog" / DATA_MARKER
DATA_DIR_WINDOWS = r"C:\users\Public\TallyPrimeEditLog" + "\\" + DATA_MARKER


@dataclass(frozen=True)
class OperatorConfig:
    wine_bin: Path
    tally_dir: Path
    data_dir: Path                     # the s0probe folder, as the Mac sees it
    data_dir_windows: str              # the same folder as Tally sees it (for /DATA:)
    seed_dir: Path                     # repo seed_data/ — read-only source of the pristine company folder
    backups_dir: Path                  # beside s0probe, never inside it (Tally treats its data folder as companies)
    wine_log: Path                     # Wine's stdout / stderr
    # B = 100000: the number Tally actually assigned when the operator created company B in the
    # UI on 2026-09-24 (lowest free number, not "next after A").
    company_numbers: dict[str, str] = field(default_factory=lambda: {"A": "100003", "B": "100000"})
    start_wait_s: float = 240.0
    stop_wait_s: float = 20.0
    click_wait_s: float = 900.0        # the licence box waits up to 15 minutes for the person
    poll_s: float = 3.0

    def __post_init__(self) -> None:
        windows = self.data_dir_windows.rstrip("\\").lower()
        if self.data_dir.name != DATA_MARKER or not windows.endswith("\\" + DATA_MARKER):
            raise ValueError(f"Tally's data path must be the {DATA_MARKER!r} folder (S0 spec §5.8), got {self.data_dir}")
        if self.backups_dir.resolve().is_relative_to(self.data_dir.resolve()):
            raise ValueError("Backups must live outside the s0probe data folder")

    @property
    def edition(self) -> str:
        return "Edit Log" if "editlog" in self.tally_dir.name.lower() else "standard"

    def company_folder(self, label: str) -> Path:
        return self.data_dir / self.company_numbers[label]

    def seed_folder(self, label: str) -> Path:
        return self.seed_dir / self.company_numbers[label]


def default_config() -> OperatorConfig:
    return OperatorConfig(wine_bin=WINE_BIN, tally_dir=TALLY_DIR, data_dir=DATA_DIR, data_dir_windows=DATA_DIR_WINDOWS,
                          seed_dir=REPO_ROOT / "seed_data", backups_dir=DATA_DIR.parent / "s0probe-backups",
                          wine_log=REPO_ROOT / "probes" / "results" / "logs" / "tally-wine.log")
