variable "DOCKER_USERNAME" {}
variable "VERSION" {}

group "default" {
  targets = ["uwsgi", "daphne", "collectstatic"]
}

target "uwsgi" {
    context    = "apps/django_projects/korfbal"
    dockerfile = "docker/uwsgi.Dockerfile"
    platforms  = ["linux/amd64", "linux/arm64"]
    tags = [
        "${DOCKER_USERNAME}/kwt-uwsgi:${VERSION}",
        "${DOCKER_USERNAME}/kwt-uwsgi:latest"
    ]
    args = {
        BUILDKIT_INLINE_CACHE = "1"
    }
    cache-from = ["type=local,kwt-scope=uwsgi"]
    cache-to   = ["type=local,mode=max,kwt-scope=uwsgi,ttl=0"]
}

target "daphne" {
    context    = "apps/django_projects/korfbal"
    dockerfile = "docker/daphne.Dockerfile"
    platforms  = ["linux/amd64", "linux/arm64"]
    tags = [
        "${DOCKER_USERNAME}/kwt-daphne:${VERSION}",
        "${DOCKER_USERNAME}/kwt-daphne:latest"
    ]
    args = {
        BUILDKIT_INLINE_CACHE = "1"
    }
    cache-from = ["type=local,scope=kwt-daphne"]
    cache-to   = ["type=local,mode=max,scope=kwt-daphne,ttl=0"]
}

target "collectstatic" {
    context    = "apps/django_projects/korfbal"
    dockerfile = "docker/collectstatic.Dockerfile"
    platforms  = ["linux/amd64", "linux/arm64"]
    tags = [
        "${DOCKER_USERNAME}/kwt-collectstatic:${VERSION}",
        "${DOCKER_USERNAME}/kwt-collectstatic:latest"
    ]
    args = {
        BUILDKIT_INLINE_CACHE = "1"
    }
    cache-from = ["type=local,scope=kwt-collectstatic"]
    cache-to   = ["type=local,mode=max,scope=kwt-collectstatic,ttl=0"]
}
