from __future__ import annotations

import pytest

from millrace_os.paths import UnsafePathError, ensure_directory, validate_file_target


@pytest.mark.parametrize("dangling", [False, True])
def test_symlink_ancestor_is_refused_even_below_existing_directory(tmp_path, dangling):
    real = tmp_path / "real"
    if not dangling:
        (real / "existing").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        ensure_directory(link / "existing" / "new")
    assert not (real / "existing" / "new").exists()


def test_unsafe_ancestor_is_not_chmodded(tmp_path):
    unsafe = tmp_path / "unsafe"
    (unsafe / "safe").mkdir(parents=True)
    unsafe.chmod(0o777)
    with pytest.raises(UnsafePathError):
        ensure_directory(unsafe / "safe")
    assert unsafe.stat().st_mode & 0o777 == 0o777


def test_dangling_file_target_is_refused(tmp_path):
    target = tmp_path / "credential"
    target.symlink_to(tmp_path / "absent")
    with pytest.raises(UnsafePathError):
        validate_file_target(target)
    assert not (tmp_path / "absent").exists()
