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
    cache-from = ["type=local,src=./.buildx-cache"]
    cache-to   = ["type=local,src=./.buildx-cache,mode=max"]
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
    cache-from = ["type=local,src=./.buildx-cache"]
    cache-to   = ["type=local,src=./.buildx-cache,mode=max"]
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
    cache-from = ["type=local,src=./.buildx-cache"]
    cache-to   = ["type=local,src=./.buildx-cache,mode=max"]
}
