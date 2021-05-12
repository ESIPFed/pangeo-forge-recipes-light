import hashlib
import logging
import os
import re
import tempfile
import unicodedata
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Union

import fsspec

logger = logging.getLogger(__name__)

# fsspec doesn't provide type hints, so I'm not sure what the write type is for open files
OpenFileType = Any


def _get_url_size(fname):
    with fsspec.open(fname, mode="rb") as of:
        size = of.size
    return size


@contextmanager
def _fsspec_safe_open(fname: str, **kwargs) -> Iterator[OpenFileType]:
    # workaround for inconsistent behavior of fsspec.open
    # https://github.com/intake/filesystem_spec/issues/579
    with fsspec.open(fname, **kwargs) as fp:
        with fp as fp2:
            yield fp2


def _copy_btw_filesystems(input_opener, output_opener, BLOCK_SIZE=10_000_000):
    with input_opener as source:
        with output_opener as target:
            while True:
                data = source.read(BLOCK_SIZE)
                if not data:
                    break
                target.write(data)


class AbstractTarget(ABC):
    @abstractmethod
    def get_mapper(self):
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check that the file exists."""
        pass

    @abstractmethod
    def rm(self, path: str) -> None:
        """Remove file."""
        pass

    @contextmanager
    def open(self, path: str, **kwargs):  # don't know how to type hint this
        """Open file with a context manager."""
        pass

    @abstractmethod
    def size(self, path: str) -> int:
        """Get file size"""
        pass


def _hash_path(path: str) -> str:
    return str(hash(path))


@dataclass
class FSSpecTarget(AbstractTarget):
    """Representation of a storage target for Pangeo Forge.

    :param fs: The filesystem object we are writing to.
    :param root_path: The path under which the target data will be stored.
    """

    fs: fsspec.AbstractFileSystem
    root_path: str = ""

    def get_mapper(self) -> fsspec.mapping.FSMap:
        """Get a mutable mapping object suitable for storing Zarr data."""
        return self.fs.get_mapper(self.root_path)

    def _full_path(self, path: str):
        return os.path.join(self.root_path, path)

    def exists(self, path: str) -> bool:
        """Check that the file is in the cache."""
        return self.fs.exists(self._full_path(path))

    def rm(self, path: str) -> None:
        """Remove file from the cache."""
        self.fs.rm(self._full_path(path))

    def size(self, path: str) -> int:
        return self.fs.size(self._full_path(path))

    @contextmanager
    def open(self, path: str, **kwargs) -> Iterator[None]:
        """Open file with a context manager."""
        with self.fs.open(self._full_path(path), **kwargs) as f:
            yield f

    def __post_init__(self):
        if not self.fs.isdir(self.root_path):
            self.fs.mkdir(self.root_path)


class FlatFSSpecTarget(FSSpecTarget):
    """A target that sanitizes all the path names so that everthing is stored
    in a single directory.

    Designed to be used as a cache for inputs.
    """

    def _full_path(self, path: str) -> str:
        # this is just in case _slugify(path) is non-unique
        prefix = hashlib.md5(path.encode()).hexdigest()
        slug = _slugify(path)
        new_path = "-".join([prefix, slug])
        return os.path.join(self.root_path, new_path)


class CacheFSSpecTarget(FlatFSSpecTarget):
    """Alias for FlatFSSpecTarget"""

    def cache_file(self, fname: str, **open_kwargs) -> None:
        # check and see if the file already exists in the cache
        logger.info(f"Caching file '{fname}'")
        if self.exists(fname):
            cached_size = self.size(fname)
            remote_size = _get_url_size(fname)
            if cached_size == remote_size:
                # TODO: add checksumming here
                logger.info(f"File '{fname}' is already cached")
                return

        input_opener = _fsspec_safe_open(fname, mode="rb", **open_kwargs)
        target_opener = self.open(fname, mode="wb")
        logger.info(f"Coping remote file '{fname}' to cache")
        _copy_btw_filesystems(input_opener, target_opener)


@contextmanager
def file_opener(
    fname: str,
    cache: Optional[CacheFSSpecTarget] = None,
    copy_to_local: bool = False,
    **open_kwargs,
) -> Iterator[Union[OpenFileType, str]]:
    """
    Context manager for opening files.

    :param fname: The filename / url to open. Fsspec will inspect the protocol
        (e.g. http, ftp) and determine the appropriate filesystem type to use.
    :param cache: A target where the file may have been cached. If none, the file
        will be opened directly.
    :param copy_to_local: If True, always copy the file to a local temporary file
        before opening. In this case, function yields a path name rather than an open file.
    """
    if cache is not None:
        logger.info(f"Opening '{fname}' from cache")
        opener = cache.open(fname, mode="rb")
    else:
        logger.info(f"Opening  '{fname}' directly.")
        opener = _fsspec_safe_open(fname, mode="rb", **open_kwargs)
    if copy_to_local:
        _, suffix = os.path.splitext(fname)
        ntf = tempfile.NamedTemporaryFile(suffix=suffix)
        tmp_name = ntf.name
        logger.info(f"Copying '{fname}' to local file '{tmp_name}'")
        target_opener = open(tmp_name, mode="wb")
        _copy_btw_filesystems(opener, target_opener)
        yield tmp_name
        ntf.close()  # cleans up the temporary file
    else:
        with opener as fp:
            yield fp


def _slugify(value: str) -> str:
    # Adopted from
    # https://github.com/django/django/blob/master/django/utils/text.py
    # https://stackoverflow.com/questions/295135/turn-a-string-into-a-valid-filename
    value = str(value)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^.\w\s-]+", "_", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-_")


class UninitializedTarget(AbstractTarget):
    def get_mapper(self):
        raise UninitializedTargetError

    def exists(self, path: str) -> bool:
        return False

    def rm(self, path: str) -> None:
        raise UninitializedTargetError

    def open(self, path: str, **kwargs):  # don't know how to type hint this
        raise UninitializedTargetError

    def size(self, path: str, **kwargs):
        raise UninitializedTargetError


class TargetError(Exception):
    """Base class for exceptions in this module."""

    pass


class UninitializedTargetError(TargetError):
    """Operation on an uninitialized Target."""

    pass
