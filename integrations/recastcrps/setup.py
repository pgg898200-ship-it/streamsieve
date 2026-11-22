from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "recastcrps._core",
            sources=["python/recastcrps/module.cpp"],
            include_dirs=["include"],
            depends=["include/recastcrps.hpp"],
            language="c++",
            extra_compile_args=["-std=c++17"],
        )
    ]
)
