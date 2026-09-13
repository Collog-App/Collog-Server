#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ $(id -u) == 0 ]] || { echo 'Run as root'; exit 1; }
[[ $# == 2 ]] || { echo 'Usage deploy.sh IMAGE_URI COMMIT'; exit 1; }
source /etc/collog/deploy.conf
image_uri=$1
revision=$2
registry="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || exit 1
[[ "$image_uri" == "$registry/$ECR_REPOSITORY:$revision" ]] || exit 1
[[ -s /etc/collog/backend.env ]] || { echo 'Missing backend.env'; exit 1; }

exec 9>/run/lock/collog-deploy.lock
flock -n 9 || { echo 'Another deployment is running'; exit 1; }
root=/opt/collog
release="$root/releases/$revision"
previous=$(readlink -f "$root/current" || true)
container_id=''
backend_stopped=false
deployment_started=false

compose() {
    local directory=$1
    shift
    docker compose --project-name collog-production --env-file /etc/collog/backend.env \
        --env-file "$directory/image.env" -f "$directory/docker-compose.yml" "$@"
}

cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$container_id" ]]; then docker rm -f "$container_id" >/dev/null 2>&1 || true; fi
    docker logout "$registry" >/dev/null 2>&1 || true
    if (( status != 0 )) && [[ -n "$previous" && -d "$previous" ]]; then
        if [[ "$deployment_started" == true ]]; then
            echo 'Deployment failed, restoring the previous application image'
            compose "$previous" up -d --no-build --no-deps --wait --wait-timeout 180 backend caddy || true
        elif [[ "$backend_stopped" == true ]]; then
            compose "$previous" start backend || true
        fi
    fi
    exit "$status"
}
trap cleanup EXIT

aws ecr get-login-password --region "$AWS_REGION" |
    docker login --username AWS --password-stdin "$registry"
docker pull "$image_uri"
mkdir -p "$release" "$root/backups"
container_id=$(docker create "$image_uri")
docker cp "$container_id:/app/deploy-bundle/." "$release/"
docker rm "$container_id" >/dev/null
container_id=''
printf 'BACKEND_IMAGE=%s\n' "$image_uri" > "$release/image.env"
compose "$release" config --quiet

busy_sql="SELECT count(*) FROM calls WHERE state IN ('CREATED','RINGING','ACTIVE','PROCESSING') "
busy_sql+="OR (state = 'ENDED' AND (recording_enabled OR ended_at IS NULL));"
if [[ -n "$previous" && -d "$previous" ]]; then
    busy=$(compose "$previous" exec -T postgres psql -U collog -d collog -tAc "$busy_sql")
    [[ "$busy" == 0 ]] || { echo 'Calls or analysis are active, retry deployment later'; exit 1; }
    compose "$previous" stop backend
    backend_stopped=true
    busy=$(compose "$previous" exec -T postgres psql -U collog -d collog -tAc "$busy_sql")
    [[ "$busy" == 0 ]] || { echo 'A call started before shutdown, restoring service'; exit 1; }
    backup="$root/backups/$(date -u +%Y%m%dT%H%M%SZ)-$revision.dump"
    compose "$previous" exec -T postgres pg_dump -U collog -d collog -Fc > "$backup"
    [[ -s "$backup" ]] || { echo 'Database backup is empty'; exit 1; }
fi

deployment_started=true
compose "$release" up -d --no-build --wait --wait-timeout 180 postgres redis livekit egress
compose "$release" run --rm --no-deps migrate
compose "$release" up -d --no-build --no-deps --wait --wait-timeout 180 backend caddy
compose "$release" exec -T backend python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8080/v1/health', timeout=10)"
ln -sfn "$release" "$root/current.next"
mv -Tf "$root/current.next" "$root/current"
echo "Deployed $revision"
