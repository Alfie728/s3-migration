# S3 Data Processing Plan

## Objective

Process voice call recordings stored in S3 to:
1. Remove PII from `info.json` files
2. Separate failed calls from valid calls
3. Upload valid calls to customer's S3 bucket with sanitized paths

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
web-data-platform/
└── [projectId]/
    └── recordings/
        ├── voice-calls/          # Only passed calls (cleaned)
        └── rejected-calls/       # Failed calls moved here (cleaned)

customer-bucket/
└── [projectId]/
    └── recordings/
        └── voice-calls/
            └── [date]---[sessionId]/                # Emails removed (empty string)
                ├── info.json                        # Cleaned, no PII
                ├── merged_xxx.wav
                └── separate/
                    └── -[sessionId]_audio_xxx.ogg   # Email removed
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

### Step 1: Download all info.json files locally (+ Backup)

**Action:**
- Use AWS CLI to download all `info.json` files from `web-data-platform`
- Preserve folder structure locally
- Keep this as backup of original files with PII

**Output:**
```
./downloaded_jsons/                    # This serves as BACKUP of originals
└── [projectId]/
    └── recordings/
        └── voice-calls/
            └── [folder]/
                └── info.json
```

---

#### How to Run Step 1

```bash
# Download all info.json files
aws s3 cp s3://web-data-platform/ ./downloaded_jsons/ \
  --recursive \
  --exclude "*" \
  --include "*/voice-calls/*/info.json"

# Create timestamped backup
cp -r ./downloaded_jsons/ ./backup_originals_$(date +%Y%m%d_%H%M%S)/
```

**Verify:**
```bash
# Count downloaded files
find ./downloaded_jsons -name "info.json" | wc -l
```

---

### Step 2: Process JSONs locally

**Action:**
- Read each `info.json`
- Clean PII fields
- Update `filePath` and `mergedFilePath` to sanitized versions
- Check `isPassed` for all participants
- Categorize as passed or failed
- Save cleaned JSON back to local file
- Generate manifest files for tracking
- Log all operations for recovery

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
    "folder": "[projectId]/recordings/voice-calls/[folder]",
    "sanitized_folder": "[projectId]/recordings/voice-calls/[sanitized-folder]"
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

#### How to Run Step 2

Save as `step2_process.py`:
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
        
        # Sanitize filePath
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


