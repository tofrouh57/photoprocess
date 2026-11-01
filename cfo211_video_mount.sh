#!/bin/bash

# ==========================================
# Synology SMB mount script for macOS
# ==========================================
#
# mount folder has to belong to user tof
# step 1 create root folder
# sudo chown tof:staff /Volumes/root_cfo216
# sudo chmod 755 /Volumes/root_cfo216
# step 2 create mount folder
# mkdir -p /Volumes/root_cfo216/cfo216_media
# step 3 mount the share


NAS_IP="192.168.1.182"
SMB_USER="macbook"
SMB_PASSWORD="Tofer&2578$"
SHARE="video"

MOUNT_POINT="/Volumes/root_cfo211/cfo211_video"

echo "Checking Synology SMB mount..."

# Check mount point exists
if [ ! -d "$MOUNT_POINT" ]; then
    echo "ERROR: Mount point does not exist:"
    echo "$MOUNT_POINT"
    echo "Create it first with:"
    echo "sudo mkdir -p \"$MOUNT_POINT\""
    exit 1
fi

# Check whether already mounted
if mount | grep -F " on $MOUNT_POINT (" >/dev/null; then
    echo "Share is already mounted."

    if ls "$MOUNT_POINT" >/dev/null 2>&1; then
        echo "Access verified successfully."
        exit 0
    else
        echo "ERROR: Share is mounted but inaccessible."
        exit 1
    fi
fi

# Mount as current macOS user (no sudo)
echo "Mounting //$SMB_USER@$NAS_IP/$SHARE ..."

mount_smbfs  -d 0755 -f 0644 \
    "//$SMB_USER:$SMB_PASSWORD@$NAS_IP/$SHARE" \
    "$MOUNT_POINT"

if [ $? -ne 0 ]; then
    echo "ERROR: SMB mount failed."
    exit 1
fi

# Verify access
if ls "$MOUNT_POINT" >/dev/null 2>&1; then
    echo ""
    echo "SUCCESS: Synology share mounted."
    echo "Location: $MOUNT_POINT"
else
    echo "ERROR: Mount succeeded but access verification failed."
    exit 1
fi
