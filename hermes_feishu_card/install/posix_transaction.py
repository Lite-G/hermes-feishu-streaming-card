"""Directory-bound file writes and rollback-capable POSIX transactions.

CLI chooses the install/restore plan. This module owns directory descriptors,
file snapshots, staged bytes, exclusive replacement and rollback verification.
It never loads CLI, Hermes detection, configuration or network services.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Callable
from uuid import uuid4

from .file_transaction import (
    _RestoreIdentity,
    _RestoreEvidenceSnapshot,
    _CLI_SNAPSHOT_UNSET,
    _assert_restore_directory_ancestry_unchanged,
    _atomic_write_text_portable,
)


class _CliTargetBinding:
    def __init__(
        self,
        path: Path,
        parent_fd: int,
        parent_identity: _RestoreIdentity,
        initial_snapshot: _RestoreEvidenceSnapshot | None,
        initial_bytes: bytes | None,
        initial_mode: int | None,
    ) -> None:
        self.path = path
        self.parent_fd = parent_fd
        self.parent_identity = parent_identity
        self.initial_snapshot = initial_snapshot
        self.initial_bytes = initial_bytes
        self.initial_mode = initial_mode

    @property
    def basename(self) -> str:
        return self.path.name

    def close(self) -> None:
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


class _CliStagedText:
    def __init__(
        self,
        basename: str,
        identity: _RestoreIdentity,
        digest: str,
        mode: int,
    ) -> None:
        self.basename = basename
        self.identity = identity
        self.digest = digest
        self.mode = mode
        self.consumed = False


_CLI_DIRFD_BINDING_SUPPORTED = (
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "fchmod")
    and all(
        operation in getattr(os, "supports_dir_fd", set())
        for operation in (os.open, os.stat, os.unlink, os.rename)
    )
)


def _cli_dirfd_binding_supported() -> bool:
    return _CLI_DIRFD_BINDING_SUPPORTED


def _atomic_write_text(
    path: Path,
    contents: str,
    *,
    mode: int | None = None,
    _binding: _CliTargetBinding | None = None,
    _expected_before: _RestoreEvidenceSnapshot | None | object = _CLI_SNAPSHOT_UNSET,
    _enforce_path_parent: bool = True,
) -> _RestoreEvidenceSnapshot:
    if not _cli_dirfd_binding_supported():
        if _binding is not None:
            raise ValueError(
                "secure bound write requires directory-relative filesystem "
                "operations on this platform"
            )
        return _atomic_write_text_portable(
            path,
            contents,
            mode=mode,
            expected_before=_expected_before,
        )
    owns_binding = _binding is None
    binding = _binding if _binding is not None else _bind_cli_target(path)
    if binding.path != path:
        if owns_binding:
            binding.close()
        raise ValueError("atomic write binding does not match target")
    expected_before = (
        binding.initial_snapshot
        if _expected_before is _CLI_SNAPSHOT_UNSET
        else _expected_before
    )
    rollback_stage: _CliStagedText | None = None
    write_stage: _CliStagedText | None = None
    try:
        _assert_bound_cli_parent(binding)
        current_snapshot, current_bytes, current_mode = _read_bound_cli_target_state(
            binding.parent_fd, binding.basename
        )
        if current_snapshot != expected_before:
            raise ValueError("refusing to replace a changed target")
        if current_bytes is not None:
            rollback_stage = _stage_bound_cli_bytes(
                binding,
                current_bytes,
                current_mode if current_mode is not None else 0o600,
            )
        selected_mode = mode if mode is not None else current_mode
        write_stage = _stage_bound_cli_bytes(
            binding,
            contents.encode("utf-8"),
            selected_mode if selected_mode is not None else 0o600,
        )
        committed = _replace_bound_cli_stage(
            binding,
            write_stage,
            expected_before=current_snapshot,
        )
        if _enforce_path_parent:
            try:
                _assert_cli_path_parent_bound(binding)
            except (OSError, ValueError) as exc:
                try:
                    if current_snapshot is None:
                        _unlink_bound_cli_target(binding, committed)
                    else:
                        if rollback_stage is None:
                            raise ValueError("atomic write lost rollback stage")
                        restored = _replace_bound_cli_stage(
                            binding,
                            rollback_stage,
                            expected_before=committed,
                        )
                        if restored[2] != current_snapshot[2]:
                            raise ValueError("atomic write rollback verification failed")
                except (OSError, ValueError) as rollback_exc:
                    raise ValueError(
                        "atomic write rollback failed; manual review required"
                    ) from rollback_exc
                raise ValueError("directory changed during write") from exc
        return committed
    finally:
        for staged in (write_stage, rollback_stage):
            if staged is not None:
                _cleanup_bound_cli_stage(binding, staged)
        if owns_binding:
            binding.close()


def _bind_cli_target(path: Path) -> _CliTargetBinding:
    parent_fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(path.parent, flags)
        opened_parent = os.fstat(parent_fd)
        current_parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or not stat.S_ISDIR(current_parent.st_mode)
            or (opened_parent.st_dev, opened_parent.st_ino)
            != (current_parent.st_dev, current_parent.st_ino)
        ):
            raise ValueError("refusing to stage through a changed directory")
        snapshot, payload, file_mode = _read_bound_cli_target_state(
            parent_fd, path.name
        )
        binding = _CliTargetBinding(
            path,
            parent_fd,
            (opened_parent.st_dev, opened_parent.st_ino),
            snapshot,
            payload,
            file_mode,
        )
        parent_fd = -1
        return binding
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _assert_bound_cli_parent(binding: _CliTargetBinding) -> None:
    current = os.fstat(binding.parent_fd)
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != binding.parent_identity
    ):
        raise ValueError("bound target directory changed during write")


def _assert_cli_path_parent_bound(binding: _CliTargetBinding) -> None:
    try:
        current = binding.path.parent.lstat()
    except FileNotFoundError as exc:
        raise ValueError("directory changed during write") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != binding.parent_identity
    ):
        raise ValueError("directory changed during write")


def _stage_bound_cli_bytes(
    binding: _CliTargetBinding,
    payload: bytes,
    mode: int,
) -> _CliStagedText:
    _assert_bound_cli_parent(binding)
    basename = f".{binding.basename}.{uuid4().hex}.tmp"
    descriptor = -1
    staged: _CliStagedText | None = None
    try:
        descriptor = os.open(
            basename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=binding.parent_fd,
        )
        snapshot = os.fstat(descriptor)
        staged = _CliStagedText(
            basename,
            (snapshot.st_dev, snapshot.st_ino),
            hashlib.sha256(payload).hexdigest(),
            mode,
        )
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, mode)
        os.close(descriptor)
        descriptor = -1
        return staged
    except Exception:
        if staged is not None:
            _cleanup_bound_cli_stage(binding, staged)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _replace_bound_cli_stage(
    binding: _CliTargetBinding,
    staged: _CliStagedText,
    *,
    expected_before: _RestoreEvidenceSnapshot | None,
) -> _RestoreEvidenceSnapshot:
    _assert_bound_cli_parent(binding)
    if _read_bound_cli_target(binding.parent_fd, binding.basename) != expected_before:
        raise ValueError("refusing to replace a changed target")
    current_stage = os.stat(
        staged.basename,
        dir_fd=binding.parent_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(current_stage.st_mode)
        or (current_stage.st_dev, current_stage.st_ino) != staged.identity
    ):
        raise ValueError("refusing to replace a changed staged file")
    os.replace(
        staged.basename,
        binding.basename,
        src_dir_fd=binding.parent_fd,
        dst_dir_fd=binding.parent_fd,
    )
    staged.consumed = True
    committed = _read_bound_cli_target(binding.parent_fd, binding.basename)
    expected_committed = (*staged.identity, staged.digest)
    if committed != expected_committed:
        raise ValueError("could not verify committed target")
    return expected_committed


def _unlink_bound_cli_target(
    binding: _CliTargetBinding,
    expected_snapshot: _RestoreEvidenceSnapshot,
) -> None:
    _assert_bound_cli_parent(binding)
    if _read_bound_cli_target(binding.parent_fd, binding.basename) != expected_snapshot:
        raise ValueError("refusing to unlink a changed target")
    os.unlink(binding.basename, dir_fd=binding.parent_fd)
    if _read_bound_cli_target(binding.parent_fd, binding.basename) is not None:
        raise ValueError("could not verify removed target")


def _cleanup_bound_cli_stage(
    binding: _CliTargetBinding,
    staged: _CliStagedText,
) -> None:
    if staged.consumed or binding.parent_fd < 0:
        return
    try:
        current = os.stat(
            staged.basename,
            dir_fd=binding.parent_fd,
            follow_symlinks=False,
        )
        if (
            stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino) == staged.identity
        ):
            os.unlink(staged.basename, dir_fd=binding.parent_fd)
            staged.consumed = True
    except OSError:
        pass


def _read_bound_cli_target(
    parent_fd: int, basename: str
) -> tuple[int, int, str] | None:
    snapshot, _payload, _mode = _read_bound_cli_target_state(parent_fd, basename)
    return snapshot


def _read_bound_cli_target_state(
    parent_fd: int, basename: str
) -> tuple[_RestoreEvidenceSnapshot | None, bytes | None, int | None]:
    try:
        before = os.stat(
            basename,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None, None, None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("refusing to use a non-regular target")
    identity = (before.st_dev, before.st_ino)
    descriptor = -1
    chunks: list[bytes] = []
    try:
        descriptor = os.open(
            basename,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != identity
        ):
            raise ValueError("target changed while being verified")
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    after = os.stat(
        basename,
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino) != identity
    ):
        raise ValueError("target changed while being verified")
    payload = b"".join(chunks)
    return (
        (before.st_dev, before.st_ino, hashlib.sha256(payload).hexdigest()),
        payload,
        stat.S_IMODE(before.st_mode),
    )

def _write_targets_transactionally(
    changes: list[tuple[Path, str]],
    *,
    expected_identities: dict[Path, _RestoreEvidenceSnapshot | None] | None = None,
    expected_directories: dict[Path, _RestoreIdentity | None] | None = None,
    preserve_earlier_writes_on_rollback_failure: bool = False,
    pre_commit_validate: Callable[[], None] | None = None,
    validate: Callable[[], None] | None = None,
) -> None:
    if not _cli_dirfd_binding_supported():
        raise ValueError(
            "secure restore transaction requires directory-relative "
            "filesystem operations on this platform"
        )
    bindings: dict[Path, _CliTargetBinding] = {}
    snapshots: list[tuple[Path, str | None]] = []
    try:
        for path, _contents in changes:
            if expected_directories is not None:
                _assert_restore_directory_ancestry_unchanged(
                    path, expected_directories
                )
            binding = _bind_cli_target(path)
            bindings[path] = binding
            if expected_directories is not None:
                _assert_restore_directory_ancestry_unchanged(
                    path, expected_directories
                )
                expected_parent = expected_directories.get(path.parent)
                if (
                    expected_parent is not None
                    and binding.parent_identity != expected_parent
                ):
                    raise ValueError(
                        f"{path.parent.name} directory changed during restore; "
                        "refusing to restore"
                    )
            if expected_identities is not None:
                expected_snapshot = expected_identities.get(path)
                if binding.initial_snapshot != expected_snapshot:
                    raise ValueError(
                        f"{path.name} changed during restore; refusing to clean up"
                    )
            snapshots.append(
                (
                    path,
                    binding.initial_bytes.decode("utf-8")
                    if binding.initial_bytes is not None
                    else None,
                )
            )

        if pre_commit_validate is not None:
            pre_commit_validate()
        written: list[Path] = []
        post_write_snapshots: dict[Path, _RestoreEvidenceSnapshot] = {}
        try:
            for path, contents in changes:
                binding = bindings[path]
                post_write_snapshots[path] = _atomic_write_text(
                    path,
                    contents,
                    _binding=binding,
                    _expected_before=binding.initial_snapshot,
                )
                written.append(path)
            for path in written:
                if expected_directories is not None:
                    _assert_restore_directory_ancestry_unchanged(
                        path, expected_directories
                    )
                binding = bindings[path]
                _assert_cli_path_parent_bound(binding)
                if (
                    _read_bound_cli_target(binding.parent_fd, binding.basename)
                    != post_write_snapshots[path]
                ):
                    raise ValueError("restore transaction lost write ownership")
            if validate is not None:
                validate()
        except (OSError, UnicodeError, ValueError) as exc:
            rollback_failed = False
            snapshot_by_path = dict(snapshots)
            for path in reversed(written):
                previous = snapshot_by_path[path]
                try:
                    post_write_snapshot = post_write_snapshots.get(path)
                    if post_write_snapshot is None:
                        raise ValueError("restore transaction lost write ownership")
                    binding = bindings[path]
                    if previous is None:
                        _unlink_bound_cli_target(binding, post_write_snapshot)
                    else:
                        _atomic_write_text(
                            path,
                            previous,
                            _binding=binding,
                            _expected_before=post_write_snapshot,
                            _enforce_path_parent=False,
                        )
                except (OSError, UnicodeError, ValueError):
                    rollback_failed = True
                    if preserve_earlier_writes_on_rollback_failure:
                        break
            if rollback_failed:
                raise ValueError(
                    "restore transaction rollback failed; manual review required"
                ) from exc
            raise
    finally:
        for binding in bindings.values():
            try:
                binding.close()
            except OSError:
                pass


