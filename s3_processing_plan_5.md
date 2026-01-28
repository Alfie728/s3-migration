# S3 Data Processing Plan

## Objective

Process voice call recordings stored in S3 to:
1. Copy all data to a sanitized bucket with emails removed from paths
2. Remove PII from `info.json` files
3. Separate failed calls from valid calls
4. Sync valid calls to customer's S3 bucket

---

## Source Structure

```
web-data-platform/
└── [projectId]/
    └── recordings/
        └── voice-calls/
            └── [date]-[email1]-[email2]-[sessionId]/
                ├── info.json
                ├── merged_xxx.wav
                └── separate/
                    ├── [email1]-[sessionId]_audio_xxx.ogg
                    └── [email2]-[sessionId]_audio_xxx.ogg
```

---

## Target Structure (After Processing)

```
web-data-platform/                    # UNTOUCHED - serves as backup
└── (original structure preserved)

web-data-platform-sanitized/          # NEW - working bucket with sanitized paths
└── [projectId]/
    └── recordings/
        ├── voice-calls/              # Only passed calls
        │   └── [date]---[sessionId]/
        │       ├── info.json         # Cleaned, no PII
        │       ├── merged_xxx.wav
        │       └── separate/
        │           └── -[sessionId]_audio_xxx.ogg
        └── rejected-calls/           # Failed calls moved here
            └── [date]---[sessionId]/
                └── ...

customer-bucket/                      # Final destination (synced from sanitized)
└── [projectId]/
    └── recordings/
        └── voice-calls/
            └── [date]---[sessionId]/
                ├── info.json
                ├── merged_xxx.wav
                └── separate/
                    └── -[sessionId]_audio_xxx.ogg
```

---

## PII Fields to Remove

| Location | Fields |
|----------|--------|
| `participants[]` | `email`, `name` |
| `participants[].demographics.backgroundInfo` | `dateOfBirth`, `currentAddress`, `birthCity` |
| `participants[].QAReview` | `participantName` |
| `participants[].filePath` | Replace emails with empty string |
| Root level | `mergedFilePath` - Replace emails with empty string |

---

## Pass/Fail Criteria

A call is considered **failed** if ANY participant has `isPassed: false` in their `QAReview` object.

```json
{
  "participants": [
    {
      "QAReview": {
        "isPassed": true  // Check this field
      }
    }
  ]
}
```

---

## Processing Steps

### Prerequisites: EC2 Setup (Required for 4.6TB)

For 4.6TB of data, running from an EC2 instance in the same region as your S3 buckets is essential for speed.

---

#### EC2 Setup via AWS Console

**1. Create IAM Role for EC2**

1. Go to **IAM Console** → **Roles** → **Create role**
2. Select **AWS service** → **EC2** → **Next**
3. Search and select **AmazonS3FullAccess** → **Next**
4. Role name: `S3MigrationRole` → **Create role**

**2. Launch EC2 Instance**

1. Go to **EC2 Console** → **Launch instance**
2. Configure:
   - **Name:** `s3-migration`
   - **AMI:** Amazon Linux 2023 (default)
   - **Instance type:** `c5.xlarge` (4 vCPU, 8GB RAM)
   - **Key pair:** Create new or select existing
   - **Network settings:** Allow SSH from your IP
   - **Storage:** 100 GB gp3
   - **Advanced details → IAM instance profile:** Select `S3MigrationRole`
3. Click **Launch instance**

**3. SSH into EC2**

```bash
# Get public IP from EC2 Console
ssh -i your-key.pem ec2-user@<EC2_PUBLIC_IP>
```

**4. Setup Environment on EC2**

```bash
# Install Python
sudo yum update -y
sudo yum install -y python3 python3-pip

# Install boto3 for Python S3 operations
pip3 install boto3

# Configure AWS CLI for max performance
aws configure set default.s3.max_concurrent_requests 100
aws configure set default.s3.max_queue_size 10000
aws configure set default.s3.multipart_threshold 64MB
aws configure set default.s3.multipart_chunksize 16MB

# Verify S3 access
aws s3 ls s3://web-data-platform/ --summarize

# Create working directory
mkdir -p ~/s3-migration
cd ~/s3-migration
```

**5. Upload Scripts to EC2**

