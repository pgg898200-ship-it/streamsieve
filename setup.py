"""Setuptools build script compiling the zero-dependency C++20/AVX2 streamsieve runtime."""

from __future__ import annotations

import os
import platform
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class _CTypesBuildExt(build_ext):
    """Build a plain ctypes-loadable shared library without requiring PyInit_*."""

    def get_export_symbols(self, ext: Extension) -> list[str]:
        return []

    def get_ext_filename(self, ext_name: str) -> str:
        parts = ext_name.split(".")
        if sys.platform == "darwin":
            suffix = ".dylib"
        elif sys.platform == "win32":
            suffix = ".dll"
        else:
            suffix = ".so"
        return os.path.join(*parts[:-1], parts[-1] + suffix)


def _compile_args() -> list[str]:
    if sys.platform == "win32":
        args = ["/std:c++20", "/O2", "/fp:precise"]
        if os.environ.get("STREAMSIEVE_NO_AVX2") != "1":
            args.append("/arch:AVX2")
        return args

    args = ["-std=c++20", "-O3", "-ffp-contract=off", "-fPIC"]
    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64") and os.environ.get("STREAMSIEVE_NO_AVX2") != "1":
        if os.environ.get("STREAMSIEVE_MARCH_NATIVE") == "1":
            args.append("-march=native")
        else:
            args.extend(["-mavx2", "-mfma"])
    return args


def _link_args() -> list[str]:
    if sys.platform == "darwin":
        return ["-Wl,-undefined,dynamic_lookup"]
    if sys.platform.startswith("linux"):
        return ["-lm"]
    return []


ext_modules = [
    Extension(
        name="streamsieve._libstreamsieve",
        sources=["csrc/streamsieve_capi.cpp"],
        include_dirs=["csrc"],
        language="c++",
        extra_compile_args=_compile_args(),
        extra_link_args=_link_args(),
    )
]

setup(
    ext_modules=ext_modules,
    cmdclass={"build_ext": _CTypesBuildExt},
)
