"""Private local storage shared by native review API and dedicated workers."""

from .env import env, env_bool


VIDEO_ANALYSIS_ROOT = env("VIDEO_ANALYSIS_ROOT", "/var/lib/korfbal/video-analysis")

VIDEO_ANALYSIS_PYTHON = env("VIDEO_ANALYSIS_PYTHON", "/opt/vision/bin/python")
VIDEO_ANALYSIS_TRAINING_POLICY = env("VIDEO_ANALYSIS_TRAINING_POLICY", "")
VIDEO_ANALYSIS_OBJECT_STORAGE = env_bool("VIDEO_ANALYSIS_OBJECT_STORAGE", False)
VIDEO_ANALYSIS_MEDIA_BUCKET = env("VIDEO_ANALYSIS_MEDIA_BUCKET", "korfbal-video-media")
VIDEO_ANALYSIS_ARTIFACT_BUCKET = env(
    "VIDEO_ANALYSIS_ARTIFACT_BUCKET", "korfbal-video-artifacts"
)
