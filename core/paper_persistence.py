"""Paper mode position persistence with atomic writes and recovery."""

from pathlib import Path
from typing import Optional

from core.base_persistence import BasePositionPersistence


class PaperPositionPersistence(BasePositionPersistence):
    """
    Paper mode position persistence.
    
    Inherits atomic writes, backup/recovery, and proper error handling
    from BasePositionPersistence.
    """

    def __init__(self, path: Optional[Path] = None):
        super().__init__(path or Path("data/paper_positions.json"))
