"""Generate private S3 identity config or wait/create the deployment bucket."""
import json
import os
from pathlib import Path
import sys
import time


if sys.argv[1] == "config":
    path = Path("/config/s3.json")
    path.write_text(json.dumps({"identities": [{"name": "video", "credentials": [{
        "accessKey": os.environ["AWS_ACCESS_KEY_ID"], "secretKey": os.environ["AWS_SECRET_ACCESS_KEY"]}],
        "actions": ["Admin", "Read", "List", "Tagging", "Write"]}]}))
    path.chmod(0o600)
else:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    client = boto3.client("s3", endpoint_url=os.environ["VIDEO_S3_ENDPOINT"], region_name="us-east-1",
        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}, s3={"addressing_style": "path"}))
    deadline = time.monotonic() + 120
    while True:
        try:
            try:
                client.head_bucket(Bucket=os.environ["VIDEO_S3_BUCKET"])
            except ClientError as error:
                if error.response["Error"]["Code"] not in ("404", "NoSuchBucket", "NotFound"):
                    raise
                client.create_bucket(Bucket=os.environ["VIDEO_S3_BUCKET"])
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError("private object store did not become ready") from None
            time.sleep(2)
    print("Private bucket ready")
