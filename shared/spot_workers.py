import os
from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError


MANAGED_BY = "XtractDashboard"
ROLE = "xtract-spot-worker"


class SpotWorkerConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpotWorkerConfig:
    region: str
    ami_id: str
    subnet_id: str
    security_group_ids: list[str]
    key_name: str
    instance_type: str
    count: int
    workers_per_instance: int
    root_volume_gb: int
    root_device_name: str
    name_prefix: str
    iam_instance_profile: str


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SpotWorkerConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise SpotWorkerConfigError(f"{name} must be greater than zero")
    return value


def _config() -> SpotWorkerConfig:
    region = os.environ.get("SPOT_WORKER_REGION") or os.environ.get("AWS_REGION") or "eu-north-1"
    ami_id = os.environ.get("SPOT_WORKER_AMI_ID", "").strip()
    subnet_id = os.environ.get("SPOT_WORKER_SUBNET_ID", "").strip()
    security_groups = [
        sg.strip()
        for sg in os.environ.get("SPOT_WORKER_SECURITY_GROUP_IDS", "").split(",")
        if sg.strip()
    ]
    key_name = os.environ.get("SPOT_WORKER_KEY_NAME", "").strip()

    missing = []
    if not ami_id:
        missing.append("SPOT_WORKER_AMI_ID")
    if not subnet_id:
        missing.append("SPOT_WORKER_SUBNET_ID")
    if not security_groups:
        missing.append("SPOT_WORKER_SECURITY_GROUP_IDS")
    if not key_name:
        missing.append("SPOT_WORKER_KEY_NAME")
    if missing:
        raise SpotWorkerConfigError("Missing Spot worker config: " + ", ".join(missing))

    return SpotWorkerConfig(
        region=region,
        ami_id=ami_id,
        subnet_id=subnet_id,
        security_group_ids=security_groups,
        key_name=key_name,
        instance_type=os.environ.get("SPOT_WORKER_INSTANCE_TYPE", "t3.medium").strip(),
        count=_env_int("SPOT_WORKER_COUNT", 1),
        workers_per_instance=_env_int("SPOT_WORKERS_PER_INSTANCE", 8),
        root_volume_gb=_env_int("SPOT_WORKER_ROOT_VOLUME_GB", 20),
        root_device_name=os.environ.get("SPOT_WORKER_ROOT_DEVICE_NAME", "/dev/sda1").strip(),
        name_prefix=os.environ.get("SPOT_WORKER_NAME_PREFIX", "xtract-spot-worker").strip(),
        iam_instance_profile=os.environ.get("SPOT_WORKER_IAM_INSTANCE_PROFILE", "").strip(),
    )


def _client(cfg: SpotWorkerConfig | None = None):
    cfg = cfg or _config()
    return boto3.client("ec2", region_name=cfg.region)


def _user_data(workers_per_instance: int) -> str:
    return f"""#!/bin/bash
set -eux

systemctl daemon-reload

for i in $(seq 1 {workers_per_instance}); do
  systemctl enable "xtract-worker@$i"
  systemctl restart "xtract-worker@$i"
done
"""


def _instance_tags(name_prefix: str) -> list[dict]:
    return [
        {"Key": "Name", "Value": name_prefix},
        {"Key": "Role", "Value": ROLE},
        {"Key": "ManagedBy", "Value": MANAGED_BY},
    ]


