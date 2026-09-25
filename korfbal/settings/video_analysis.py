"""Private local storage shared by native review API and dedicated workers."""

from .env import env, env_bool
from .storage import (
    AWS_MEDIA_BUCKET_NAME,
    AWS_S3_ENDPOINT_URL,
    KORFBAL_MEDIA_S3_ENDPOINT_URL,
)


VIDEO_ANALYSIS_ROOT = env("VIDEO_ANALYSIS_ROOT", "/var/lib/korfbal/video-analysis")

VIDEO_ANALYSIS_PYTHON = env("VIDEO_ANALYSIS_PYTHON", "/opt/vision/bin/python")
VIDEO_ANALYSIS_TRAINING_POLICY = env("VIDEO_ANALYSIS_TRAINING_POLICY", "")
VIDEO_ANALYSIS_OBJECT_STORAGE = env_bool("VIDEO_ANALYSIS_OBJECT_STORAGE", False)
_default_media_bucket = (
    AWS_MEDIA_BUCKET_NAME
    if KORFBAL_MEDIA_S3_ENDPOINT_URL != AWS_S3_ENDPOINT_URL
    else "korfbal-video-media"
)
_default_artifact_bucket = (
    AWS_MEDIA_BUCKET_NAME
    if KORFBAL_MEDIA_S3_ENDPOINT_URL != AWS_S3_ENDPOINT_URL
    else "korfbal-video-artifacts"
)
VIDEO_ANALYSIS_MEDIA_BUCKET = env("VIDEO_ANALYSIS_MEDIA_BUCKET", _default_media_bucket)
VIDEO_ANALYSIS_ARTIFACT_BUCKET = env(
    "VIDEO_ANALYSIS_ARTIFACT_BUCKET", _default_artifact_bucket
)

# A public download can be 6 GB and is copied into immutable workspace media.
VIDEO_ANALYSIS_PIPELINE_IMPORT_FREE_BYTES = int(
    env("VIDEO_ANALYSIS_PIPELINE_IMPORT_FREE_BYTES", "13000000000")
)
VIDEO_ANALYSIS_PIPELINE_WORK_FREE_BYTES = int(
    env("VIDEO_ANALYSIS_PIPELINE_WORK_FREE_BYTES", "2000000000")
)
