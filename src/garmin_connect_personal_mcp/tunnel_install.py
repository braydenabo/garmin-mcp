"""Install the official tunnel client privately, with a release checksum check."""

import hashlib
import os
import platform
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import platformdirs

from garmin_connect_personal_mcp.config import APP_NAME

RELEASES = "https://github.com/openai/tunnel-client/releases"
MAX_DOWNLOAD = 256 * 1024 * 1024


class InstallError(Exception):
    """An installation failure that is safe to show in the terminal."""


def installed_path() -> Path:
    name = "tunnel-client.exe" if os.name == "nt" else "tunnel-client"
    return platformdirs.user_data_path(APP_NAME, appauthor=False) / "tools" / name


def target_platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "amd64": "amd64", "x86_64": "amd64"}
    if system not in ("darwin", "linux", "windows") or machine not in arch:
        raise InstallError("Automatic installation supports macOS, Linux and Windows on ARM64/x64.")
    return system, arch[machine]


def _download(url: str, destination: Path, limit: int) -> str:
    """Download only from the official release namespace and return its SHA-256."""
    if not url.startswith(RELEASES + "/"):
        raise InstallError("The download URL is not an official OpenAI tunnel-client release.")
    request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})  # noqa: S310
    digest = hashlib.sha256()
    total = 0
    with (
        urllib.request.urlopen(request, timeout=30) as response,  # noqa: S310
        destination.open("xb") as output,
    ):
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > limit:
                raise InstallError("The release download exceeded the expected size limit.")
            output.write(block)
            digest.update(block)
    return digest.hexdigest()


def select_asset(checksums: str, system: str, arch: str) -> tuple[str, str, str]:
    pattern = re.compile(
        rf"([a-fA-F0-9]{{64}})\s+\*?(tunnel-client-(v[0-9][0-9A-Za-z.+-]*)-"
        rf"{re.escape(system)}-{re.escape(arch)}\.zip)"
    )
    matches = [
        match for line in checksums.splitlines() if (match := pattern.fullmatch(line.strip()))
    ]
    if len(matches) != 1:
        raise InstallError("No unique official release was found for this computer.")
    checksum, filename, tag = matches[0].groups()
    return checksum.lower(), filename, tag


def _unpack(archive: Path, destination: Path, binary_name: str) -> None:
    with zipfile.ZipFile(archive) as release:
        # Copy one named file; never extract archive paths or links onto the filesystem.
        candidates = [info for info in release.infolist() if info.filename == binary_name]
        if len(candidates) != 1:
            raise InstallError("The official archive has an unexpected executable layout.")
        info = candidates[0]
        if info.is_dir() or not 0 < info.file_size <= MAX_DOWNLOAD:
            raise InstallError("The executable has an unexpected size.")
        with release.open(info) as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output)
    destination.chmod(0o700)


def install(destination: Path | None = None) -> Path:
    """Install if absent. Existing installations are left untouched."""
    destination = installed_path() if destination is None else destination
    if destination.is_file():
        return destination
    system, arch = target_platform()
    try:
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tunnel-install-", dir=destination.parent) as tmp:
            staging = Path(tmp)
            sums = staging / "SHA256SUMS.txt"
            print("Finding the latest official OpenAI tunnel-client release...", flush=True)
            _download(f"{RELEASES}/latest/download/SHA256SUMS.txt", sums, 128 * 1024)
            checksum, filename, tag = select_asset(sums.read_text(encoding="utf-8"), system, arch)
            print(f"Downloading {tag} for {system}/{arch}...", flush=True)
            archive = staging / "release.zip"
            actual = _download(f"{RELEASES}/download/{tag}/{filename}", archive, MAX_DOWNLOAD)
            if actual != checksum:
                raise InstallError(
                    "Download checksum mismatch. Nothing was installed; retry setup."
                )
            binary = staging / destination.name
            _unpack(
                archive, binary, "tunnel-client.exe" if system == "windows" else "tunnel-client"
            )
            # No partial executable is visible to subsequent runs.
            os.replace(binary, destination)
        print("Download checksum verified. Installed the tunnel client.", flush=True)
        return destination
    except urllib.error.HTTPError as exc:
        raise InstallError(
            f"The release download returned HTTP {exc.code}. Check network access and retry."
        ) from None
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise InstallError(
            "Could not download or install tunnel-client. Check network access and folder "
            "permissions, then retry setup."
        ) from exc
