"""Start one ephemeral Fargate runner per queued GitHub Actions job.

GitHub delivers a ``workflow_job`` webhook the moment a job is queued, to this
function's own URL. The handler verifies the delivery, mints a just-in-time (JIT)
runner configuration scoped to that single job, and launches a Fargate task
carrying it.

The endpoint is unauthenticated at the AWS layer because GitHub cannot sign with
SigV4. Authentication is the HMAC check below, which runs before anything that
costs money or touches GitHub. The runner
picks up the job, runs it, deregisters itself and exits, which stops the task.

Nothing is running -- and nothing is billed for compute -- between jobs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
import uuid

import boto3


logger = logging.getLogger()
logger.setLevel(logging.INFO)

GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
RUNNER_LABEL = os.environ["RUNNER_LABEL"]
CLUSTER = os.environ["ECS_CLUSTER"]
TASK_DEFINITION = os.environ["ECS_TASK_DEFINITION"]
CONTAINER_NAME = os.environ["ECS_CONTAINER_NAME"]
SUBNETS = [subnet for subnet in os.environ["SUBNET_IDS"].split(",") if subnet]
SECURITY_GROUPS = [group for group in os.environ["SECURITY_GROUP_IDS"].split(",") if group]
CAPACITY_PROVIDER = os.environ.get("CAPACITY_PROVIDER", "").strip()
WEBHOOK_SECRET_ARN = os.environ["WEBHOOK_SECRET_ARN"]
GITHUB_TOKEN_SECRET_ARN = os.environ["GITHUB_TOKEN_SECRET_ARN"]

ecs = boto3.client("ecs")
secretsmanager = boto3.client("secretsmanager")

# Lambda keeps these for the life of the execution environment; rotating a secret
# takes effect on the next cold start, or sooner if the container is recycled.
_secret_cache: dict[str, str] = {}


def _secret(arn: str) -> str:
    if arn not in _secret_cache:
        _secret_cache[arn] = secretsmanager.get_secret_value(SecretId=arn)["SecretString"].strip()
    return _secret_cache[arn]


def _response(status: int, message: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"message": message}),
    }


def _body_bytes(event: dict) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _signature_is_valid(body: bytes, header: str | None) -> bool:
    """Constant-time check of GitHub's HMAC-SHA256 delivery signature."""
    if not header:
        return False
    expected = "sha256=" + hmac.new(_secret(WEBHOOK_SECRET_ARN).encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _headers_lower(event: dict) -> dict:
    return {key.lower(): value for key, value in (event.get("headers") or {}).items()}


def _generate_jit_config(runner_name: str) -> str:
    """Ask GitHub for a single-use runner configuration bound to this repository."""
    payload = json.dumps(
        {
            "name": runner_name,
            "runner_group_id": 1,
            # GitHub adds the self-hosted/Linux/X64 defaults on top of these.
            "labels": [RUNNER_LABEL],
            "work_folder": "_work",
        }
    ).encode()
    request = urllib.request.Request(
        f"{GITHUB_API_URL}/repos/{GITHUB_REPOSITORY}/actions/runners/generate-jitconfig",
        data=payload,
        method="POST",
        headers={
            "accept": "application/vnd.github+json",
            "authorization": f"Bearer {_secret(GITHUB_TOKEN_SECRET_ARN)}",
            "content-type": "application/json",
            "user-agent": "dot-oracle-runner-dispatcher",
            "x-github-api-version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["encoded_jit_config"]


def _run_task(jit_config: str, job_id: int) -> str:
    kwargs = {
        "cluster": CLUSTER,
        "taskDefinition": TASK_DEFINITION,
        "count": 1,
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": SUBNETS,
                "securityGroups": SECURITY_GROUPS,
                # The RDS subnets route to an internet gateway, so a public IP is what
                # gives the task egress to GitHub without paying for a NAT gateway.
                "assignPublicIp": "ENABLED",
            }
        },
        "overrides": {
            "containerOverrides": [
                {
                    "name": CONTAINER_NAME,
                    "environment": [
                        {"name": "ACTIONS_RUNNER_INPUT_JITCONFIG", "value": jit_config},
                        {"name": "GITHUB_JOB_ID", "value": str(job_id)},
                    ],
                }
            ]
        },
    }
    if CAPACITY_PROVIDER:
        kwargs["capacityProviderStrategy"] = [{"capacityProvider": CAPACITY_PROVIDER, "weight": 1}]
    else:
        kwargs["launchType"] = "FARGATE"

    result = ecs.run_task(**kwargs)
    for failure in result.get("failures", []):
        raise RuntimeError(f"RunTask failed: {failure.get('reason')} ({failure.get('detail')})")
    return result["tasks"][0]["taskArn"]


def handler(event: dict, context: object) -> dict:
    # A Lambda function URL has no routing in front of it, so every path and method
    # arrives here. GitHub only ever POSTs; anything else is noise off the internet.
    method = event.get("requestContext", {}).get("http", {}).get("method")
    if method and method != "POST":
        return _response(405, "method not allowed")

    body = _body_bytes(event)
    headers = _headers_lower(event)

    if not _signature_is_valid(body, headers.get("x-hub-signature-256")):
        # Do not say which part failed; this endpoint is on the public internet.
        logger.warning("Rejected a delivery with a bad or missing signature.")
        return _response(401, "invalid signature")

    github_event = headers.get("x-github-event")
    if github_event == "ping":
        return _response(200, "pong")
    if github_event != "workflow_job":
        return _response(200, f"ignoring {github_event} event")

    payload = json.loads(body)
    if payload.get("action") != "queued":
        return _response(200, f"ignoring workflow_job.{payload.get('action')}")

    job = payload.get("workflow_job", {})
    labels = job.get("labels", [])
    if RUNNER_LABEL not in labels:
        return _response(200, f"job does not target {RUNNER_LABEL}")

    repository = payload.get("repository", {}).get("full_name")
    if repository != GITHUB_REPOSITORY:
        # A webhook secret is shared with whoever configured the hook; refuse to
        # launch runners on behalf of any repository but the configured one.
        logger.warning("Refusing a job from unexpected repository %s.", repository)
        return _response(403, "unexpected repository")

    runner_name = f"dot-oracle-{uuid.uuid4().hex[:12]}"
    try:
        jit_config = _generate_jit_config(runner_name)
    except urllib.error.HTTPError as error:
        logger.error("generate-jitconfig failed: %s %s", error.code, error.read()[:500])
        return _response(502, "could not generate a runner configuration")

    task_arn = _run_task(jit_config, job.get("id", 0))
    logger.info("Started %s as %s for job %s.", task_arn, runner_name, job.get("id"))
    return _response(202, f"started {task_arn}")
