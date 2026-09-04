# Ephemeral Oracle CI runner

The `oracle-standalone` job in [`.github/workflows/test.yml`](../../.github/workflows/test.yml)
runs the test suite against a real Oracle Database rather than a container. The
instance is not publicly reachable, so the job cannot run on a GitHub-hosted runner. This directory holds
everything needed to stand up a self-hosted runner that lives inside the VPC and
exists only while a job is running.

This directory is deployment-specific and is not part of the upstream project. It
deliberately carries no account IDs, endpoints, VPC IDs or usernames: it lives in a
public fork, and those are reconnaissance material even though none of them is a
credential. Every command below reads them from the shell variables set here, so
export them once — and keep them out of any file you commit:

```bash
export REGION=us-east-1
export CI_REPOSITORY=<owner/repo the runner serves>
export DB_INSTANCE=<rds instance identifier>
export MASTER_USER=<rds master username>
export ORACLE_DSN=<endpoint>:1521/<PDB service name>
# Filled in from the describe-db-instances call under "Deploy the stack".
export VPC_ID= SUBNET_A= SUBNET_B= SUBNET_C= RDS_SECURITY_GROUP=
```

The workflow it serves is upstreamable on its own: the Oracle job is skipped
wherever the `ORACLE_CI_ENABLED` repository variable is unset, so upstream and
every other fork are unaffected by its presence.

```
GitHub: job queued
   │
   │  workflow_job webhook, HMAC-signed
   ▼
Lambda function URL ──► dispatcher
                            │  1. verify the signature
                            │  2. POST /actions/runners/generate-jitconfig
                            │  3. ecs:RunTask
                            ▼
                        Fargate task, in the RDS VPC
                            │  runs one job
                            │  ├── 1521 ─► Oracle RDS
                            │  └──  443 ─► GitHub, PyPI (via the subnet's IGW)
                            ▼
                        task exits, billing stops
```

## Why a function URL and not an API Gateway

The dispatcher needs one HTTPS endpoint, in front of one function, with no
routing, no request transformation and no authorizer — GitHub authenticates
itself by signing the body. That is precisely what a Lambda function URL is, so
the stack uses one and adds no API Gateway to the account.

The endpoint is `AuthType: NONE`, because GitHub cannot sign requests with SigV4.
It is therefore reachable by anyone who learns the URL, and worth knowing what
that does and does not mean:

- The dispatcher verifies GitHub's HMAC signature before it does anything that
  costs money or touches the GitHub API. An unsigned request gets a 401 and
  nothing else happens.
- `DispatcherMaxConcurrency` (reserved concurrency, default 5) bounds what a
  flood of unsigned requests can cost. Real webhook traffic never approaches it.
- A function URL cannot have an AWS WAF attached, where an API Gateway can. If
  you later want WAF rules, IP allow-listing against
  [GitHub's `hooks` ranges](https://api.github.com/meta), or usage plans, that is
  the reason to move to an API Gateway — not this deployment.

### Removing the public endpoint entirely

If you would rather have no internet-facing endpoint at all, drop the webhook and
have the workflow start its own runner: add a job on `ubuntu-latest` that assumes
an AWS role via GitHub's OIDC provider (`aws-actions/configure-aws-credentials`,
no stored AWS keys) and calls `lambda:InvokeFunction` on the dispatcher directly.
The dispatcher keeps minting the JIT config and running the task, so the GitHub
token still never leaves Lambda, and `AWS::Lambda::Url` and its permission come
out of the template.

The cost is in the workflow rather than the account: that job has to know how many
runners to start, and a runner started for a cell that then gets skipped idles
until `RUNNER_MAX_SECONDS`. The webhook has neither problem, since GitHub tells
you exactly which job queued.

## Why Fargate and not Lambda for the runner

The dispatcher *is* a Lambda — it is a few hundred milliseconds of work per
webhook, which is exactly what Lambda is for. The runner itself cannot be:

- **Lambda's hard 15-minute execution limit.** The Oracle job runs the suite
  single-process (see the `-n0` note in `tox.ini`) against a database on the other
  end of a network hop. It will not finish in 15 minutes.
- **The runner is a long-poll client, not a request handler.** It registers,
  waits to be handed a job, streams logs back, then deregisters. Lambda's
  invoke-and-return model fights that shape at every step.

