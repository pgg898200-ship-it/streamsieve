# RecastCRPS 0.1.0 Linux wheel

This binary targets CPython 3.12 on x86-64 Linux with the manylinux_2_24 platform tag. It was installed and tested on glibc 2.41. The minimum compatibility tag comes from auditwheel 6.8.2 symbol and library inspection. The SHA-256 digest of [the wheel](recastcrps-0.1.0-cp312-cp312-manylinux_2_24_x86_64.whl) is `3870ee020fe788d9218e25f5e7823770c21b66551b93610b0d3a91a9149a064e`.

The build used the [published Space source archive](https://huggingface.co/spaces/pgg-engineering/recastcrps/resolve/8b5a3d11d5fc3671c1d471a014b386570d320eee/recastcrps-0.1.0.tar.gz), SHA-256 `a0f29d0159932c85d52613770db944708eedd513a63395e5169a78e15130d746`. The extension source, header and setup.py matched release commit 28be03c. To repeat the build in a CPython 3.12 environment with GCC 14.2 and the downloaded archive, run

```sh
python -m pip install 'auditwheel==6.8.2' 'patchelf==0.19.3.0'
CC=gcc CXX=g++ CFLAGS='-O2 -g0 -march=x86-64 -mtune=generic' CXXFLAGS='-O2 -g0 -march=x86-64 -mtune=generic' python -m pip wheel --no-cache-dir --no-deps -w raw ./recastcrps-0.1.0.tar.gz
auditwheel repair --plat manylinux_2_24_x86_64 -w wheels raw/*.whl
auditwheel show wheels/*.whl
```

The November 27 checks included nine Python tests, 5,920 full and withdrawn xskillscore comparisons, all native comparator drivers and the five-method scoringrules comparison on 4,864 revisions. The 20,000-call weighted latency run measured 220 ns p50, 281 ns p95 and 371 ns p99 on one pinned AMD EPYC 9V74 logical CPU. The prebuilt Numba batch led on eight equal-weight members. Sorted prefix led for static native withdrawal queries and ordered Fenwick led on the tie-heavy trace. Raw results are in [verification](verification/).
