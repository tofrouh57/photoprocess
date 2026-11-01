#!/bin/bash

MOUNT_POINT="/Volumes/root_cfo211/cfo211_photo"

if mount | grep -F " on $MOUNT_POINT (" >/dev/null; then
    echo "Unmounting Synology share..."
    umount "$MOUNT_POINT"
    echo "Unmount completed."
else
    echo "Share is not mounted."
fi
