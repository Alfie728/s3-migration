import re
import argparse
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

SOURCE_BUCKET = 'web-data-platform'
DEST_BUCKET = 'web-data-platform-sanitized'
MAX_WORKERS = 50  # Parallel threads


def sanitize_path(path):
    """Remove emails from paths (replace with empty string)"""
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return re.sub(email_pattern, '', path)


def copy_single_object(s3_client, source_key, dry_run):
    """Copy a single object with sanitized path"""
    dest_key = sanitize_path(source_key)

    try:
        if dry_run:
            if source_key != dest_key:
                print(f"  {source_key}\n  -> {dest_key}\n")
            return True, source_key == dest_key

        copy_source = {'Bucket': SOURCE_BUCKET, 'Key': source_key}
        s3_client.copy_object(
            CopySource=copy_source,
            Bucket=DEST_BUCKET,
            Key=dest_key
        )
        return True, source_key == dest_key

    except Exception as e:
        print(f"  ERROR: {source_key} - {e}")
        return False, False


def step1_copy_to_sanitized(dry_run=False, prefix=''):
    """Copy all objects from source to sanitized bucket with cleaned paths"""
    s3_client = boto3.client('s3')

    if dry_run:
        print("DRY RUN MODE - No files will be copied\n")

    # List all objects
    print(f"Listing objects in s3://{SOURCE_BUCKET}/{prefix}...")
    paginator = s3_client.get_paginator('list_objects_v2')

    all_keys = []
    for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            all_keys.append(obj['Key'])

    total = len(all_keys)
    print(f"Found {total} objects to copy\n")

    if dry_run:
        print("Sample of paths that will be renamed:\n")
        renamed_count = 0
        for key in all_keys[:100]:  # Show first 100
            if key != sanitize_path(key):
                print(f"  {key}\n  -> {sanitize_path(key)}\n")
                renamed_count += 1
                if renamed_count >= 20:
                    print(f"  ... and more\n")
                    break

        total_renamed = sum(1 for k in all_keys if k != sanitize_path(k))
        print(f"\nSummary:")
        print(f"   Total objects: {total}")
        print(f"   Will be renamed: {total_renamed}")
        print(f"   Unchanged: {total - total_renamed}")
        print(f"\nRun without --dry-run to copy files")
        return

    # Copy with parallel workers
    print(f"Copying {total} objects with {MAX_WORKERS} parallel workers...")

    copied = 0
    errors = 0
    unchanged = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(copy_single_object, s3_client, key, dry_run): key
            for key in all_keys
        }

        for future in as_completed(futures):
            success, was_unchanged = future.result()
            if success:
                copied += 1
                if was_unchanged:
                    unchanged += 1
            else:
                errors += 1

            if (copied + errors) % 500 == 0:
                print(f"  Progress: {copied + errors}/{total} ({copied} copied, {errors} errors)")

    print(f"\nCopied {copied} objects to s3://{DEST_BUCKET}/")
    print(f"   Renamed: {copied - unchanged}")
    print(f"   Unchanged: {unchanged}")
    if errors > 0:
        print(f"WARNING: {errors} errors occurred")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Copy S3 bucket with sanitized paths')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without copying')
    parser.add_argument('--prefix', type=str, default='', help='Only process keys with this prefix')
    parser.add_argument('--workers', type=int, default=50, help='Number of parallel workers (default: 50)')
    args = parser.parse_args()

    MAX_WORKERS = args.workers
    step1_copy_to_sanitized(dry_run=args.dry_run, prefix=args.prefix)
