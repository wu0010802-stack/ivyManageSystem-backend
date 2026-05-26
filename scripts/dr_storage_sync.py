"""scripts/dr_storage_sync.py

把 Supabase Storage bucket 鏡像到 R2。Idempotent。

用法：
  python scripts/dr_storage_sync.py \
    --buckets leave-attachments growth-reports \
    --target s3://ivy-dr/storage/ \
    --mode incremental

環境變數：SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / AWS_* / R2_ENDPOINT
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from utils.taipei_time import now_taipei_naive
from typing import Iterable

import boto3
from supabase import create_client

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _list_supabase(client, bucket: str) -> list[dict]:
    """回傳 [{name, updated_at, size}]，遞迴展平。"""
    out: list[dict] = []
    stack = [""]
    while stack:
        prefix = stack.pop()
        items = (
            client.storage.from_(bucket).list(prefix)
            if prefix
            else client.storage.from_(bucket).list()
        )
        for it in items:
            name = it.get("name")
            if not name:
                continue
            full = f"{prefix}/{name}" if prefix else name
            # Supabase 目錄項目 metadata == None；檔案項目含 size / updated_at
            meta = it.get("metadata")
            if meta is None and it.get("id") is None:
                # directory
                stack.append(full)
            else:
                out.append(
                    {
                        "name": full,
                        "updated_at": it.get("updated_at")
                        or it.get("created_at")
                        or "",
                        "size": (meta or {}).get("size") or it.get("size") or 0,
                    }
                )
    return out


def _list_r2(s3, bucket: str, prefix: str) -> dict[str, dict]:
    """回傳 {key: {user_metadata, size}}。"""
    paginator = s3.get_paginator("list_objects_v2")
    out: dict[str, dict] = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            head = s3.head_object(Bucket=bucket, Key=obj["Key"])
            out[obj["Key"]] = {
                "user_metadata": head.get("Metadata") or {},
                "size": obj["Size"],
            }
    return out


def _r2_key(target_prefix: str, src_bucket: str, src_name: str) -> str:
    return f"{target_prefix.rstrip('/')}/{src_bucket}/{src_name}"


def _decide_action(src: dict, dst: dict | None, mode: str = "incremental") -> str:
    if dst is None:
        return "upload"
    if mode == "full":
        return "upload"
    src_ts = src["updated_at"]
    dst_ts = (dst["user_metadata"] or {}).get("x-source-updated-at", "")
    if src_ts and dst_ts and src_ts > dst_ts:
        return "upload"
    return "skip"


def _sync_bucket(
    sb_client,
    s3,
    src_bucket: str,
    target_uri: str,
    dry_run: bool,
    mode: str = "incremental",
) -> dict[str, int]:
    # target_uri 例：s3://ivy-dr/storage/
    assert target_uri.startswith("s3://")
    _, _, rest = target_uri.partition("s3://")
    dst_bucket, _, dst_prefix = rest.partition("/")

    src_items = _list_supabase(sb_client, src_bucket)
    dst_items = _list_r2(s3, dst_bucket, f"{dst_prefix.rstrip('/')}/{src_bucket}/")

    stats = {"upload": 0, "skip": 0, "error": 0}
    for src in src_items:
        key = _r2_key(dst_prefix, src_bucket, src["name"])
        dst = dst_items.get(key)
        action = _decide_action(src, dst, mode)
        if action == "skip":
            stats["skip"] += 1
            continue
        if dry_run:
            logger.info(
                "[dry-run] would upload %s/%s → %s", src_bucket, src["name"], key
            )
            stats["upload"] += 1
            continue
        try:
            data = sb_client.storage.from_(src_bucket).download(src["name"])
            s3.put_object(
                Bucket=dst_bucket,
                Key=key,
                Body=data,
                Metadata={
                    "x-source-updated-at": src["updated_at"]
                    or now_taipei_naive().isoformat()
                },
            )
            stats["upload"] += 1
            logger.info("uploaded %s/%s → %s", src_bucket, src["name"], key)
        except Exception as e:
            logger.exception("upload failed %s/%s: %s", src_bucket, src["name"], e)
            stats["error"] += 1
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--buckets", nargs="+", required=True)
    parser.add_argument("--target", required=True, help="s3://bucket/prefix/")
    parser.add_argument(
        "--mode", choices=["incremental", "full"], default="incremental"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    r2_endpoint = os.environ["R2_ENDPOINT"]

    sb = create_client(sb_url, sb_key)
    s3 = boto3.client(
        "s3",
        endpoint_url=r2_endpoint,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_DEFAULT_REGION", "auto"),
    )

    overall = {"upload": 0, "skip": 0, "error": 0}
    for bucket in args.buckets:
        stats = _sync_bucket(sb, s3, bucket, args.target, args.dry_run, args.mode)
        logger.info("bucket=%s stats=%s", bucket, stats)
        for k in overall:
            overall[k] += stats[k]

    logger.info("overall=%s", overall)
    if overall["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
