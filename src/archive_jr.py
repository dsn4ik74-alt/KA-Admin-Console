"""Run the supplied event-log archiver with editable parameters."""
import os
from pathlib import Path
import sys

from powershell_jobs import run_script


def archive_logs(source, destination, base_name, months, delete, on_event=lambda event: None):
    if not source.strip() or not destination.strip():
        raise ValueError("Укажите исходную папку и папку архивов")
    source = Path(source.strip()).resolve()
    destination = Path(destination.strip()).resolve()
    if not source.is_dir():
        raise ValueError("Исходная папка ЖР не найдена")
    if destination == source or destination.is_relative_to(source):
        raise ValueError("Папка архивов должна находиться вне исходной папки")
    try:
        months = int(months)
    except (ValueError, TypeError):
        raise ValueError("Число месяцев должно быть целым") from None
    if not 1 <= months <= 1200:
        raise ValueError("Укажите от 1 до 1200 месяцев")
    environment = os.environ.copy()
    environment.update(JR_SOURCE=str(source), JR_ARCHIVE=str(destination),
                       JR_BASE_NAME=base_name.strip(), JR_MONTHS=str(months),
                       JR_DELETE_AFTER_ARCHIVE="1" if delete else "0")
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    code = (root / "archive_jr.ps1").read_text(encoding="utf-8-sig")
    return run_script(code, {}, on_event, environment=environment, require_done=False)
