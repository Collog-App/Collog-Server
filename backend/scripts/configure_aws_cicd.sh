#!/usr/bin/env bash
set -euo pipefail
export AWS_PAGER=""
export AWS_REGION=ap-northeast-2
export AWS_DEFAULT_REGION="$AWS_REGION"

account_id=534545247934
instance_id=i-0c3ec9275c7d3aa53
bucket=collog-storage-main-534545247934-ap-northeast-2-an
stack_name=collog-cicd
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
template="$script_dir/../deploy/aws-cicd.yml"

actual_account=$(aws sts get-caller-identity --query Account --output text)
if [[ "$actual_account" != "$account_id" ]]; then
  echo "Expected AWS account $account_id, received $actual_account" >&2
  exit 1
fi

aws ec2 describe-instances --instance-ids "$instance_id" \
  --query 'Reservations[0].Instances[0].[InstanceId,State.Name]' --output table
bucket_region=$(aws s3api get-bucket-location --bucket "$bucket" \
  --expected-bucket-owner "$account_id" --query LocationConstraint --output text)
if [[ "$bucket_region" != "$AWS_REGION" ]]; then
  echo "Bucket must be owned by $account_id in $AWS_REGION" >&2
  exit 1
fi

existing_profile=$(aws ec2 describe-iam-instance-profile-associations \
  --filters "Name=instance-id,Values=$instance_id" \
  --query "IamInstanceProfileAssociations[?State!='disassociated'].IamInstanceProfile.Arn | [0]" --output text)
existing_stack_profile=None
stack_status=$(aws cloudformation list-stacks \
  --query "StackSummaries[?StackName=='$stack_name' && StackStatus!='DELETE_COMPLETE'].StackStatus | [0]" \
  --output text)
if [[ "$stack_status" != None ]]; then
  existing_stack_profile=$(aws cloudformation describe-stacks --stack-name "$stack_name" \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceProfileArn'].OutputValue | [0]" --output text)
fi
if [[ "$existing_profile" != None && "$existing_profile" != "$existing_stack_profile" ]]; then
  echo "The instance already has another IAM profile. Review it before running this script." >&2
  echo "$existing_profile" >&2
  exit 1
fi

oidc_arn="arn:aws:iam::$account_id:oidc-provider/token.actions.githubusercontent.com"
existing_oidc=$(aws iam list-open-id-connect-providers \
  --query "OpenIDConnectProviderList[?Arn=='$oidc_arn'].Arn | [0]" --output text)
owned_oidc=None
if [[ "$stack_status" != None ]]; then
  owned_oidc=$(aws cloudformation list-stack-resources --stack-name "$stack_name" \
    --query "StackResourceSummaries[?LogicalResourceId=='GitHubOidcProvider'].PhysicalResourceId | [0]" \
    --output text)
fi
if [[ "$existing_oidc" == None || "$existing_oidc" == "$owned_oidc" ]]; then
  existing_oidc=""
else
  valid_audience=$(aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$existing_oidc" \
    --query "contains(ClientIDList, 'sts.amazonaws.com')" --output text)
  if [[ "$valid_audience" != True ]]; then
    echo "Existing GitHub OIDC provider needs the sts.amazonaws.com audience. Review its settings." >&2
    exit 1
  fi
fi

aws cloudformation deploy --stack-name "$stack_name" --template-file "$template" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides "InstanceId=$instance_id" "StorageBucket=$bucket" \
    "ExistingOidcProviderArn=$existing_oidc"

profile_name=$(aws cloudformation describe-stacks --stack-name "$stack_name" \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceProfileName'].OutputValue | [0]" --output text)
if [[ "$existing_profile" == None ]]; then
  aws ec2 associate-iam-instance-profile --instance-id "$instance_id" \
    --iam-instance-profile "Name=$profile_name" --query IamInstanceProfileAssociation.State --output text
fi

aws cloudformation describe-stacks --stack-name "$stack_name" \
  --query 'Stacks[0].Outputs' --output table
echo "GitHub repository variables"
echo "AWS_REGION=$AWS_REGION"
echo "EC2_INSTANCE_ID=$instance_id"
echo "ECR_REPOSITORY=collog-server"
deploy_role=$(aws cloudformation describe-stacks --stack-name "$stack_name" \
  --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue | [0]" --output text)
echo "AWS_DEPLOY_ROLE_ARN=$deploy_role"
echo "The EC2 SSM agent must be running before deployment."
