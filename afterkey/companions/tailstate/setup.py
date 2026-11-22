from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "_tailstate_native",
            sources=["src/tailstate.c", "python/native_module.c"],
            include_dirs=["include"],
            depends=["include/tailstate.h"],
            # Windows extension DLLs require explicit exports for ctypes.
            export_symbols=[
                "ts_create", "ts_create_indexed", "ts_destroy", "ts_head",
                "ts_store_bytes", "ts_put", "ts_delete", "ts_get",
                "ts_begin", "ts_resume", "ts_next", "ts_since",
            ],
        )
    ]
)
