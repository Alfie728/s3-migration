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

**Run:**
```bash
# Dry run first (see what would be renamed)
python3 step1_copy_sanitized.py --dry-run

# Run for real (this will take a while for 4.6TB)
python3 step1_copy_sanitized.py --workers 50

# Process specific projects only (can specify multiple prefixes)
python3 step1_copy_sanitized.py --prefix 692801d5ce882a630401bee8/ 692801d6ce882a630401beee/ 6968e7dd2371cd2887b5799f/ --dry-run
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

**Run:**
```bash
# Dry run first (see what would be synced)
python3 step6_sync_customer.py --bucket customer-bucket-name --dry-run

# Sync specific projects only
python3 step6_sync_customer.py --bucket customer-bucket-name --prefix 692801d5ce882a630401bee8/ 692801d6ce882a630401beee/ 6968e7dd2371cd2887b5799f/ --dry-run

# Run for real
python3 step6_sync_customer.py --bucket customer-bucket-name
```

**Verify:**
```bash
CUSTOMER_BUCKET="customer-bucket-name"

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
