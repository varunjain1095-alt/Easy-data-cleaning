import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# QDC_DATA_DIR points at the mounted Railway Volume in production so sessions
# survive restarts/redeploys.
DATA_DIR = Path(os.environ.get("QDC_DATA_DIR", BASE_DIR / "data"))
SESSIONS_DIR = DATA_DIR / "sessions"
QUARANTINE_DIRNAME = "quarantine"

MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB, enforced during streaming
MAX_ROWS = 100_000                    # per selected sheet/table/item
SESSION_TTL_SECONDS = 3600            # 1 hour of inactivity
CHECKPOINT_INTERVAL = 10              # working-dataset checkpoint every N applied ops
PREVIEW_ROWS = 20
JOB_WORKERS = 4
UPLOAD_CHUNK_BYTES = 1024 * 1024      # 1 MB stream chunks

MISSING_MARKERS = {"na", "n/a", "null", "none", "nan", "-", "--", ""}
ROW_ID_COLUMN = "__qdc_row_id"