Fargate keeps the property you actually wanted: nothing runs and nothing is
billed for compute between jobs. See [Cost](#cost) for the small fixed charges
that remain.

## What you need first

| Thing | Where | Notes |
| --- | --- | --- |
| ECR repository | AWS | Holds the runner image built here. |
| Webhook secret | Secrets Manager | Any high-entropy string; GitHub signs deliveries with it. |
| Runner registration token | Secrets Manager | Fine-grained PAT, see below. |
| Admin on the repository | GitHub | Needed to add a self-hosted runner, a webhook and an environment. |

### The CI identity

The token and the webhook belong to **`msmitherdc-actions`**, not to a personal
account, so that CI's access to the repository is separate from anyone's day-to-day
account and can be revoked on its own.

1. Sign in as `msmitherdc-actions` and have it granted **Admin** on the repository
   this stack serves.
2. Create a **fine-grained personal access token** owned by `msmitherdc-actions`,
   scoped to that one repository, with a single permission:
   **Repository permissions → Administration → Read and write**. That is the
   permission `generate-jitconfig` requires and nothing more; the token cannot read
   code, write to the repo, or reach any other repository.
3. Set an expiry you will actually rotate against, and store the token:

   ```bash
   aws secretsmanager create-secret --region us-east-1 \
       --name dot-oracle-runner/github-token \
       --secret-string 'github_pat_...'

   aws secretsmanager create-secret --region us-east-1 \
       --name dot-oracle-runner/webhook-secret \
       --secret-string "$(openssl rand -hex 32)"
   ```

A GitHub App owned by `msmitherdc-actions` is the stronger option — short-lived
installation tokens instead of a long-lived PAT — but it makes the dispatcher sign
a JWT, which pulls a cryptography dependency into what is currently a
standard-library-only Lambda. The PAT keeps this deployable from one template with
no build step. Swap it in later by replacing `_secret(GITHUB_TOKEN_SECRET_ARN)` in
`lambda/webhook_dispatcher.py` with an installation-token exchange.

## Deploy

### 1. Build and push the runner image

```bash
REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REPO=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/dot-oracle-runner

aws ecr create-repository --repository-name dot-oracle-runner --region "$REGION"
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REPO

# --platform is required when building from an Apple Silicon machine: the task
# definition runs X86_64.
docker build --platform linux/amd64 -t $REPO:1 runner/
docker push $REPO:1
```

Pin `--build-arg RUNNER_VERSION=2.x.y` for a reproducible image; the default
resolves the latest `actions/runner` release at build time.

### 2. Deploy the stack

First read the VPC, subnets and security group off the database, and fill in the
variables from the top of this file:

```bash
aws rds describe-db-instances --db-instance-identifier "$DB_INSTANCE" --region "$REGION" \
    --query 'DBInstances[0].{VPC:DBSubnetGroup.VpcId,SG:VpcSecurityGroups,Subnets:DBSubnetGroup.Subnets[].SubnetIdentifier,Endpoint:Endpoint.Address}'
```

Pick subnets in at least two availability zones. `Code: lambda/` is a local path,
so the template is packaged before it is deployed; any S3 bucket in the region
works.

```bash
aws cloudformation package \
    --template-file cloudformation.yaml \
    --s3-bucket <your-artifact-bucket> \
    --s3-prefix dot-oracle-runner \
    --output-template-file .packaged.yaml

aws cloudformation deploy \
    --region "$REGION" \
    --template-file .packaged.yaml \
    --stack-name dot-oracle-runner \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        GitHubRepository=$CI_REPOSITORY \
        RunnerImageUri=$REPO:1 \
        VpcId=$VPC_ID \
        SubnetIds=$SUBNET_A\\,$SUBNET_B\\,$SUBNET_C \
        RdsSecurityGroupId=$RDS_SECURITY_GROUP \
        WebhookSecretArn=<arn> \
        GitHubTokenSecretArn=<arn>
```

The stack adds exactly one rule to that security group: TCP 1521 from the runner
security group. It is removed again when the stack is deleted.

The `WebhookUrl` output is the function URL, and the payload URL for the next
step. It looks like `https://<id>.lambda-url.us-east-1.on.aws/`.

### 3. Point GitHub at it

On the repository, as `msmitherdc-actions`:

- **Settings → Webhooks → Add webhook**
  - Payload URL: the `WebhookUrl` stack output
  - Content type: `application/json`
  - Secret: the webhook secret from Secrets Manager
  - Events: **Let me select individual events → Workflow jobs** only

- **Settings → Environments → New environment → `oracle-ci`**
  - Required reviewers: whoever should vouch for a fork's code before it runs
    next to a database. This is the second half of the fork gate; the first is the
    `oracle-ci` label the workflow checks.
  - Environment secrets:

    | Secret | Value |
    | --- | --- |
    | `ORACLE_DSN` | `<endpoint>:1521/<PDB service name>` |
    | `ORACLE_PASSWORD` | the password from the schema bootstrap |

  Put them on the environment, not the repository, so they are unreachable from
  every other job in the workflow.

  > **Required reviewers apply to every run, not just pull requests.** With one
  > configured, a push to `master` also waits for approval before the Oracle job
  > starts. That is usually what you want on a shared database, but if you would
  > rather have `master` run unattended, leave required reviewers off and let the
  > `oracle-ci` label be the whole gate for pull requests. Making the environment
  > itself conditional is not an option: the database secrets live on it, so a job
  > that skips the environment cannot connect.

- **Settings → Labels → New label → `oracle-ci`**

- **Settings → Secrets and variables → Actions → Variables → New variable**
  - `ORACLE_CI_ENABLED` = `true`

  The workflow's Oracle job is skipped unless this is set, so that a fork without a
  runner does not queue a job nothing can pick up. This variable is what turns it on
  for the repository the runner serves.

### 4. Create the database schemas

```bash
sqlplus "$MASTER_USER"@"$ORACLE_DSN" @sql/bootstrap_ci_schemas.sql
```

The service name in `ORACLE_DSN` must be the *pluggable* database's. On an
`oracle-ee-cdb` instance a CDB root connection will not find these schemas; the PDB
service name is the `DBName` reported by `describe-db-instances`.

## Running the job

The Oracle job never runs automatically on a pull request. Reach it by:

- pushing to `master`, or
- running the workflow manually (**Actions → Test → Run workflow**), or
- adding the `oracle-ci` label to a pull request, then approving the `oracle-ci`
  environment when GitHub asks.

Locally, against any Oracle you can reach:

```bash
export ORACLE_DSN=localhost:1521/FREEPDB1 ORACLE_USER=dot_ci_dj52 ORACLE_PASSWORD=...
tox -e py312-dj52-ora21
tox -e migrations-dj52-ora21
tox -e oracle-reset          # drop everything the run left behind
```

## Cost

There is no compute charge between jobs: no instance, no NAT gateway, no idle
task. What does bill continuously is small and fixed:

| Item | Roughly |
| --- | --- |
| Secrets Manager, 2 secrets | $0.80 / month |
| ECR storage, ~1 GB image | $0.10 / month |
| CloudWatch Logs | cents, at the configured retention |
| Lambda | per webhook invocation; far inside the free tier |

Per job you pay Fargate for the task's lifetime — at the default 2 vCPU / 4 GB,
about $0.10 per hour of job. `CapacityProvider=FARGATE_SPOT` cuts that by roughly
70% at the risk of the task being reclaimed mid-run.

The runner tasks are given public IPs, which is what lets them reach GitHub over
the subnet's internet gateway. Moving them to private subnets would need a NAT
gateway at about $32/month whether or not anything is running — the one choice
here that would break the no-idle-cost property.

## Security notes

- **A self-hosted runner will execute whatever is in the branch it builds.** On a
  public repository that is the whole risk: an attacker opens a pull request, and
  their code runs inside your VPC with the environment's database credentials. The
  label plus the environment's required reviewer is what stands between a fork and
  the database, and neither is optional. Never move this job to `pull_request_target`.
- Each task is **single-use**. The JIT configuration is valid for one job, and the
  container is discarded afterwards, so nothing an untrusted job writes to disk can
  reach the next one.
- The runner's IAM task role is **empty**. A job that escapes into the AWS API
  finds no permissions; the database credentials arrive from GitHub, not from this
  account.
- The CI schemas hold no `ANY` privileges and cannot reach the Jenkins schema. See
  the grant list in `sql/bootstrap_ci_schemas.sql`.
- The dispatcher rejects deliveries whose HMAC does not verify, and refuses jobs
  from any repository but the configured one.

## Troubleshooting

**A job sits queued forever.** The webhook never arrived or was rejected. Check
the delivery in **Settings → Webhooks → Recent Deliveries** and the dispatcher's
log group, `/aws/lambda/dot-oracle-runner-dispatcher`.

**The task starts and dies immediately.** Read `/aws/ecs/dot-oracle-runner-runner`.
A `Bad credentials` from `generate-jitconfig` means the PAT expired or lost its
Administration permission; a missing `ACTIONS_RUNNER_INPUT_JITCONFIG` means
something started the task outside the dispatcher.

**The tests cannot reach the database.** Confirm the ingress rule survived on the
RDS security group, and that the subnets you passed are ones the instance is
actually reachable in.

**A cancelled run left a schema half-migrated.** `tox -e oracle-reset` with that
schema's credentials empties it; the next run would do this anyway before starting.

## Teardown

```bash
aws cloudformation delete-stack --stack-name dot-oracle-runner --region us-east-1
```

Then delete the ECR repository, the two secrets, the GitHub webhook, the
`oracle-ci` environment, and — if you want the instance back as it was — the three
schemas (the teardown statements are at the bottom of the bootstrap SQL).
