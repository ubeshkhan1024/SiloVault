"""SQLAlchemy ORM models package.

Model registration contract
----------------------------
Every concrete model in SiloVault must be imported here and re-exported via
``__all__``.

When :func:`backend.database.init_db` runs, it executes ``import backend.models``
before calling ``Base.metadata.create_all()``. Re-exporting all models here
guarantees that every table class is loaded into Python memory and registered
on ``Base.metadata`` before table creation executes.

When adding a new model module under ``backend/models/``:
1. Subclass ``Base`` from :mod:`backend.database`.
2. Import the model class here in ``backend/models/__init__.py``.
3. Add the class name to ``__all__``.
"""

from backend.models.activity import ActivityRecord
from backend.models.ai_task import AiTask
from backend.models.pending_deletion import PendingDeletion
from backend.models.unified_file import UnifiedFileRecord

__all__: list[str] = ["ActivityRecord", "AiTask", "PendingDeletion", "UnifiedFileRecord"]
