variable "DOCKER_USERNAME" {}
variable "VERSION" {}

group "default" {
  targets = ["uwsgi", "daphne", "collectstatic"]
}

target "uwsgi" {
    context    = "./"
    dockerfile = "docker/uwsgi.Dockerfile"
    platforms  = ["linux/amd64", "linux/arm64"]
    tags = [
        "${DOCKER_USERNAME}/kwt-uwsgi:${VERSION}",
        "${DOCKER_USERNAME}/kwt-uwsgi:latest"
    ]
    args = {
        BUILDKIT_INLINE_CACHE = "1"
    }
    cache-from = [
        "
        type=s3,
        region=nl,
        bucket=kwt-docker-cache,
        name=kwt-uwsgi,
        endpoint_url=https://cache.butrosgroot.com,use_path_style=true,
        access_key_id=${MINIO_ACCESS_KEY},
        secret_access_key=${MINIO_SECRET_KEY}
        "
    ]
    cache-to = [
        "
        type=s3,
        region=nl,
        bucket=kwt-docker-cache,
        name=kwt-uwsgi,
        endpoint_url=https://cache.butrosgroot.com,
        mode=max,
        use_path_style=true,
        access_key_id=${MINIO_ACCESS_KEY},
        secret_access_key=${MINIO_SECRET_KEY}
        "
    ]
}

target "daphne" {
    context    = "./"
    dockerfile = "docker/daphne.Dockerfile"
    platforms  = ["linux/amd64", "linux/arm64"]
    tags = [
        "${DOCKER_USERNAME}/kwt-daphne:${VERSION}",
        "${DOCKER_USERNAME}/kwt-daphne:latest"
    ]
    args = {
        BUILDKIT_INLINE_CACHE = "1"
    }
    cache-from = [
        "
        type=s3,
        region=nl,
        bucket=kwt-docker-cache,
        name=kwt-daphne,
        endpoint_url=https://cache.butrosgroot.com,use_path_style=true,
        access_key_id=${MINIO_ACCESS_KEY},
        secret_access_key=${MINIO_SECRET_KEY}
        "
    ]
    cache-to = [
        "
        type=s3,
        region=nl,
        bucket=kwt-docker-cache,
        name=kwt-daphne,
        endpoint_url=https://cache.butrosgroot.com,
        mode=max,
        use_path_style=true,
        access_key_id=${MINIO_ACCESS_KEY},
        secret_access_key=${MINIO_SECRET_KEY}
        "
    ]
}

target "collectstatic" {
    context    = "./"
    dockerfile = "docker/collectstatic.Dockerfile"
    platforms  = ["linux/amd64", "linux/arm64"]
    tags = [
        "${DOCKER_USERNAME}/kwt-collectstatic:${VERSION}",
        "${DOCKER_USERNAME}/kwt-collectstatic:latest"
    ]
    args = {
        BUILDKIT_INLINE_CACHE = "1"
    }
    cache-from = [
        "
        type=s3,
        region=nl,
        bucket=kwt-docker-collectstatic,
        name=kwt-daphne,
        endpoint_url=https://cache.butrosgroot.com,use_path_style=true,
        access_key_id=${MINIO_ACCESS_KEY},
        secret_access_key=${MINIO_SECRET_KEY}
        "
    ]
    cache-to = [
        "
        type=s3,
        region=nl,
        bucket=kwt-docker-cache,
        name=kwt-collectstatic,
        endpoint_url=https://cache.butrosgroot.com,
        mode=max,
        use_path_style=true,
        access_key_id=${MINIO_ACCESS_KEY},
        secret_access_key=${MINIO_SECRET_KEY}
        "
    ]
}
