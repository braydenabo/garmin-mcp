import hashlib
import io
import urllib.error
import zipfile
from pathlib import Path
from typing import Any

import pytest

from garmin_connect_personal_mcp import tunnel_install as install


def release_zip(name: str = "tunnel-client") -> bytes:
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        archive.writestr(name, b"synthetic executable")
    return contents.getvalue()


@pytest.fixture
def release(monkeypatch: pytest.MonkeyPatch) -> bytes:
    contents = release_zip()
    checksum = hashlib.sha256(contents).hexdigest()
    monkeypatch.setattr(install, "target_platform", lambda: ("darwin", "arm64"))

    def download(url: str, destination: Path, limit: int) -> str:
        data = (
            f"{checksum}  tunnel-client-v1.2.3-darwin-arm64.zip\n".encode()
            if url.endswith("SHA256SUMS.txt")
            else contents
        )
        destination.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(install, "_download", download)
    return contents


def test_installs_verified_release_atomically(release: bytes, tmp_path: Path) -> None:
    destination = tmp_path / "tools" / "tunnel-client"
    assert install.install(destination) == destination
    assert destination.read_bytes() == b"synthetic executable"
    assert list(destination.parent.iterdir()) == [destination]


def test_existing_binary_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "tunnel-client"
    destination.write_bytes(b"existing")
    monkeypatch.setattr(install, "target_platform", lambda: pytest.fail("unexpected installation"))
    assert install.install(destination) == destination
    assert destination.read_bytes() == b"existing"


def test_bad_checksum_leaves_nothing_installed(
    release: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def corrupt(url: str, destination: Path, limit: int) -> str:
        destination.write_bytes(b"0" * 64 + b"  tunnel-client-v1.2.3-darwin-arm64.zip\n")
        return "1" * 64

    monkeypatch.setattr(install, "_download", corrupt)
    destination = tmp_path / "tools" / "tunnel-client"
    with pytest.raises(install.InstallError, match="checksum"):
        install.install(destination)
    assert not destination.exists()
    assert not list(destination.parent.iterdir())


@pytest.mark.parametrize(
    "system,arch", [("darwin", "arm64"), ("linux", "amd64"), ("windows", "arm64")]
)
def test_selects_exact_platform_asset(system: str, arch: str) -> None:
    sums = f"{'a' * 64}  tunnel-client-v1.2.3-{system}-{arch}.zip\n"
    sums += f"{'b' * 64}  tunnel-client-runtime-v1.2.3-{system}-{arch}.zip\n"
    assert install.select_asset(sums, system, arch) == (
        "a" * 64,
        f"tunnel-client-v1.2.3-{system}-{arch}.zip",
        "v1.2.3",
    )


@pytest.mark.parametrize(
    "sums", ["", "not-a-checksum", f"{'a' * 64}  ../tunnel-client-v1.2.3-darwin-arm64.zip"]
)
def test_missing_or_malformed_release_is_rejected(sums: str) -> None:
    with pytest.raises(install.InstallError):
        install.select_asset(sums, "darwin", "arm64")


@pytest.mark.parametrize("name", ["../../tunnel-client", "folder/tunnel-client", "not-the-binary"])
def test_does_not_extract_unexpected_archive_paths(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "download.zip"
    archive.write_bytes(release_zip(name))
    destination = tmp_path / "tunnel-client"
    with pytest.raises(install.InstallError, match="layout"):
        install._unpack(archive, destination, "tunnel-client")
    assert not destination.exists()


def test_windows_binary_is_supported(tmp_path: Path) -> None:
    archive = tmp_path / "download.zip"
    archive.write_bytes(release_zip("tunnel-client.exe"))
    destination = tmp_path / "tunnel-client.exe"
    install._unpack(archive, destination, "tunnel-client.exe")
    assert destination.read_bytes() == b"synthetic executable"


@pytest.mark.parametrize(
    "machine,expected", [("aarch64", "arm64"), ("x86_64", "amd64"), ("AMD64", "amd64")]
)
def test_architecture_aliases(monkeypatch: pytest.MonkeyPatch, machine: str, expected: str) -> None:
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.platform, "machine", lambda: machine)
    assert install.target_platform() == ("linux", expected)


def test_unsupported_platform_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.platform, "system", lambda: "unsupported")
    with pytest.raises(install.InstallError, match="supports"):
        install.target_platform()


def test_download_enforces_source_and_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(install.InstallError, match="official"):
        install._download("file:///etc/passwd", tmp_path / "bad", 100)
    monkeypatch.setattr(
        install.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"too large")
    )
    with pytest.raises(install.InstallError, match="size limit"):
        install._download(install.RELEASES + "/test", tmp_path / "download", 2)


def test_network_failure_is_actionable(
    release: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> str:
        raise urllib.error.HTTPError("https://github.com", 403, "Forbidden", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(install, "_download", fail)
    with pytest.raises(install.InstallError, match="HTTP 403"):
        install.install(tmp_path / "tunnel-client")
