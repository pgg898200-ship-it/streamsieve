#define PY_SSIZE_T_CLEAN
#include <Python.h>

/* The Python wrapper uses ctypes to share one ABI with the standalone C build. */
static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_tailstate_native",
    "Native runtime for the tailstate module.",
    -1,
    NULL
};

PyMODINIT_FUNC PyInit__tailstate_native(void)
{
    return PyModule_Create(&module);
}
