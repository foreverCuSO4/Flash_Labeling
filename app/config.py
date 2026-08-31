from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__import__("os").environ.get("DATA_DIR", str(BASE_DIR / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "app.db"
SECRET_KEY = __import__("os").environ.get("SECRET_KEY", "dev-secret-change-me-in-production")
SESSION_COOKIE = "yololabel_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
