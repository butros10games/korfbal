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
    cache-from = ["type=gha,kwt-scope=uwsgi"]
    cache-to   = ["type=gha,mode=max,kwt-scope=uwsgi,ttl=0"]
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
    cache-from = ["type=gha,scope=kwt-daphne"]
    cache-to   = ["type=gha,mode=max,scope=kwt-daphne,ttl=0"]
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
    cache-from = ["type=gha,scope=kwt-collectstatic"]
    cache-to   = ["type=gha,mode=max,scope=kwt-collectstatic,ttl=0"]
}
