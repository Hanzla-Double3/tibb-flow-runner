{
  "openapi": "3.0.0",
  "info": {
    "title": "Manual tools",
    "version": "1.0"
  },
  "paths": {
    "/receipts/upload": {
      "post": {
        "operationId": "upload_test_receipt",
        "tags": [
          "receipts"
        ],
        "requestBody": {
          "required": true,
          "content": {
            "multipart/form-data": {
              "schema": {
                "type": "object",
                "properties": {
                  "receipt_file": {
                    "type": "string",
                    "format": "binary"
                  },
                  "amount": {
                    "type": "string"
                  },
                  "notes": {
                    "type": "string"
                  }
                }
              }
            }
          }
        }
      }
    },
    "/profile/avatar": {
      "post": {
        "operationId": "upload_avatar",
        "tags": [
          "profile"
        ],
        "requestBody": {
          "required": true,
          "content": {
            "multipart/form-data": {
              "schema": {
                "type": "object",
                "properties": {
                  "avatar": {
                    "type": "string"
                  },
                  "caption": {
                    "type": "string"
                  }
                }
              }
            }
          }
        }
      }
    }
  },
  "components": {
    "securitySchemes": {
      "bearerAuth": {
        "type": "http",
        "scheme": "bearer"
      }
    }
  }
}