def step2_process_jsons(dry_run=False):
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
                
                # Categorize
                sanitized_folder = sanitize_path(folder)
                call_info = {
                    's3_key': s3_key,
                    'folder': folder,
                    'sanitized_folder': sanitized_folder
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
    
    step2_process_jsons(dry_run=args.dry_run)
```

Run:
```bash
# Dry run first (no changes made)
python step2_process.py --dry-run

# Then run for real
python step2_process.py
```

**After running, verify:**
```bash
# Check counts
cat ./downloaded_jsons/_passed_calls.json | python -c "import sys,json; print(len(json.load(sys.stdin)))"
cat ./downloaded_jsons/_failed_calls.json | python -c "import sys,json; print(len(json.load(sys.stdin)))"

# Review log
tail -50 ./downloaded_jsons/_processing.log
```

---

### Step 3: Re-upload cleaned info.json to original locations

**Action:**
- Upload all cleaned `info.json` files back to S3
- Overwrites original files (removes PII from source)

**How path matching works:**

When we download in Step 1, we preserve the exact folder structure locally:

```
S3 path:
s3://web-data-platform/6904ca3d.../recordings/voice-calls/01_01_2026-email1-email2-uuid/info.json

Downloads to local path:
./downloaded_jsons/6904ca3d.../recordings/voice-calls/01_01_2026-email1-email2-uuid/info.json
```

When we upload, the relative path from `./downloaded_jsons/` becomes the S3 key:

```
Local:  ./downloaded_jsons/6904ca3d.../voice-calls/[folder]/info.json
                           └──────────────────┬──────────────────────┘
                                              ▼
S3:     s3://web-data-platform/6904ca3d.../voice-calls/[folder]/info.json
```

This ensures each cleaned file overwrites its exact original location.

**Result:**
- Original `info.json` files in S3 are replaced with cleaned versions
- PII is permanently removed from source bucket

---

#### How to Run Step 3

```bash
# Dry run first (see what would be uploaded)
aws s3 cp ./downloaded_jsons/ s3://web-data-platform/ \
  --recursive \
  --exclude "*" \
  --include "*/info.json" \
  --exclude "_*.json" \
  --exclude "*.log" \
  --dryrun

# Then run for real
aws s3 cp ./downloaded_jsons/ s3://web-data-platform/ \
  --recursive \
  --exclude "*" \
  --include "*/info.json" \
  --exclude "_*.json" \
  --exclude "*.log"
```

**Verify:**
```bash
# Check a random file to confirm PII is removed
aws s3 cp s3://web-data-platform/[projectId]/recordings/voice-calls/[any-folder]/info.json - | head -50
```

---

### Step 4: Move failed calls to rejected-calls folder

**Action:**
- Read `_failed_calls.json` manifest
- For each failed call folder:
  - Copy entire folder to `rejected-calls/`
  - Delete original folder from `voice-calls/`

**Source:** `[projectId]/recordings/voice-calls/[folder]/`
**Destination:** `[projectId]/recordings/rejected-calls/[folder]/`

**Note:** This moves ALL files in the folder (info.json, audio files, etc.)

---

#### How to Run Step 4

Save as `step4_move_failed.py`:
```python
import json
import os
import argparse
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

SOURCE_BUCKET = 'web-data-platform'
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
    
    old_s3_path = f"s3://{SOURCE_BUCKET}/{old_folder}/"
    new_s3_path = f"s3://{SOURCE_BUCKET}/{new_folder}/"
    
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


def step4_move_failed_calls(dry_run=False):
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
    step4_move_failed_calls(dry_run=args.dry_run)
```

Run:
```bash
# Dry run first
python3 step4_move_failed.py --dry-run

# Run for real with 20 parallel workers
python3 step4_move_failed.py --workers 20
```
```

Run:
```bash
# Dry run first (no changes made)
python step4_move_failed.py --dry-run

# Then run for real
python step4_move_failed.py
```

---

### Step 5: Upload valid calls to customer bucket

**Action:**
- Read `_passed_calls.json` manifest
- For each passed call folder:
  - Sanitize folder name (replace emails with empty string)
  - Sanitize audio filenames (replace emails with empty string)
  - Copy entire folder to customer bucket with new paths
  - `filePath` and `mergedFilePath` in info.json already updated in Step 2

> ⚠️ **Why Python?** This step requires reading the manifest, sanitizing paths, and renaming files. CLI cannot do this conditional logic.

**Path sanitization examples:**

| Type | Before | After |
|------|--------|-------|
| Folder | `01_01_2026-afulton753@gmail.com-dtpittman@hotmail.com-uuid/` | `01_01_2026---uuid/` |
| Merged audio | `merged_1767463316202.wav` | `merged_1767463316202.wav` (no email, unchanged) |
| Separate audio | `afulton753@gmail.com-uuid_audio_xxx.ogg` | `-uuid_audio_xxx.ogg` |

**Source:** `web-data-platform/[projectId]/recordings/voice-calls/[folder]/`
**Destination:** `customer-bucket/[projectId]/recordings/voice-calls/[sanitized-folder]/`

**Files to copy (with renaming):**
```
[sanitized-folder]/
├── info.json                           # Already cleaned in Step 2
├── merged_xxx.wav                      # Copy as-is (no email in filename)
└── separate/
    ├── -uuid_audio_xxx.ogg             # Renamed from email-uuid_audio_xxx.ogg
    └── -uuid_audio_xxx.ogg             # Renamed from email-uuid_audio_xxx.ogg
```

---

#### How to Run Step 5

Save as `step5_upload_customer.py`:
```python
import json
import os
import re
import argparse
import subprocess
import tempfile
import shutil
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

SOURCE_BUCKET = 'web-data-platform'
CUSTOMER_BUCKET = 'customer-bucket'  # TODO: Update with actual bucket name
LOCAL_DIR = './downloaded_jsons'
MAX_WORKERS = 20  # Parallel threads


def sanitize_path(path):
    """Remove emails from paths (replace with empty string)"""
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return re.sub(email_pattern, '', path)


def log_operation(log_file, status, folder, result):
    """Write to processing log"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_file, 'a') as f:
        f.write(f"{timestamp} | {status:<10} | {folder} | {result}\n")


def upload_single_folder(call, dry_run, log_file):
    """Upload a single folder to customer bucket with sanitized paths"""
    old_folder = call['folder']
    new_folder = call['sanitized_folder']
    
    old_s3_path = f"s3://{SOURCE_BUCKET}/{old_folder}/"
    new_s3_path = f"s3://{CUSTOMER_BUCKET}/{new_folder}/"
    
    try:
        if dry_run:
            print(f"  Would upload: {old_folder} -> {new_folder}")
            return True
        
        # Create temp directory for this folder
        with tempfile.TemporaryDirectory() as temp_dir:
            # Download folder from source
            download_cmd = ['aws', 's3', 'sync', old_s3_path, temp_dir, '--quiet']
            subprocess.run(download_cmd, check=True, capture_output=True)
            
            # Rename files locally to sanitize emails from filenames
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    old_file_path = os.path.join(root, file)
                    new_file_name = sanitize_path(file)
                    new_file_path = os.path.join(root, new_file_name)
                    
                    if old_file_path != new_file_path:
                        shutil.move(old_file_path, new_file_path)
                    
                    # Replace info.json with cleaned local version
                    if file == 'info.json':
                        local_info_path = os.path.join(LOCAL_DIR, old_folder, 'info.json')
                        if os.path.exists(local_info_path):
                            shutil.copy(local_info_path, new_file_path)
            
            # Upload to customer bucket with sanitized folder name
            upload_cmd = ['aws', 's3', 'sync', temp_dir, new_s3_path, '--quiet']
            subprocess.run(upload_cmd, check=True, capture_output=True)
            
            log_operation(log_file, 'CUSTOMER', new_folder, 'SUCCESS')
        
        return True
        
    except subprocess.CalledProcessError as e:
        log_operation(log_file, 'CUST_ERR', old_folder, str(e))
        print(f"  ERROR: {old_folder} - {e}")
        return False


def step5_upload_to_customer(dry_run=False):
    """Upload valid calls to customer bucket with sanitized paths using parallel processing"""
    log_file = os.path.join(LOCAL_DIR, '_processing.log')
    
    with open(os.path.join(LOCAL_DIR, '_passed_calls.json'), 'r') as f:
        passed_calls = json.load(f)
    
    if dry_run:
        print("🔍 DRY RUN MODE - No files will be uploaded\n")
    
    total = len(passed_calls)
    print(f"{'[DRY RUN] ' if dry_run else ''}Uploading {total} valid calls to {CUSTOMER_BUCKET} with {MAX_WORKERS} parallel workers...")
    
    uploaded_count = 0
    error_count = 0
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(upload_single_folder, call, dry_run, log_file): call 
            for call in passed_calls
        }
        
        for future in as_completed(futures):
            if future.result():
                uploaded_count += 1
            else:
                error_count += 1
            
            if (uploaded_count + error_count) % 100 == 0:
                print(f"  Progress: {uploaded_count + error_count}/{total} ({uploaded_count} success, {error_count} errors)")
    
    print(f"\n{'[DRY RUN] ' if dry_run else ''}✅ {'Would upload' if dry_run else 'Uploaded'} {uploaded_count} valid calls to {CUSTOMER_BUCKET}")
    if error_count > 0:
        print(f"⚠️  {error_count} errors - check _processing.log")
    
    if dry_run:
        print("\n🔍 Run without --dry-run to apply changes")
    
    return uploaded_count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Upload valid calls to customer bucket')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without uploading files')
    parser.add_argument('--workers', type=int, default=20, help='Number of parallel workers (default: 20)')
    args = parser.parse_args()
    
    MAX_WORKERS = args.workers
    step5_upload_to_customer(dry_run=args.dry_run)
```

Run:
```bash
# Update CUSTOMER_BUCKET in the script first!

# Dry run first
python3 step5_upload_customer.py --dry-run

# Run for real with parallel workers
python3 step5_upload_customer.py --workers 20

# For 4.6TB, increase workers
python3 step5_upload_customer.py --workers 50
```

**Verify:**
```bash
# Check for errors
grep "CUST_ERR" ./downloaded_jsons/_processing.log

# List files in customer bucket
aws s3 ls s3://customer-bucket/ --recursive --summarize | tail -5

# Verify no emails in paths
aws s3 ls s3://customer-bucket/ --recursive | grep -E '[a-zA-Z0-9._%+-]+@' | head -10
```

---

## Rollback Plan

If something goes wrong:

1. **Step 2 fails:** Original files still in `./downloaded_jsons/`, just re-run
2. **Step 3 fails:** Restore from `./backup_originals_[timestamp]/` and re-upload to S3
3. **Step 4 fails:** Check both `voice-calls/` and `rejected-calls/` for duplicates, use `_processing.log` to identify last successful operation
4. **Step 5 fails:** Safe to re-run; customer bucket is additive only, use `_processing.log` to resume from last successful upload

**Recovery using logs:**
```bash
# Check last successful operation
tail -100 ./downloaded_jsons/_processing.log

# Find failed operations
grep "ERROR" ./downloaded_jsons/_processing.log
```

**Restore original info.json from backup:**
```bash
# If Step 3 corrupted files, restore from backup
aws s3 cp ./backup_originals_[timestamp]/ s3://web-data-platform/ \
  --recursive \
  --exclude "*" \
  --include "*/info.json"
```

**Recommendation:** Enable S3 versioning on `web-data-platform` before running, so original files can be recovered if needed.

---

## Execution Order

```
┌─────────────────────────────────────────────────────────┐
│  Step 1: Download info.json files                       │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Step 2: Process locally (clean PII + categorize)       │
└─────────────────────┬───────────────────────────────────┘
                      ▼
            ┌─────────────────────┐
            │  Review manifests   │  ← Manual checkpoint
            │  before proceeding  │
            └─────────┬───────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Step 3: Re-upload cleaned info.json                    │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Step 4: Move failed calls to rejected-calls/           │
└─────────────────────┬───────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│  Step 5: Upload valid calls to customer bucket          │
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
| Step 1: Download info.json | ~5-10 min |
| Step 2: Process locally | ~5-10 min |
| Step 3: Re-upload info.json | ~5-10 min |
| Step 4: Move failed calls | ~30-60 min (depends on % failed) |
| Step 5: Upload to customer | ~2-4 hours (4.6TB) |
| **Total** | **~3-5 hours** |
