"""
AWS Lambda Handler for Warranty Verification API.

Endpoints:
  POST /api/warranty/check        — Check warranty for a single device
  POST /api/warranty/batch        — Check warranty for multiple devices
  GET  /api/warranty/vendors      — List supported vendors
  GET  /api/warranty/check/{vendor}/{serial}  — GET-based lookup
"""

import json
import traceback
from warranty_service import WarrantyService


def handler(event, context):
    """Lambda handler for API Gateway integration."""
    try:
        # Parse request
        http_method = event.get("httpMethod", event.get("requestContext", {}).get("http", {}).get("method", "GET"))
        path = event.get("path", event.get("rawPath", ""))
        body = event.get("body", "{}")
        if isinstance(body, str):
            try:
                body = json.loads(body) if body else {}
            except json.JSONDecodeError:
                body = {}

        # Query params for GET requests
        query_params = event.get("queryStringParameters") or {}
        path_params = event.get("pathParameters") or {}

        service = WarrantyService()

        # Route request
        if "vendors" in path:
            # GET /api/warranty/vendors
            result = service.get_supported_vendors()
            return _response(200, {"vendors": result})

        elif "batch" in path and http_method == "POST":
            # POST /api/warranty/batch
            items = body.get("items", [])
            if not items:
                return _response(400, {"error": "items array is required"})
            results = service.batch_check(items)
            return _response(200, {"results": results, "count": len(results)})

        elif http_method == "POST":
            # POST /api/warranty/check
            serial_number = body.get("serialNumber", "")
            vendor = body.get("vendor", "")
            device_type = body.get("deviceType", "")
            force_refresh = body.get("forceRefresh", False)

            if not serial_number:
                return _response(400, {"error": "serialNumber is required"})
            if not vendor:
                return _response(400, {"error": "vendor is required (hp, dell, lenovo, etc.)"})

            result = service.check_warranty(serial_number, vendor, device_type, force_refresh)
            status_code = 200 if result.get("valid") else 200  # Always 200, status in body
            return _response(status_code, result)

        elif http_method == "GET":
            # GET /api/warranty/check?vendor=hp&serialNumber=XXX
            # or GET /api/warranty/check/{vendor}/{serial}
            serial_number = query_params.get("serialNumber", path_params.get("serial", ""))
            vendor = query_params.get("vendor", path_params.get("vendor", ""))

            if not serial_number or not vendor:
                return _response(400, {"error": "vendor and serialNumber are required"})

            result = service.check_warranty(serial_number, vendor)
            return _response(200, result)

        else:
            return _response(404, {"error": "Not found"})

    except Exception as e:
        print(f"Handler error: {traceback.format_exc()}")
        return _response(500, {"error": f"Internal error: {str(e)[:200]}"})


def _response(status_code: int, body: dict) -> dict:
    """Build API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
        "body": json.dumps(body, default=str),
    }
