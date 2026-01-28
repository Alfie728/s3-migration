import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

SOURCE_BUCKET = 'web-data-platform'
DEST_BUCKET = 'web-data-platform-sanitized'
MAX_WORKERS = 50  # Parallel threads

# Email to userId mapping (loaded from file)
EMAIL_TO_USERID = {}


def load_email_mapping(mapping_file):
    """Load email to userId mapping from JSON file"""
    global EMAIL_TO_USERID
    with open(mapping_file, 'r') as f:
        EMAIL_TO_USERID = json.load(f)
    print(f"Loaded {len(EMAIL_TO_USERID)} email mappings from {mapping_file}\n")


def sanitize_path(path):
    """Remove emails from paths (replace with empty string)"""
    # Require email local part to start with a letter to avoid matching dates like 07_18_2025_20_31-
    email_pattern = r'[a-zA-Z][a-zA-Z0-9._%+-]*@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return re.sub(email_pattern, '', path)


def sanitize_separate_file(path):
    """
    Sanitize files in separate/ folder:
    - .ogg files: [email]-[sessionId]_audio_[random-uuid].ogg -> [userId]-[sessionId]-audio.ogg
    - transcription files: already use userId, just sanitize the folder path
    """
    # Check if this is a file in a separate/ folder
    if '/separate/' not in path:
        return sanitize_path(path)

    # Split into folder path and filename
    folder_path, filename = path.rsplit('/', 1)

    # Sanitize the folder path (remove emails from folder names)
    sanitized_folder = sanitize_path(folder_path)

    # Handle .ogg audio files: [email]-[sessionId]_audio_[random-uuid].ogg
    if filename.endswith('.ogg'):
        # Pattern: email-sessionId_audio_uuid.ogg
        # Email can contain dots, so we need to match until @domain.tld
        ogg_pattern = r'^([a-zA-Z][a-zA-Z0-9._%+-]*@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})-([0-9a-f-]{36})_audio_[0-9a-f-]+\.ogg$'
        match = re.match(ogg_pattern, filename)

        if match:
            email = match.group(1)
            session_id = match.group(2)

            # Look up userId from email
            user_id = EMAIL_TO_USERID.get(email)
            if user_id:
                new_filename = f"{user_id}-{session_id}-audio.ogg"
                return f"{sanitized_folder}/{new_filename}"
            else:
                # Email not found in mapping, just remove email from filename
                new_filename = f"-{session_id}-audio.ogg"
                return f"{sanitized_folder}/{new_filename}"

    # For transcription files or unmatched patterns, just sanitize emails
    sanitized_filename = sanitize_path(filename)
    return f"{sanitized_folder}/{sanitized_filename}"


def get_dest_key(source_key):
    """Get the destination key for a source key"""
    if '/separate/' in source_key:
        return sanitize_separate_file(source_key)
    else:
        return sanitize_path(source_key)


def copy_single_object(s3_client, source_key, dry_run):
    """Copy a single object with sanitized path"""
    dest_key = get_dest_key(source_key)

    try:
        if dry_run:
            if source_key != dest_key:
                print(f"  {source_key}\n  -> {dest_key}\n")
            return True, source_key == dest_key, None

        copy_source = {'Bucket': SOURCE_BUCKET, 'Key': source_key}
        s3_client.copy_object(
            CopySource=copy_source,
            Bucket=DEST_BUCKET,
            Key=dest_key
        )
        return True, source_key == dest_key, None

    except Exception as e:
        return False, False, str(e)


def step1_copy_to_sanitized(dry_run=False, prefixes=None):
    """Copy all objects from source to sanitized bucket with cleaned paths"""
    s3_client = boto3.client('s3')
    prefixes = prefixes or ['']

    if dry_run:
        print("DRY RUN MODE - No files will be copied\n")

    # List all objects
    print(f"Listing objects in s3://{SOURCE_BUCKET}/...")
    if prefixes != ['']:
        print(f"Filtering to prefixes: {prefixes}")
    paginator = s3_client.get_paginator('list_objects_v2')

    all_keys = []
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=prefix):
            for obj in page.get('Contents', []):
                all_keys.append(obj['Key'])

    total = len(all_keys)
    print(f"Found {total} objects to copy\n")

    if dry_run:
        # Categorize files
        separate_ogg = [k for k in all_keys if '/separate/' in k and k.endswith('.ogg')]
        separate_json = [k for k in all_keys if '/separate/' in k and k.endswith('.json')]
        other_files = [k for k in all_keys if '/separate/' not in k]

        print(f"File breakdown:")
        print(f"   separate/ .ogg files: {len(separate_ogg)}")
        print(f"   separate/ .json files: {len(separate_json)}")
        print(f"   Other files: {len(other_files)}\n")

        # Show samples of separate/ .ogg files
        print("=== Sample separate/ .ogg files ===")
        for key in separate_ogg[:5]:
            dest = get_dest_key(key)
            print(f"  {key}")
            print(f"  -> {dest}\n")

        # Show samples of separate/ .json files
        print("=== Sample separate/ .json files ===")
        for key in separate_json[:3]:
            dest = get_dest_key(key)
            print(f"  {key}")
            print(f"  -> {dest}\n")

        # Show samples of other renamed files
        print("=== Sample other renamed files ===")
        renamed_others = [k for k in other_files if k != get_dest_key(k)][:5]
        for key in renamed_others:
            dest = get_dest_key(key)
            print(f"  {key}")
            print(f"  -> {dest}\n")

        total_renamed = sum(1 for k in all_keys if k != get_dest_key(k))
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
    error_list = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(copy_single_object, s3_client, key, dry_run): key
            for key in all_keys
        }

        for future in as_completed(futures):
            key = futures[future]
            success, was_unchanged, error = future.result()
            if success:
                copied += 1
                if was_unchanged:
                    unchanged += 1
            else:
                errors += 1
                error_list.append({'key': key, 'error': error})

            if (copied + errors) % 500 == 0:
                print(f"  Progress: {copied + errors}/{total} ({copied} copied, {errors} errors)")

    print(f"\nCopied {copied} objects to s3://{DEST_BUCKET}/")
    print(f"   Renamed: {copied - unchanged}")
    print(f"   Unchanged: {unchanged}")
    if errors > 0:
        print(f"WARNING: {errors} errors occurred")
        # Write error log
        with open('_copy_errors.log', 'w') as f:
            for item in error_list:
                f.write(f"{item['key']}\n  Error: {item['error']}\n\n")
        print(f"Error log written to: _copy_errors.log")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Copy S3 bucket with sanitized paths')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without copying')
    parser.add_argument('--prefix', type=str, nargs='+', default=[], help='Only process keys with these prefixes (can specify multiple)')
    parser.add_argument('--workers', type=int, default=50, help='Number of parallel workers (default: 50)')
    parser.add_argument('--email-mapping', type=str, default='email_to_userid.json',
                        help='JSON file with email to userId mapping (default: email_to_userid.json)')
    args = parser.parse_args()

    MAX_WORKERS = args.workers
    prefixes = args.prefix if args.prefix else None

    # Load email mapping
    load_email_mapping(args.email_mapping)

    step1_copy_to_sanitized(dry_run=args.dry_run, prefixes=prefixes)
