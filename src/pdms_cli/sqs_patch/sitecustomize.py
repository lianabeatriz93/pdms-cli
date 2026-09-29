"""Loaded by Python at startup in services run by pdms with local events (pdms puts this folder on PYTHONPATH).

It sends every SQS client created through botocore to the local ElasticMQ given in ``PDMS_SQS_ENDPOINT``, with
dummy credentials, without touching the service code or other AWS clients (S3 & co. keep their normal endpoint).
PDMS services pin botocore 1.29, which predates ``AWS_ENDPOINT_URL_SQS``, hence the patch. Being a
``sitecustomize``, it also applies in every process uvicorn ``--reload`` spawns.
"""

import os


def _patch() -> None:
    endpoint = os.environ.get("PDMS_SQS_ENDPOINT")
    if not endpoint:
        return
    try:
        import botocore.session
    except ImportError:  # not a boto service
        return
    original = botocore.session.Session.create_client
    if getattr(original, "_pdms_patched", False):
        return

    def create_client(self, service_name, *args, **kwargs):
        if service_name == "sqs":
            if not kwargs.get("endpoint_url"):
                kwargs["endpoint_url"] = endpoint
            kwargs.setdefault("region_name", os.environ.get("AWS_REGION") or "us-east-1")
            kwargs["aws_access_key_id"] = "local"
            kwargs["aws_secret_access_key"] = "local"  # nosec B105 - local ElasticMQ, not a real credential
            kwargs.pop("aws_session_token", None)
        return original(self, service_name, *args, **kwargs)

    create_client._pdms_patched = True
    botocore.session.Session.create_client = create_client


try:
    _patch()
except Exception:  # noqa: BLE001 - a broken patch must never stop the service from starting
    pass
