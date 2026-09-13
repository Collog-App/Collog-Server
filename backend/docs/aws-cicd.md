# AWS CI/CD

GitHub Actions tests pull requests and pushes to `main` and `chore/CI-CD`.
After tests pass on `main`, it builds an amd64 image, publishes an immutable commit tag to ECR,
and runs `/opt/collog/bin/deploy.sh IMAGE_URI COMMIT` on EC2 through Systems Manager.
GitHub uses OIDC. EC2 uses its instance role for ECR and S3 access.

## AWS setup

Run from the repository root in AWS CloudShell with permissions to manage CloudFormation, IAM,
ECR and the target EC2 instance profile.

```bash
bash backend/scripts/configure_aws_cicd.sh
```

The script targets account `534545247934`, region `ap-northeast-2`, instance `i-0c3ec9275c7d3aa53`,
and existing bucket `collog-storage-main-534545247934-ap-northeast-2-an`.
It refuses to replace an unrelated instance profile. Existing GitHub OIDC providers are reused.
The trust policy matches this repository's immutable OIDC subject, including its owner and repository IDs.

Set repository Actions variables using the stack outputs.

| Variable | Value |
|---|---|
| `AWS_REGION` | `ap-northeast-2` |
| `ECR_REPOSITORY` | `collog-server` |
| `EC2_INSTANCE_ID` | `i-0c3ec9275c7d3aa53` |
| `AWS_DEPLOY_ROLE_ARN` | Stack output `DeployRoleArn` |

## EC2 setup

On Ubuntu 24.04 or 26.04 x86_64, copy both scripts to the same directory and run the bootstrap as root.

```bash
sudo bash bootstrap_ec2.sh
```

It installs Docker, AWS CLI and SSM Agent, adds a 2 GiB swap file if none exists,
and installs the deployment script and boot service. The server performs no image builds.

Store production settings in `/etc/collog/backend.env`, owned by root with mode `600`.
Use `deploy/production.env.example` and configure these values for this deployment.

```dotenv
API_DOMAIN=api.collog.live
LIVEKIT_DOMAIN=rtc.collog.live
S3_USE_INSTANCE_ROLE=true
S3_REGION=ap-northeast-2
S3_BUCKET=collog-storage-main-534545247934-ap-northeast-2-an
S3_ENDPOINT_URL=https://s3.ap-northeast-2.amazonaws.com
S3_PUBLIC_ENDPOINT_URL=https://s3.ap-northeast-2.amazonaws.com
APNS_ENVIRONMENT=production
QUESTION_TTS_PROVIDER=elevenlabs_direct
```

Leave S3 access and secret keys empty in instance-role mode.
Set the remaining provider credentials, independent DB/JWT/LiveKit secrets and `APNS_KEY_FILE`.
The APNs key must be readable by container UID `10001`.
Keep secrets on EC2. Do not put them in the repository, build arguments or workflow logs.

## Deployment

Merge into `main` after CI passes. The instance must be running and registered in Systems Manager.
When it is stopped, image publication can finish but deployment requires starting EC2 and rerunning the workflow.
The workflow does not start instances or expand SSH access.

Deployment downloads the image and extracts its matching Compose and Caddy files.
It refuses to proceed during calls or analysis, stops the API, checks again, and backs up PostgreSQL.
It then applies migrations, starts the application, and verifies its internal health endpoint.
Verify public HTTPS and a two-device call after the first deployment.

```bash
curl --fail https://api.collog.live/v1/health
```

On failure, the script attempts to restore the previous application image.
Database migrations are not reversed. Migrations must remain compatible with the previous application.
Backups stay in `/opt/collog/backups` with restricted permissions and require an operator retention policy.
ECR images are not automatically expired, so a running or rollback image stays available.

At boot, `collog.service` starts the last successful release in `/opt/collog/current`.
Before stopping EC2, let calls, uploads and analysis finish. EBS, S3 and Elastic IP charges can continue while stopped.
