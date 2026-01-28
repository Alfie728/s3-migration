import re
import argparse
import boto3

SOURCE_BUCKET = 'web-data-platform'
DEST_BUCKET = 'web-data-platform-sanitized'


def sanitize_path(path):
    """Remove emails from paths (same logic as step1)"""
    email_pattern = r'[a-zA-Z][a-zA-Z0-9._%+-]*@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return re.sub(email_pattern, '', path)


def find_original_folder(s3_client, sanitized_folder):
    """Find the original folder in source bucket that matches the sanitized folder"""
    # Extract project and base path
    parts = sanitized_folder.split('/')
    project_id = parts[0]

    # Get the session ID (UUID at the end)
    session_folder = parts[-1]  # e.g., "01_16_2026_13_09---6a1c2301-8ee2-492a-b253-7d3dea14d1f1"

    # Extract the UUID part (last segment after the last dash before UUID pattern)
    uuid_match = re.search(r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$', session_folder)
    if not uuid_match:
        return None

    session_uuid = uuid_match.group(1)

    # List objects in source bucket with the project prefix and find matching folder
    paginator = s3_client.get_paginator('list_objects_v2')
    prefix = f"{project_id}/recordings/voice-calls/"

    for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=prefix, Delimiter='/'):
        for common_prefix in page.get('CommonPrefixes', []):
            folder_path = common_prefix['Prefix'].rstrip('/')
            if session_uuid in folder_path:
                return folder_path

    return None


def copy_missing_files(log_file='_missing_files_sanitized.log', dry_run=False):
    """Copy missing files from source bucket to sanitized bucket"""
    s3_client = boto3.client('s3')

    # Parse log file for folders with missing files
    missing_folders = set()
    with open(log_file, 'r') as f:
        for line in f:
            line = line.strip()
            if '/voice-calls/' in line and not line.startswith('==='):
                missing_folders.add(line)

    if not missing_folders:
        print("No missing folders found in log file")
        return

    print(f"Found {len(missing_folders)} folders with missing files")
    if dry_run:
        print("DRY RUN MODE - No files will be copied\n")

    for sanitized_folder in sorted(missing_folders):
        print(f"\nProcessing: {sanitized_folder}")

        # Find original folder
        original_folder = find_original_folder(s3_client, sanitized_folder)
        if not original_folder:
            print(f"  ERROR: Could not find original folder")
            continue

        print(f"  Original: {original_folder}")

        # List all files in original folder
        paginator = s3_client.get_paginator('list_objects_v2')

        for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=original_folder + '/'):
            for obj in page.get('Contents', []):
                source_key = obj['Key']
                dest_key = sanitize_path(source_key)

                # Check if file already exists in destination
                try:
                    s3_client.head_object(Bucket=DEST_BUCKET, Key=dest_key)
                    # File exists, skip
                    continue
                except:
                    pass

                # Copy file
                print(f"  Copying: {source_key}")
                print(f"       -> {dest_key}")

                if not dry_run:
                    try:
                        s3_client.copy_object(
                            CopySource={'Bucket': SOURCE_BUCKET, 'Key': source_key},
                            Bucket=DEST_BUCKET,
                            Key=dest_key
                        )
                    except Exception as e:
                        print(f"    ERROR: {e}")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done!")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Copy missing files from source to sanitized bucket')
    parser.add_argument('--log', type=str, default='_missing_files_sanitized.log', help='Missing files log')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without copying')
    args = parser.parse_args()

    copy_missing_files(log_file=args.log, dry_run=args.dry_run)
