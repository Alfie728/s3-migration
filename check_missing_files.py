import argparse
import boto3
from collections import defaultdict

DEFAULT_BUCKET = 'web-data-platform'


def check_missing_files(bucket=None, prefixes=None, output_file='_missing_files.log'):
    """Check for folders missing info.json or merged_*.wav files"""
    s3_client = boto3.client('s3')
    bucket = bucket or DEFAULT_BUCKET
    prefixes = prefixes or ['']

    print(f"Scanning s3://{bucket}/...")
    if prefixes != ['']:
        print(f"Filtering to prefixes: {prefixes}")

    paginator = s3_client.get_paginator('list_objects_v2')

    # Group files by folder - track root files and separate/ files
    folders = defaultdict(lambda: {'root': set(), 'separate': set()})

    for prefix in prefixes:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                # Only look at voice-calls folders
                if '/voice-calls/' not in key:
                    continue

                # Extract folder path (up to the session folder)
                parts = key.split('/')
                # Find voice-calls index and get the folder after it
                try:
                    vc_idx = parts.index('voice-calls')
                    if vc_idx + 1 < len(parts):
                        folder = '/'.join(parts[:vc_idx + 2])
                        filename = parts[-1]

                        # Check if file is in separate/ subfolder
                        if 'separate' in parts:
                            folders[folder]['separate'].add(filename)
                        else:
                            folders[folder]['root'].add(filename)
                except ValueError:
                    continue

    print(f"Found {len(folders)} voice-call folders\n")

    # Check for missing files
    missing_info = []
    missing_merged = []
    missing_separate = []

    for folder, files in folders.items():
        has_info = 'info.json' in files['root']
        has_merged = any(f.startswith('merged_') and f.endswith('.wav') for f in files['root'])
        has_separate = len(files['separate']) > 0 and any(f.endswith('.ogg') for f in files['separate'])

        if not has_info:
            missing_info.append(folder)
        if not has_merged:
            missing_merged.append(folder)
        if not has_separate:
            missing_separate.append(folder)

    # Write to log file
    with open(output_file, 'w') as f:
        f.write(f"=== Missing Files Report ===\n")
        f.write(f"Total folders scanned: {len(folders)}\n\n")

        f.write(f"=== Missing info.json ({len(missing_info)} folders) ===\n")
        for folder in sorted(missing_info):
            f.write(f"{folder}\n")

        f.write(f"\n=== Missing merged_*.wav ({len(missing_merged)} folders) ===\n")
        for folder in sorted(missing_merged):
            f.write(f"{folder}\n")

        f.write(f"\n=== Missing separate/*.ogg ({len(missing_separate)} folders) ===\n")
        for folder in sorted(missing_separate):
            f.write(f"{folder}\n")

    # Print summary
    print(f"Results written to {output_file}\n")
    print(f"Summary:")
    print(f"   Total folders: {len(folders)}")
    print(f"   Missing info.json: {len(missing_info)}")
    print(f"   Missing merged_*.wav: {len(missing_merged)}")
    print(f"   Missing separate/*.ogg: {len(missing_separate)}")

    if missing_info:
        print(f"\nSample folders missing info.json:")
        for folder in missing_info[:5]:
            print(f"   {folder}")

    if missing_merged:
        print(f"\nSample folders missing merged_*.wav:")
        for folder in missing_merged[:5]:
            print(f"   {folder}")

    if missing_separate:
        print(f"\nSample folders missing separate/*.ogg:")
        for folder in missing_separate[:5]:
            print(f"   {folder}")

    return missing_info, missing_merged, missing_separate


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Check for folders missing info.json or merged_*.wav')
    parser.add_argument('--bucket', type=str, default=DEFAULT_BUCKET, help='S3 bucket to check (default: web-data-platform)')
    parser.add_argument('--prefix', type=str, nargs='+', default=[], help='Only check these project prefixes')
    parser.add_argument('--output', type=str, default='_missing_files.log', help='Output log file')
    args = parser.parse_args()

    prefixes = args.prefix if args.prefix else None
    check_missing_files(bucket=args.bucket, prefixes=prefixes, output_file=args.output)
