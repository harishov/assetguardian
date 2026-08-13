#!/bin/bash
###############################################################################
# AssetGuardian — Deploy Warranty Verification Feature
# Run this in AWS CloudShell
###############################################################################

set -e
REGION="ap-southeast-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
FUNCTION_NAME="assetguardian-warranty-checker"
ROLE_NAME="assetguardian-warranty-lambda-role"
TABLE_NAME="assetguardian-warranty-cache"
API_NAME="assetguardian"  # Existing API Gateway name

echo "================================================================"
echo "  AssetGuardian — Warranty Feature Deployment"
echo "  Account: $ACCOUNT_ID | Region: $REGION"
echo "================================================================"

# ─── Step 1: Create DynamoDB Cache Table ──────────────────────────────────────
echo ""
echo "[1/5] Creating DynamoDB cache table..."
aws dynamodb describe-table --table-name $TABLE_NAME --region $REGION 2>/dev/null && echo "  Table exists" || {
    aws dynamodb create-table \
        --table-name $TABLE_NAME \
        --key-schema AttributeName=cacheKey,KeyType=HASH \
        --attribute-definitions AttributeName=cacheKey,AttributeType=S \
        --billing-mode PAY_PER_REQUEST \
        --region $REGION
    echo "  Table created: $TABLE_NAME"
    # Enable TTL
    aws dynamodb update-time-to-live \
        --table-name $TABLE_NAME \
        --time-to-live-specification Enabled=true,AttributeName=ttl \
        --region $REGION
    echo "  TTL enabled"
}

# ─── Step 2: Create Lambda Role ───────────────────────────────────────────────
echo ""
echo "[2/5] Creating Lambda execution role..."
aws iam get-role --role-name $ROLE_NAME 2>/dev/null && echo "  Role exists" || {
    aws iam create-role \
        --role-name $ROLE_NAME \
        --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
        --region $REGION
    
    # Attach policies
    aws iam attach-role-policy --role-name $ROLE_NAME --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    aws iam attach-role-policy --role-name $ROLE_NAME --policy-arn arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess
    
    echo "  Role created with DynamoDB + CloudWatch permissions"
    echo "  Waiting 10s for role propagation..."
    sleep 10
}

# ─── Step 3: Package Lambda ───────────────────────────────────────────────────
echo ""
echo "[3/5] Packaging Lambda function..."
PACKAGE_DIR="/tmp/warranty-lambda"
rm -rf $PACKAGE_DIR
mkdir -p $PACKAGE_DIR

# Copy backend code
cp -r backend/*.py $PACKAGE_DIR/
cp -r backend/vendors $PACKAGE_DIR/

# Install dependencies
pip install -r backend/requirements.txt -t $PACKAGE_DIR/ -q

# Create zip
cd $PACKAGE_DIR
zip -r9 /tmp/warranty-lambda.zip . -q
cd -
echo "  Package created: $(du -sh /tmp/warranty-lambda.zip | cut -f1)"

# ─── Step 4: Create/Update Lambda Function ────────────────────────────────────
echo ""
echo "[4/5] Deploying Lambda function..."
aws lambda get-function --function-name $FUNCTION_NAME --region $REGION 2>/dev/null && {
    echo "  Updating existing function..."
    aws lambda update-function-code \
        --function-name $FUNCTION_NAME \
        --zip-file fileb:///tmp/warranty-lambda.zip \
        --region $REGION > /dev/null
} || {
    echo "  Creating new function..."
    aws lambda create-function \
        --function-name $FUNCTION_NAME \
        --runtime python3.11 \
        --handler handler.handler \
        --role "arn:aws:iam::${ACCOUNT_ID}:role/$ROLE_NAME" \
        --zip-file fileb:///tmp/warranty-lambda.zip \
        --timeout 30 \
        --memory-size 256 \
        --environment "Variables={WARRANTY_CACHE_TABLE=$TABLE_NAME,AWS_REGION_NAME=$REGION}" \
        --region $REGION > /dev/null
}
echo "  Lambda deployed: $FUNCTION_NAME"

# Update environment variables (in case of update)
aws lambda update-function-configuration \
    --function-name $FUNCTION_NAME \
    --timeout 30 \
    --memory-size 256 \
    --environment "Variables={WARRANTY_CACHE_TABLE=$TABLE_NAME,AWS_REGION_NAME=$REGION}" \
    --region $REGION > /dev/null 2>/dev/null || true

# ─── Step 5: Add API Gateway Route ───────────────────────────────────────────
echo ""
echo "[5/5] Configuring API Gateway route..."

# Find existing API
API_ID=$(aws apigatewayv2 get-apis --region $REGION --query "Items[?contains(Name,'$API_NAME')].ApiId" --output text)

if [ -n "$API_ID" ] && [ "$API_ID" != "None" ]; then
    echo "  Found API: $API_ID"
    
    # Create integration
    LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"
    INTEGRATION_ID=$(aws apigatewayv2 create-integration \
        --api-id $API_ID \
        --integration-type AWS_PROXY \
        --integration-uri $LAMBDA_ARN \
        --payload-format-version "2.0" \
        --region $REGION \
        --query "IntegrationId" --output text 2>/dev/null) || true
    
    if [ -n "$INTEGRATION_ID" ]; then
        # Add routes
        aws apigatewayv2 create-route --api-id $API_ID --route-key "POST /api/warranty/check" --target "integrations/$INTEGRATION_ID" --region $REGION 2>/dev/null || true
        aws apigatewayv2 create-route --api-id $API_ID --route-key "POST /api/warranty/batch" --target "integrations/$INTEGRATION_ID" --region $REGION 2>/dev/null || true
        aws apigatewayv2 create-route --api-id $API_ID --route-key "GET /api/warranty/vendors" --target "integrations/$INTEGRATION_ID" --region $REGION 2>/dev/null || true
        
        # Grant API Gateway permission to invoke Lambda
        aws lambda add-permission \
            --function-name $FUNCTION_NAME \
            --statement-id apigateway-warranty \
            --action lambda:InvokeFunction \
            --principal apigateway.amazonaws.com \
            --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*" \
            --region $REGION 2>/dev/null || true
        
        echo "  Routes added: POST /api/warranty/check, POST /api/warranty/batch, GET /api/warranty/vendors"
    fi
else
    echo "  WARNING: API Gateway '$API_NAME' not found. Routes not added."
    echo "  You'll need to manually add routes or create a new API."
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "================================================================"
echo "  Warranty Feature Deployed!"
echo "================================================================"
echo "  Lambda:    $FUNCTION_NAME"
echo "  DynamoDB:  $TABLE_NAME"
echo "  Endpoints: POST /api/warranty/check"
echo "             POST /api/warranty/batch"
echo "             GET  /api/warranty/vendors"
echo ""
echo "  To configure vendor APIs, set environment variables:"
echo "    HP:     HP_WARRANTY_API_KEY, HP_WARRANTY_API_SECRET"
echo "    Dell:   DELL_API_CLIENT_ID, DELL_API_CLIENT_SECRET"
echo "    Lenovo: LENOVO_WARRANTY_API_KEY (optional)"
echo ""
echo "  Test: aws lambda invoke --function-name $FUNCTION_NAME --payload '{\"httpMethod\":\"POST\",\"path\":\"/api/warranty/check\",\"body\":\"{\\\"serialNumber\\\":\\\"5CG01523C7\\\",\\\"vendor\\\":\\\"hp\\\"}\"}' /tmp/out.json --region $REGION && cat /tmp/out.json"
echo "================================================================"
