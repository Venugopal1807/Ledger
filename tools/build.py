#!/usr/bin/env python3
"""Build dist/ledger.pyz, a standalone executable artifact.

    python3 tools/build.py              # build dist/ledger.pyz
    python3 tools/build.py --output P   # build somewhere else
    python3 tools/build.py --verify     # build twice, compare, exit non-zero
                                        # if the two differ

The output is byte-for-byte reproducible: the same source tree produces the
same bytes on any machine, in any directory, at any time. That is not a
property a zip file has by default, so every source of variance is pinned
here explicitly:

* member order is sorted, not filesystem traversal order
* timestamps are fixed, from SOURCE_DATE_EPOCH when set and the 1980 zip
  epoch otherwise, never the wall clock
* permissions are a constant in the archive, so a contributor's umask
  cannot leak into the output
* create_system is pinned to Unix, which zipfile would otherwise set from
  the building platform
* the compression level is pinned rather than left to the default
* no bytecode is included: .pyc files embed absolute paths and interpreter
  magic numbers, which are the opposite of reproducible

zipfile is used rather than zipapp.create_archive because that helper does
not expose the metadata control the guarantees above require.
"""

import argparse
import hashlib
import io
import os
import pathlib
import shutil
import sys
import tempfile
import time
import zipfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "dist" / "ledger.pyz"

# Exactly what the runtime needs, listed rather than discovered, so a new
# test file or scratch script can never end up in a shipped artifact.
RUNTIME_FILES = ("ledger.py",)

SHEBANG = b"#!/usr/bin/env python3\n"

# Zip timestamps cannot predate 1980; this is the earliest representable
# moment, and the conventional choice for reproducible archives.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# Constant in the archive regardless of what the files look like on disk.
FILE_MODE = 0o100644  # regular file, rw-r--r--
CREATE_SYSTEM_UNIX = 3
COMPRESS_LEVEL = 9

# The entry point. Fixed text with nothing generated into it: no build
# date, no hostname, no path.
MAIN_SHIM = '''"""Entry point for the ledger.pyz artifact."""

import sys

from ledger import main

if __name__ == "__main__":
    sys.exit(main())
'''


def archive_date_time(epoch=None):
    """The timestamp every member carries.

    Honours SOURCE_DATE_EPOCH so a distribution can pin builds to a commit
    date; otherwise the zip epoch, never the current time.
    """
    if epoch is None:
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch in (None, ""):
        return ZIP_EPOCH
    moment = time.gmtime(int(epoch))
    if moment.tm_year < 1980:
        return ZIP_EPOCH
    return (moment.tm_year, moment.tm_mon, moment.tm_mday,
            moment.tm_hour, moment.tm_min, moment.tm_sec)


def members(root):
    """(archive name, bytes) for every file in the artifact, sorted.

    Sorted by archive name rather than emitted in discovery order, so the
    filesystem's traversal order cannot reach the output.
    """
    contents = {"__main__.py": MAIN_SHIM.encode("utf-8")}
    for name in RUNTIME_FILES:
        source = root / name
        if not source.is_file():
            raise FileNotFoundError(f"runtime source missing: {source}")
        contents[name] = source.read_bytes()
    return sorted(contents.items())


def build_bytes(root=REPO_ROOT, epoch=None):
    """Produce the artifact in memory, so a partial file never lands on disk."""
    date_time = archive_date_time(epoch)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=COMPRESS_LEVEL) as archive:
        for name, payload in members(root):
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FILE_MODE << 16
            # zipfile picks this from the building platform otherwise.
            info.create_system = CREATE_SYSTEM_UNIX
            archive.writestr(info, payload, compresslevel=COMPRESS_LEVEL)
    return SHEBANG + buffer.getvalue()


def write_artifact(path, data):
    """Write the artifact and make it executable.

    The mode is set explicitly rather than left to the process umask, so
    the result does not depend on how the shell was configured.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def describe_difference(first, second):
    """Explain how two archives differ, rather than only that they do."""
    lines = []
    if len(first) != len(second):
        lines.append(f"size: {len(first)} vs {len(second)} bytes")

    def entries(data):
        with zipfile.ZipFile(io.BytesIO(data[len(SHEBANG):])) as archive:
            return {
                info.filename: (
                    info.date_time, info.external_attr, info.create_system,
                    info.compress_type, info.CRC, info.file_size,
                )
                for info in archive.infolist()
            }

    try:
        left, right = entries(first), entries(second)
    except zipfile.BadZipFile as error:
        lines.append(f"one archive is unreadable: {error}")
        return "\n".join(lines)

    for name in sorted(set(left) | set(right)):
        if name not in left:
            lines.append(f"{name}: only in the second build")
        elif name not in right:
            lines.append(f"{name}: only in the first build")
        elif left[name] != right[name]:
            fields = ("date_time", "external_attr", "create_system",
                      "compress_type", "crc", "file_size")
            for field, a, b in zip(fields, left[name], right[name]):
                if a != b:
                    lines.append(f"{name}: {field} {a!r} vs {b!r}")
    if list(left) != list(right):
        lines.append(f"member order: {list(left)} vs {list(right)}")
    return "\n".join(lines) or "archives differ in bytes but not in metadata"


def verify(root=REPO_ROOT):
    """Build twice into separate directories and compare the results."""
    built = []
    for label in ("a", "b"):
        with tempfile.TemporaryDirectory(prefix=f"ledger-build-{label}-") as tmp:
            data = build_bytes(root)
            write_artifact(pathlib.Path(tmp) / "ledger.pyz", data)
            built.append(data)

    first, second = built
    print(f"build 1: {digest(first)}  ({len(first)} bytes)")
    print(f"build 2: {digest(second)}  ({len(second)} bytes)")
    if first == second:
        print("reproducible: identical bytes")
        return 0
    print("NOT REPRODUCIBLE", file=sys.stderr)
    print(describe_difference(first, second), file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help="where to write the artifact")
    parser.add_argument("--verify", action="store_true",
                        help="build twice and compare instead of writing")
    args = parser.parse_args(argv)

    if args.verify:
        return verify()

    data = build_bytes()
    path = write_artifact(args.output, data)
    print(f"{path}  {digest(data)}  ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
