"""
SnP Image Labeler - Duplicate Prompt

Shown when an image about to be segregated is already in processed_images.csv.
Offers Skip / Update / Cancel, with an optional "apply to all remaining" so a
large batch does not turn into hundreds of dialogs.

Building the box and resolving the answer are separate from exec_() so both can
be exercised without opening a modal window.
"""

from pathlib import Path
from typing import Any, Dict, Tuple

from PyQt5.QtWidgets import QCheckBox, QMessageBox

from core.segregation_manager import class_folder_name

# Returned actions
SKIP   = "skip"
UPDATE = "update"
CANCEL = "cancel"


def describe_record(image_path: str, record: Dict[str, Any]) -> Tuple[str, str]:
    """(headline, detail) text for an already-processed image."""
    name = Path(image_path).name
    when = record.get("processed_at") or "an earlier session"

    folders = [
        f"    • {class_folder_name(c)}/{s}/"
        for c, s in zip(record.get("classes", []), record.get("splits", []))
        if c and s
    ]

    if record.get("matched_by") == "content":
        recorded = record.get("image_name") or "another file"
        headline = (f"'{name}' has the same contents as '{recorded}', "
                    f"which was already processed.")
    else:
        headline = f"'{name}' has already been processed."

    detail = (
        f"Processed on {when} into:\n"
        + ("\n".join(folders) if folders else "    (no folders recorded)")
        + "\n\nSkip it, or update it with the classes selected now?"
    )
    return headline, detail


def build_duplicate_box(parent, image_path: str, record: Dict[str, Any]):
    """Construct the prompt. Returns (box, buttons_by_action, remember_checkbox)."""
    headline, detail = describe_record(image_path, record)

    box = QMessageBox(parent)
    box.setWindowTitle("Already Processed")
    box.setIcon(QMessageBox.Question)
    box.setText(headline)
    box.setInformativeText(detail)

    # ActionRole keeps Skip and Update side by side in the order added,
    # with Cancel pushed to the end by RejectRole.
    buttons = {
        SKIP:   box.addButton("Skip",   QMessageBox.ActionRole),
        UPDATE: box.addButton("Update", QMessageBox.ActionRole),
        CANCEL: box.addButton("Cancel", QMessageBox.RejectRole),
    }
    box.setDefaultButton(buttons[SKIP])

    remember = QCheckBox("Apply my choice to all remaining images")
    remember.setToolTip(
        "Reuse this answer for every already-processed image until the images "
        "are re-imported or the dataset folder is changed."
    )
    box.setCheckBox(remember)
    return box, buttons, remember


def resolve_choice(clicked, buttons: Dict[str, Any], remember: bool) -> Tuple[str, bool]:
    """Map the clicked button to (action, apply_to_all). Cancel is never remembered."""
    if clicked is buttons[UPDATE]:
        return UPDATE, remember
    if clicked is buttons[SKIP]:
        return SKIP, remember
    return CANCEL, False


def ask_duplicate_action(parent, image_path: str, record: Dict[str, Any]) -> Tuple[str, bool]:
    """
    Ask what to do about an already-processed image.
    Returns (action, apply_to_all) where action is SKIP / UPDATE / CANCEL.
    """
    box, buttons, remember = build_duplicate_box(parent, image_path, record)
    box.exec_()
    return resolve_choice(box.clickedButton(), buttons, remember.isChecked())