def launch_workers(count: int | None = None) -> dict:
    cfg = _config()
    launch_count = count or cfg.count
    if launch_count <= 0:
        return {
            "launched": 0,
            "instance_ids": [],
            "instance_type": cfg.instance_type,
            "workers_per_instance": cfg.workers_per_instance,
            "root_volume_gb": cfg.root_volume_gb,
        }
    ec2 = _client(cfg)
    request = {
        "ImageId": cfg.ami_id,
        "InstanceType": cfg.instance_type,
        "MinCount": launch_count,
        "MaxCount": launch_count,
        "KeyName": cfg.key_name,
        "SubnetId": cfg.subnet_id,
        "SecurityGroupIds": cfg.security_group_ids,
        "UserData": _user_data(cfg.workers_per_instance),
        "InstanceMarketOptions": {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate",
            },
        },
        "BlockDeviceMappings": [
            {
                "DeviceName": cfg.root_device_name,
                "Ebs": {
                    "VolumeSize": cfg.root_volume_gb,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            }
        ],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": _instance_tags(cfg.name_prefix)},
            {"ResourceType": "volume", "Tags": _instance_tags(cfg.name_prefix)},
        ],
    }
    if cfg.iam_instance_profile:
        request["IamInstanceProfile"] = {"Name": cfg.iam_instance_profile}

    try:
        resp = ec2.run_instances(**request)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to launch Spot workers: {exc}") from exc

    instances = resp.get("Instances", [])
    return {
        "launched": len(instances),
        "instance_ids": [inst.get("InstanceId") for inst in instances if inst.get("InstanceId")],
        "instance_type": cfg.instance_type,
        "workers_per_instance": cfg.workers_per_instance,
        "root_volume_gb": cfg.root_volume_gb,
    }


def list_workers() -> dict:
    try:
        cfg = _config()
    except SpotWorkerConfigError as exc:
        return {"configured": False, "error": str(exc), "instances": []}

    ec2 = _client(cfg)
    try:
        resp = ec2.describe_instances(
            Filters=[
                {"Name": "tag:ManagedBy", "Values": [MANAGED_BY]},
                {"Name": "tag:Role", "Values": [ROLE]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
            ]
        )
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to list Spot workers: {exc}") from exc

    instances = []
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            tags = {tag.get("Key"): tag.get("Value") for tag in inst.get("Tags", [])}
            instances.append({
                "id": inst.get("InstanceId"),
                "name": tags.get("Name", ""),
                "state": inst.get("State", {}).get("Name", "unknown"),
                "type": inst.get("InstanceType", ""),
                "public_ip": inst.get("PublicIpAddress", ""),
                "private_ip": inst.get("PrivateIpAddress", ""),
                "lifecycle": inst.get("InstanceLifecycle", "on-demand"),
            })

    instances.sort(key=lambda item: (item["state"], item["id"] or ""))
    return {"configured": True, "instances": instances}


def auto_start_enabled() -> bool:
    raw = os.environ.get("SPOT_WORKER_AUTO_START_ON_WORK")
    if raw is None:
        raw = os.environ.get("SPOT_WORKER_AUTO_START_ON_UPLOAD", "true")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def ensure_workers() -> dict:
    cfg = _config()
    status = list_workers()
    active = sum(
        1
        for inst in status.get("instances", [])
        if inst.get("state") in {"pending", "running"}
    )
    missing = max(cfg.count - active, 0)
    if missing == 0:
        return {
            "launched": 0,
            "active": active,
            "desired": cfg.count,
            "instance_ids": [],
            "instance_type": cfg.instance_type,
            "workers_per_instance": cfg.workers_per_instance,
            "root_volume_gb": cfg.root_volume_gb,
        }

    launched = launch_workers(count=missing)
    launched["active"] = active + launched.get("launched", 0)
    launched["desired"] = cfg.count
    return launched


def terminate_workers() -> dict:
    status = list_workers()
    if not status.get("configured"):
        return {"terminated": 0, "instance_ids": [], "error": status.get("error")}

    instance_ids = [
        inst["id"]
        for inst in status.get("instances", [])
        if inst.get("id") and inst.get("state") in {"pending", "running", "stopping", "stopped"}
    ]
    if not instance_ids:
        return {"terminated": 0, "instance_ids": []}

    cfg = _config()
    ec2 = _client(cfg)
    try:
        ec2.terminate_instances(InstanceIds=instance_ids)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to terminate Spot workers: {exc}") from exc

    return {"terminated": len(instance_ids), "instance_ids": instance_ids}
