"""The AWS profile the services use."""

from __future__ import annotations

from .. import awsenv
from ..config import Config
from ..i18n import _
from .errors import ActionError

__all__ = ["read_aws", "set_aws_profile"]


def set_aws_profile(cfg: Config, profile: str) -> None:
    """Use ``profile`` (one of the AWS config's; "" = none: pdms leaves AWS as the terminal has it)."""
    if profile and profile not in (known := awsenv.profiles()):
        raise ActionError(_("Unknown AWS profile '{name}'. In {path}: {names}", name=profile, path=awsenv.config_file(),
                            names=", ".join(known) or "-"))
    cfg.defaults.aws_profile = profile
    cfg.save()


def read_aws(cfg: Config) -> list[awsenv.Change]:
    """Read the Lambdas' buckets with the chosen profile now (see :func:`awsenv.refresh`)."""
    if not cfg.defaults.aws_profile:
        raise ActionError(_("Choose an AWS profile first (pdms aws profile)."))
    try:
        return awsenv.refresh(cfg.defaults.aws_profile)
    except awsenv.AwsError as exc:
        if exc.expired:
            raise ActionError(_("The AWS session of {profile} is over: log in again (pdms aws login).",
                                profile=cfg.defaults.aws_profile)) from exc
        raise ActionError(exc.message) from exc
