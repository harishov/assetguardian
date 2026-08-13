"""
Provision the AssetGuardian AI admin account in the Cognito User Pool.

Uses AdminCreateUser + AdminSetUserPassword (permanent) + AdminAddUserToGroup
("Admins") so the account:
  - bypasses the @ncs.com.sg self-signup domain restriction (that Pre-SignUp
    Lambda trigger only fires on the public `SignUp` API, not `AdminCreateUser`)
  - never hits the NEW_PASSWORD_REQUIRED forced-reset challenge on first login,
    since the password is set as Permanent=True
  - is actually authorized as an admin: membership in the "Admins" User Pool
    Group is what the Identity Pool's role-mapping rule checks (the
    `cognito:groups` claim in the ID token) to grant the elevated AdminRole
    (S3 upload/API invoke + CloudWatch Logs read for the portal's Event Logs
    view). This is real IAM-enforced authorization, not a client-side check.

Usage:
    python3 scripts/create_admin_user.py <admin-email> <password> [user_pool_id]

If user_pool_id is omitted, it's read from the CloudFormation stack output.
"""
import os
import sys
import boto3

# us-east-1 was decommissioned 2026-08-01; ap-southeast-1 is the sole live
# deployment. Both are still overridable by env var.
REGION = os.environ.get("ADMIN_USER_REGION", "ap-southeast-1")
STACK_NAME = os.environ.get("ADMIN_USER_STACK_NAME", "AssetGuardianAiStack-ap-southeast-1")


def get_user_pool_id():
    cfn = boto3.client("cloudformation", region_name=REGION)
    outputs = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]["Outputs"]
    for o in outputs:
        if o["OutputKey"] == "UserPoolId":
            return o["OutputValue"]
    raise SystemExit("UserPoolId not found in stack outputs")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)

    admin_email = sys.argv[1]
    password = sys.argv[2]
    user_pool_id = sys.argv[3] if len(sys.argv) > 3 else get_user_pool_id()

    idp = boto3.client("cognito-idp", region_name=REGION)

    try:
        idp.admin_create_user(
            UserPoolId=user_pool_id,
            Username=admin_email,
            UserAttributes=[
                {"Name": "email", "Value": admin_email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",  # no invite email; we're setting the password directly
        )
        print(f"Created user {admin_email}")
    except idp.exceptions.UsernameExistsException:
        print(f"User {admin_email} already exists — updating password only")

    idp.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=admin_email,
        Password=password,
        Permanent=True,
    )
    print(f"Password set (permanent) for {admin_email}")

    try:
        idp.admin_add_user_to_group(
            UserPoolId=user_pool_id, Username=admin_email, GroupName="Admins"
        )
        print(f"Added {admin_email} to the Admins group")
    except idp.exceptions.ResourceNotFoundException:
        print(
            "WARNING: 'Admins' group not found in this User Pool — deploy the latest "
            "stack (which creates it) before running this script, or the account will "
            "only get standard employee-level access."
        )

    print(f"\nAdmin can now sign in at the Portal URL with:\n  email: {admin_email}\n  password: (as provided)")


if __name__ == "__main__":
    main()
