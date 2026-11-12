"""Wire and storage bounds for a single prebuilt linux/amd64 image."""
UPLOAD_LIMIT = 128 * 1024**2
UNPACK_LIMIT = 512 * 1024**2
FILE_LIMIT = 1024
# Canonical tar metadata, configuration and padding are additional to layer bytes.
ARCHIVE_LIMIT = UNPACK_LIMIT + 4 * 1024**2