From your local machine:
```bash
scp -i your-key.pem step*.py ec2-user@<EC2_PUBLIC_IP>:~/s3-migration/
```

**6. Cleanup After Migration (Don't Forget!)**

1. Go to **EC2 Console** → Select instance → **Instance state** → **Terminate**
2. Delete the key pair if no longer needed
3. Delete the IAM role if no longer needed

---

#### Estimated Time (4.6TB on EC2 same region)

| Method | Estimated Time |
|--------|----------------|
| Local machine (100 Mbps) | ~4-5 days |
| EC2 same region (10 Gbps) | ~2-4 hours |

---

### Step 1: Copy to sanitized bucket with cleaned paths

**Action:**
- Create new bucket `web-data-platform-sanitized`
- Copy ALL files from `web-data-platform` to new bucket
- Remove emails from all paths during copy (folders and filenames)
- Original bucket remains untouched as backup

**Why Python?** AWS CLI `sync` cannot rename paths. We need to:
1. List all objects
2. Calculate sanitized destination key
3. Copy each object with new key

**Path sanitization examples:**

| Type | Before | After |
|------|--------|-------|
| Folder | `01_01_2026-john@email.com-jane@email.com-uuid/` | `01_01_2026---uuid/` |
| Audio file | `john@email.com-uuid_audio_xxx.ogg` | `-uuid_audio_xxx.ogg` |
| Merged audio | `merged_1767463316202.wav` | `merged_1767463316202.wav` (unchanged) |

---

#### How to Run Step 1

**First, create the destination bucket:**
```bash
# Create sanitized bucket (update region as needed)
aws s3 mb s3://web-data-platform-sanitized --region us-east-1

# Enable versioning (recommended for safety)
aws s3api put-bucket-versioning \
  --bucket web-data-platform-sanitized \
  --versioning-configuration Status=Enabled
```

**Save as `step1_copy_sanitized.py`:**
```python
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
                print(f"  {source_key}\n  → {dest_key}\n")
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
        print("🔍 DRY RUN MODE - No files will be copied\n")

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
                print(f"  {key}\n  → {sanitize_path(key)}\n")
                renamed_count += 1
                if renamed_count >= 20:
                    print(f"  ... and more\n")
                    break

        total_renamed = sum(1 for k in all_keys if k != sanitize_path(k))
        print(f"\n📊 Summary:")
        print(f"   Total objects: {total}")
        print(f"   Will be renamed: {total_renamed}")
        print(f"   Unchanged: {total - total_renamed}")
        print(f"\n🔍 Run without --dry-run to copy files")
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

    print(f"\n✅ Copied {copied} objects to s3://{DEST_BUCKET}/")
    print(f"   Renamed: {copied - unchanged}")
    print(f"   Unchanged: {unchanged}")
    if errors > 0:
        print(f"⚠️  {errors} errors occurred")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Copy S3 bucket with sanitized paths')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without copying')
    parser.add_argument('--prefix', type=str, default='', help='Only process keys with this prefix')
    parser.add_argument('--workers', type=int, default=50, help='Number of parallel workers (default: 50)')
    args = parser.parse_args()

    MAX_WORKERS = args.workers
    step1_copy_to_sanitized(dry_run=args.dry_run, prefix=args.prefix)
```

**Run:**
```bash
# Dry run first (see what would be renamed)
python3 step1_copy_sanitized.py --dry-run

# Run for real (this will take a while for 4.6TB)
python3 step1_copy_sanitized.py --workers 50

# Or process a specific project first as a test
python3 step1_copy_sanitized.py --prefix "6968e7dd2371cd2887b5799f/" --dry-run
```

**Verify:**
```bash
# Compare object counts
aws s3 ls s3://web-data-platform/ --recursive --summarize | tail -2
aws s3 ls s3://web-data-platform-sanitized/ --recursive --summarize | tail -2

# Verify no emails in sanitized bucket paths
aws s3 ls s3://web-data-platform-sanitized/ --recursive | grep -E '[a-zA-Z0-9._%+-]+@' | head -10
# Should return nothing
```

---

### Step 2: Download info.json files from sanitized bucket

**Action:**
- Download all `info.json` files from `web-data-platform-sanitized`
- These files still contain PII in their content (just paths are sanitized)
- Preserve folder structure locally

**Output:**
```
./downloaded_jsons/
└── [projectId]/
    └── recordings/
        └── voice-calls/
            └── [date]---[sessionId]/     # Already sanitized paths
                └── info.json
```

---

#### How to Run Step 2

```bash
# Download all info.json files from sanitized bucket
aws s3 cp s3://web-data-platform-sanitized/ ./downloaded_jsons/ \
  --recursive \
  --exclude "*" \
  --include "*/voice-calls/*/info.json"

# Count downloaded files
find ./downloaded_jsons -name "info.json" | wc -l
```

---

### Step 3: Process JSONs locally (clean PII + categorize)

**Action:**
- Read each `info.json`
- Clean PII fields (email, name, dateOfBirth, etc.)
- Update `filePath` and `mergedFilePath` to sanitized versions (will now match actual paths)
- Check `isPassed` for all participants
- Categorize as passed or failed
- Save cleaned JSON back to local file
- Generate manifest files for tracking

**Output:**
```
./downloaded_jsons/
├── _passed_calls.json    # List of passed call folder paths
├── _failed_calls.json    # List of failed call folder paths
├── _processing.log       # Detailed log of all operations
└── [projectId]/...       # Cleaned info.json files
```

**Manifest format (`_passed_calls.json`):**
```json
[
  {
    "s3_key": "[projectId]/recordings/voice-calls/[folder]/info.json",
    "folder": "[projectId]/recordings/voice-calls/[folder]"
  }
]
```

**Log format (`_processing.log`):**
```
2024-01-15 10:30:01 | PROCESSED | [projectId]/voice-calls/[folder] | PASSED
2024-01-15 10:30:02 | PROCESSED | [projectId]/voice-calls/[folder] | FAILED
2024-01-15 10:30:03 | ERROR     | [projectId]/voice-calls/[folder] | Invalid JSON
```

---

#### How to Run Step 3

Save as `step3_process.py`:
```python
import json
import os
import re
import argparse
from datetime import datetime

LOCAL_DIR = './downloaded_jsons'

# PII fields to remove
PII_FIELDS = ['email', 'name']
DEMO_FIELDS = ['dateOfBirth', 'currentAddress', 'birthCity']


def sanitize_path(path):
    """Remove emails from paths (replace with empty string)"""
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return re.sub(email_pattern, '', path)


def clean_info_json(data):
    """Remove PII from info.json and update paths"""
    for participant in data.get('participants', []):
        # Remove top-level PII
        for field in PII_FIELDS:
            participant.pop(field, None)

        # Remove demographics PII
        if 'demographics' in participant:
            bg = participant['demographics'].get('backgroundInfo', {})
            for field in DEMO_FIELDS:
                bg.pop(field, None)

        # Remove participantName from QAReview
        if 'QAReview' in participant:
            participant['QAReview'].pop('participantName', None)

        # Sanitize filePath (paths already sanitized in bucket, this ensures JSON matches)
        if 'filePath' in participant:
            participant['filePath'] = sanitize_path(participant['filePath'])

    # Sanitize mergedFilePath
    if 'mergedFilePath' in data:
        data['mergedFilePath'] = sanitize_path(data['mergedFilePath'])

    return data


def is_call_passed(data):
    """Check if all participants passed QA review"""
    for participant in data.get('participants', []):
        qa_review = participant.get('QAReview', {})
        if not qa_review.get('isPassed', False):
            return False
    return True


def log_operation(log_file, status, folder, result):
    """Write to processing log"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_file, 'a') as f:
        f.write(f"{timestamp} | {status:<10} | {folder} | {result}\n")


def step3_process_jsons(dry_run=False):
    """Process all downloaded JSONs: clean PII and categorize"""
    passed_calls = []
    failed_calls = []
    log_file = os.path.join(LOCAL_DIR, '_processing.log')

    if dry_run:
        print("🔍 DRY RUN MODE - No files will be modified\n")

    for root, dirs, files in os.walk(LOCAL_DIR):
        for file in files:
            if file != 'info.json':
                continue

            local_path = os.path.join(root, file)
            s3_key = os.path.relpath(local_path, LOCAL_DIR)
            folder = os.path.dirname(s3_key)

            try:
                # Read JSON
                with open(local_path, 'r') as f:
                    data = json.load(f)

                # Check pass/fail BEFORE cleaning (preserve original isPassed)
                passed = is_call_passed(data)

                # Clean PII
                cleaned_data = clean_info_json(data)

                # Save cleaned version (skip in dry run)
                if not dry_run:
                    with open(local_path, 'w') as f:
                        json.dump(cleaned_data, f, indent=2)

                # Categorize (folder paths already sanitized from Step 1)
                call_info = {
                    's3_key': s3_key,
                    'folder': folder
                }

                if passed:
                    passed_calls.append(call_info)
                    if not dry_run:
                        log_operation(log_file, 'PROCESSED', folder, 'PASSED')
                else:
                    failed_calls.append(call_info)
                    if not dry_run:
                        log_operation(log_file, 'PROCESSED', folder, 'FAILED')

            except Exception as e:
                if not dry_run:
                    log_operation(log_file, 'ERROR', folder, str(e))
                else:
                    print(f"  ERROR: {folder} - {e}")

    # Save manifests (skip in dry run)
    if not dry_run:
        with open(os.path.join(LOCAL_DIR, '_passed_calls.json'), 'w') as f:
            json.dump(passed_calls, f, indent=2)

        with open(os.path.join(LOCAL_DIR, '_failed_calls.json'), 'w') as f:
            json.dump(failed_calls, f, indent=2)

    print(f"{'[DRY RUN] ' if dry_run else ''}✅ Processed {len(passed_calls) + len(failed_calls)} files")
    print(f"   - Passed: {len(passed_calls)}")
    print(f"   - Failed: {len(failed_calls)}")

    if dry_run:
        print("\n🔍 Run without --dry-run to apply changes")

    return passed_calls, failed_calls


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Process info.json files: clean PII and categorize')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without modifying files')
    args = parser.parse_args()

    step3_process_jsons(dry_run=args.dry_run)
```

Run:
```bash
# Dry run first (no changes made)
python3 step3_process.py --dry-run

# Then run for real
python3 step3_process.py
```

**After running, verify:**
```bash
# Check counts
cat ./downloaded_jsons/_passed_calls.json | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"
cat ./downloaded_jsons/_failed_calls.json | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"

# Review log
tail -50 ./downloaded_jsons/_processing.log
```

---

### Step 4: Upload cleaned info.json to sanitized bucket

**Action:**
- Upload all cleaned `info.json` files to `web-data-platform-sanitized`
- Overwrites the info.json files that still had PII content
- Paths already match (both local and S3 are sanitized)

**How path matching works:**

```
Local:  ./downloaded_jsons/[projectId]/voice-calls/[date]---[uuid]/info.json
                           └────────────────────┬────────────────────────────┘
                                                ▼
S3:     s3://web-data-platform-sanitized/[projectId]/voice-calls/[date]---[uuid]/info.json
```

**Result:**
- `info.json` files in sanitized bucket now have PII removed from content
- `filePath` and `mergedFilePath` match the actual sanitized paths

---

#### How to Run Step 4

```bash
# Dry run first (see what would be uploaded)
aws s3 cp ./downloaded_jsons/ s3://web-data-platform-sanitized/ \
  --recursive \
  --exclude "*" \
  --include "*/info.json" \
  --exclude "_*.json" \
  --exclude "*.log" \
  --dryrun

# Then run for real
aws s3 cp ./downloaded_jsons/ s3://web-data-platform-sanitized/ \
  --recursive \
  --exclude "*" \
  --include "*/info.json" \
  --exclude "_*.json" \
  --exclude "*.log"
```

**Verify:**
```bash
# Check a random file to confirm PII is removed
aws s3 cp s3://web-data-platform-sanitized/[projectId]/recordings/voice-calls/[any-folder]/info.json - | head -50

# Verify filePath no longer contains emails
aws s3 cp s3://web-data-platform-sanitized/[projectId]/recordings/voice-calls/[any-folder]/info.json - | grep -i "filepath"
```

---

### Step 5: Move failed calls to rejected-calls folder

**Action:**
- Read `_failed_calls.json` manifest
- For each failed call folder in `web-data-platform-sanitized`:
  - Copy entire folder to `rejected-calls/`
  - Delete original folder from `voice-calls/`

**Source:** `web-data-platform-sanitized/[projectId]/recordings/voice-calls/[folder]/`
**Destination:** `web-data-platform-sanitized/[projectId]/recordings/rejected-calls/[folder]/`

**Note:** This moves ALL files in the folder (info.json, audio files, etc.)

---

#### How to Run Step 5

Save as `step5_move_failed.py`:
```python
import json
import os
import argparse
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

SANITIZED_BUCKET = 'web-data-platform-sanitized'
LOCAL_DIR = './downloaded_jsons'
MAX_WORKERS = 20  # Parallel threads


def log_operation(log_file, status, folder, result):
    """Write to processing log"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_file, 'a') as f:
        f.write(f"{timestamp} | {status:<10} | {folder} | {result}\n")


def move_single_folder(call, dry_run, log_file):
    """Move a single folder from voice-calls to rejected-calls"""
    old_folder = call['folder']
    new_folder = old_folder.replace('/voice-calls/', '/rejected-calls/')

    old_s3_path = f"s3://{SANITIZED_BUCKET}/{old_folder}/"
    new_s3_path = f"s3://{SANITIZED_BUCKET}/{new_folder}/"

    try:
        if dry_run:
            print(f"  Would move: {old_folder}")
            return True

        # Use aws s3 sync + rm (faster than mv for folders)
        sync_cmd = ['aws', 's3', 'sync', old_s3_path, new_s3_path, '--quiet']
        subprocess.run(sync_cmd, check=True, capture_output=True)

        rm_cmd = ['aws', 's3', 'rm', old_s3_path, '--recursive', '--quiet']
        subprocess.run(rm_cmd, check=True, capture_output=True)

        log_operation(log_file, 'MOVED', old_folder, 'rejected-calls')
        return True

    except subprocess.CalledProcessError as e:
        log_operation(log_file, 'MOVE_ERR', old_folder, str(e))
        print(f"  ERROR: {old_folder} - {e}")
        return False


def step5_move_failed_calls(dry_run=False):
    """Move failed calls from voice-calls/ to rejected-calls/ using parallel processing"""
    log_file = os.path.join(LOCAL_DIR, '_processing.log')

    with open(os.path.join(LOCAL_DIR, '_failed_calls.json'), 'r') as f:
        failed_calls = json.load(f)

    if dry_run:
        print("🔍 DRY RUN MODE - No files will be moved\n")

    total = len(failed_calls)
    print(f"{'[DRY RUN] ' if dry_run else ''}Moving {total} failed calls with {MAX_WORKERS} parallel workers...")

    moved_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(move_single_folder, call, dry_run, log_file): call
            for call in failed_calls
        }

        for future in as_completed(futures):
            if future.result():
                moved_count += 1
            else:
                error_count += 1

            if (moved_count + error_count) % 100 == 0:
                print(f"  Progress: {moved_count + error_count}/{total} ({moved_count} success, {error_count} errors)")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}✅ {'Would move' if dry_run else 'Moved'} {moved_count} failed calls")
    if error_count > 0:
        print(f"⚠️  {error_count} errors - check _processing.log")

    if dry_run:
        print("\n🔍 Run without --dry-run to apply changes")

    return moved_count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Move failed calls to rejected-calls folder')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without moving files')
    parser.add_argument('--workers', type=int, default=20, help='Number of parallel workers (default: 20)')
    args = parser.parse_args()

    MAX_WORKERS = args.workers
    step5_move_failed_calls(dry_run=args.dry_run)
```

Run:
```bash
# Dry run first
python3 step5_move_failed.py --dry-run

# Run for real with 20 parallel workers
python3 step5_move_failed.py --workers 20
```

**Verify:**
```bash
# Check rejected-calls folder exists and has files
aws s3 ls s3://web-data-platform-sanitized/ --recursive | grep rejected-calls | head -10

# Check voice-calls no longer has failed calls
# (compare count before and after)
```

---

### Step 6: Sync to customer bucket

**Action:**
- Simple `aws s3 sync` from sanitized bucket to customer bucket
- Only sync `voice-calls/` (not `rejected-calls/`)
- All paths already sanitized, all PII already removed

**Source:** `web-data-platform-sanitized/[projectId]/recordings/voice-calls/`
**Destination:** `customer-bucket/[projectId]/recordings/voice-calls/`

> ✅ **Why this is now simple:** Paths are already sanitized in the source bucket, so we just need a straightforward sync.

---

#### How to Run Step 6

```bash
# TODO: Update CUSTOMER_BUCKET with actual bucket name
CUSTOMER_BUCKET="customer-bucket"

# Dry run first (see what would be synced)
aws s3 sync s3://web-data-platform-sanitized/ s3://$CUSTOMER_BUCKET/ \
  --exclude "*" \
  --include "*/recordings/voice-calls/*" \
  --dryrun

# Run for real
aws s3 sync s3://web-data-platform-sanitized/ s3://$CUSTOMER_BUCKET/ \
  --exclude "*" \
  --include "*/recordings/voice-calls/*"
```

**Verify:**
```bash
# Compare counts
aws s3 ls s3://web-data-platform-sanitized/ --recursive | grep voice-calls | wc -l
aws s3 ls s3://$CUSTOMER_BUCKET/ --recursive | grep voice-calls | wc -l

# Verify no emails in customer bucket paths
aws s3 ls s3://$CUSTOMER_BUCKET/ --recursive | grep -E '[a-zA-Z0-9._%+-]+@' | head -10
# Should return nothing

# Spot check a random info.json
aws s3 cp s3://$CUSTOMER_BUCKET/[projectId]/recordings/voice-calls/[any-folder]/info.json - | head -50
```

---

## Rollback Plan

If something goes wrong:

1. **Step 1 fails:** Safe to re-run; just delete `web-data-platform-sanitized` and start over
2. **Step 2 fails:** Re-download from sanitized bucket
3. **Step 3 fails:** Original files still in `./downloaded_jsons/`, just re-run
4. **Step 4 fails:** Re-download and re-process
5. **Step 5 fails:** Check both `voice-calls/` and `rejected-calls/` in sanitized bucket for duplicates
6. **Step 6 fails:** Safe to re-run; sync is idempotent

**Key advantage of this approach:** Original bucket `web-data-platform` is **never modified**, so you always have a complete backup.

**Recovery using logs:**
```bash
# Check last successful operation
tail -100 ./downloaded_jsons/_processing.log

# Find failed operations
grep "ERROR" ./downloaded_jsons/_processing.log
```

**Start over if needed:**
```bash
# Delete sanitized bucket and start fresh
aws s3 rm s3://web-data-platform-sanitized/ --recursive
# Then re-run from Step 1
```

---

## Execution Order

```
┌─────────────────────────────────────────────────────────┐
│  Step 1: Copy to sanitized bucket with cleaned paths    │
│          (web-data-platform → web-data-platform-sanitized)
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Step 2: Download info.json from sanitized bucket       │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Step 3: Process locally (clean PII + categorize)       │
└─────────────────────┬───────────────────────────────────┘
                      ▼
            ┌─────────────────────┐
            │  Review manifests   │  ← Manual checkpoint
            │  before proceeding  │
            └─────────┬───────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Step 4: Upload cleaned info.json to sanitized bucket   │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Step 5: Move failed calls to rejected-calls/           │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Step 6: Sync voice-calls to customer bucket            │
└─────────────────────────────────────────────────────────┘
```

---

## Estimated Counts

| Metric | Value |
|--------|-------|
| Total data size | 4.6 TB |
| Total folders | 999+ |
| Files per folder | ~4-5 (info.json, merged wav, separate audio files) |
| Estimated total files | ~4000-5000 |

## Estimated Time (on EC2 same region)

| Step | Estimated Time |
|------|----------------|
| Step 1: Copy to sanitized bucket | ~2-4 hours (4.6TB, same region copy) |
| Step 2: Download info.json | ~5-10 min |
| Step 3: Process locally | ~5-10 min |
| Step 4: Upload info.json | ~5-10 min |
| Step 5: Move failed calls | ~30-60 min (depends on % failed) |
| Step 6: Sync to customer | ~2-4 hours (4.6TB) |
| **Total** | **~5-9 hours** |

> Note: Step 1 and Step 6 are the longest as they involve copying 4.6TB of data. Running on EC2 in the same region is essential for reasonable performance.
