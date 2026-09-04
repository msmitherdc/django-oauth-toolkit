#!/usr/bin/env bash
#
# Run exactly one GitHub Actions job, then exit so the Fargate task stops.
set -euo pipefail

if [ -z "${ACTIONS_RUNNER_INPUT_JITCONFIG:-}" ]; then
    echo "ACTIONS_RUNNER_INPUT_JITCONFIG is not set. This container is started by the" >&2
    echo "webhook Lambda, which supplies a just-in-time runner configuration." >&2
    exit 64
fi

# A runner whose job was already claimed by a redelivered webhook would otherwise
# wait forever, billing Fargate the whole time. Cap the task's lifetime; keep this
# comfortably above the workflow's own timeout-minutes.
: "${RUNNER_MAX_SECONDS:=7200}"

cd /home/runner/actions-runner

# The JIT configuration is single-use: the runner registers, takes one job,
# deregisters and returns. Nothing is written back to the image.
exec timeout --signal=SIGTERM --kill-after=60s "${RUNNER_MAX_SECONDS}" \
    ./run.sh --jitconfig "${ACTIONS_RUNNER_INPUT_JITCONFIG}"
