from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    name: str
    password_hash: str
    avatar: Optional[str] = Field(default=None)  # stored filename under AVATAR_DIR; None = default avatar
    created_at: datetime = Field(default_factory=utcnow)


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    owner_id: int = Field(foreign_key="user.id")
    mode: str = Field(default="detection")  # detection | pose
    guidelines: str = Field(default="")  # annotation guidelines, Markdown
    keypoints: str = Field(default="[]")  # JSON list of keypoint names (pose mode)
    skeleton: str = Field(default="[]")  # JSON list of [i, j] edges (pose mode)
    created_at: datetime = Field(default_factory=utcnow)


class ProjectClass(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    name: str
    description: str = Field(default="")  # what this class means semantically
    ord: int  # YOLO class id


class ProjectMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    role: str = "annotator"  # owner | annotator


class Image(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    filename: str
    stored_name: str  # unique name on disk
    width: int
    height: int
    status: str = Field(default="unlabeled", index=True)  # unlabeled | labeled
    claimed_by: Optional[int] = Field(default=None, foreign_key="user.id")
    claimed_at: Optional[datetime] = None
    labeled_by: Optional[int] = Field(default=None, foreign_key="user.id")
    uploaded_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)


class VideoJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    filename: str  # original video filename
    stored_name: str  # unique name on disk under VIDEO_DIR/<project_id>/; videos are kept
    status: str = Field(default="pending", index=True)  # pending | running | done | failed | cancelled
    cancel_requested: bool = False
    progress: float = 0.0  # 0..1 decode progress (stays 0 when total_frames is unknown)
    params: str = Field(default="{}")  # JSON sampling params (see app/video.py)
    fps: float = 0.0
    total_frames: int = 0  # container-reported count (0 = unknown until finished)
    decoded_frames: int = 0
    extracted_frames: int = 0
    error: Optional[str] = None
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)


class Annotation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    image_id: int = Field(foreign_key="image.id", index=True)
    class_id: int = Field(foreign_key="projectclass.id")
    x: float  # normalized center x
    y: float  # normalized center y
    w: float  # normalized width
    h: float  # normalized height
    keypoints: Optional[str] = None  # JSON [{"x","y","v"},...] for pose mode, None for detection
    polygon: Optional[str] = None  # JSON [[x,y],...] normalized vertices for segment mode, else None
    created_by: int = Field(foreign_key="user.id")
    updated_at: datetime = Field(default_factory=utcnow)
