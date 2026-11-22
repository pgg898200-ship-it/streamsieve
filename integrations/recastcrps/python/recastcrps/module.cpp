#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <cmath>
#include <cstdint>
#include <exception>
#include <limits>
#include <memory>
#include <new>
#include <stdexcept>
#include <utility>
#include <vector>

#include "recastcrps.hpp"

namespace {
struct EnsembleObject {
    PyObject_HEAD
    recastcrps::Ensemble* engine;
};

struct Decref {
    void operator()(PyObject* obj) const { Py_DECREF(obj); }
};
using OwnedRef = std::unique_ptr<PyObject, Decref>;

PyObject* translate_exception() {
    try {
        throw;
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::out_of_range& error) {
        PyErr_SetString(PyExc_IndexError, error.what());
    } catch (const std::invalid_argument& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
    } catch (const std::domain_error& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
    } catch (const std::overflow_error& error) {
        PyErr_SetString(PyExc_OverflowError, error.what());
    } catch (const std::length_error& error) {
        PyErr_SetString(PyExc_OverflowError, error.what());
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
    } catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "unknown C++ exception");
    }
    return nullptr;
}

bool as_int32(PyObject* obj, int32_t& out) {
    const long long value = PyLong_AsLongLong(obj);
    if (value == -1 && PyErr_Occurred()) return false;
    if (value < std::numeric_limits<int32_t>::min() ||
        value > std::numeric_limits<int32_t>::max()) {
        PyErr_SetString(PyExc_OverflowError, "value outside signed 32-bit range");
        return false;
    }
    out = static_cast<int32_t>(value);
    return true;
}

template <typename T>
bool as_unsigned(PyObject* obj, T& out) {
    OwnedRef integer(PyNumber_Index(obj));
    if (!integer) return false;
    const unsigned long long value = PyLong_AsUnsignedLongLong(integer.get());
    if (PyErr_Occurred()) return false;
    if (value > std::numeric_limits<T>::max()) {
        PyErr_SetString(PyExc_OverflowError, "unsigned integer out of range");
        return false;
    }
    out = static_cast<T>(value);
    return true;
}

PyObject* ensemble_new(PyTypeObject* type, PyObject* args, PyObject* kwargs) {
    static const char* names[] = {"support", "slots", nullptr};
    PyObject* support;
    PyObject* slot_arg;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OO:Ensemble",
            const_cast<char**>(names), &support, &slot_arg)) return nullptr;
    size_t slots;
    if (!as_unsigned(slot_arg, slots)) return nullptr;

    try {
        OwnedRef iter(PyObject_GetIter(support));
        if (!iter) return nullptr;
        std::vector<int32_t> values;
        while (OwnedRef item{PyIter_Next(iter.get())}) {
            int32_t value;
            if (!as_int32(item.get(), value)) return nullptr;
            values.push_back(value);
        }
        if (PyErr_Occurred()) return nullptr;

        auto engine = std::make_unique<recastcrps::Ensemble>(std::move(values), slots);
        auto* self = reinterpret_cast<EnsembleObject*>(type->tp_alloc(type, 0));
        if (!self) return nullptr;
        self->engine = engine.release();
        return reinterpret_cast<PyObject*>(self);
    } catch (...) {
        return translate_exception();
    }
}

void ensemble_dealloc(EnsembleObject* self) {
    PyTypeObject* type = Py_TYPE(self);
    delete self->engine;
    type->tp_free(reinterpret_cast<PyObject*>(self));
    Py_DECREF(type);
}

PyObject* ensemble_set(EnsembleObject* self, PyObject* args, PyObject* kwargs) {
    static const char* names[] = {"slot", "value", "weight", nullptr};
    PyObject* slot_arg;
    PyObject* value_arg;
    PyObject* weight_arg = nullptr;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OO|O:set", const_cast<char**>(names),
            &slot_arg, &value_arg, &weight_arg)) return nullptr;
    size_t slot;
    int32_t value;
    uint32_t weight = 1;
    if (!as_unsigned(slot_arg, slot) || !as_int32(value_arg, value) ||
        (weight_arg && !as_unsigned(weight_arg, weight))) return nullptr;
    try {
        self->engine->set(slot, value, weight);
        Py_RETURN_NONE;
    } catch (...) {
        return translate_exception();
    }
}

PyObject* ensemble_erase(EnsembleObject* self, PyObject* args, PyObject* kwargs) {
    static const char* names[] = {"slot", nullptr};
    PyObject* slot_arg;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O:erase",
            const_cast<char**>(names), &slot_arg)) return nullptr;
    size_t slot;
    if (!as_unsigned(slot_arg, slot)) return nullptr;
    try {
        self->engine->erase(slot);
        Py_RETURN_NONE;
    } catch (...) {
        return translate_exception();
    }
}

bool parse_observation(PyObject* args, PyObject* kwargs, const char* format, int32_t& value) {
    static const char* names[] = {"observation", nullptr};
    PyObject* observation_arg;
    return PyArg_ParseTupleAndKeywords(args, kwargs, format,
               const_cast<char**>(names), &observation_arg) && as_int32(observation_arg, value);
}

PyObject* ensemble_score(EnsembleObject* self, PyObject* args, PyObject* kwargs) {
    int32_t value;
    if (!parse_observation(args, kwargs, "O:score", value)) return nullptr;
    try {
        return PyFloat_FromDouble(static_cast<double>(self->engine->score(value)));
    } catch (...) {
        return translate_exception();
    }
}

PyObject* ensemble_leave_one_out(EnsembleObject* self, PyObject* args, PyObject* kwargs) {
    int32_t observation;
    if (!parse_observation(args, kwargs, "O:leave_one_out", observation)) return nullptr;
    try {
        const auto scores = self->engine->leave_one_out(observation);
        if (scores.size() > static_cast<size_t>(PY_SSIZE_T_MAX)) {
            PyErr_SetString(PyExc_OverflowError, "too many slots for a Python list");
            return nullptr;
        }
        const auto slots = static_cast<Py_ssize_t>(scores.size());
        OwnedRef result(PyList_New(slots));
        if (!result) return nullptr;
        for (Py_ssize_t slot = 0; slot < slots; ++slot) {
            const double value = scores[static_cast<size_t>(slot)];
            PyObject* item;
            if (std::isnan(value)) {
                item = Py_None;
                Py_INCREF(item);
            } else {
                item = PyFloat_FromDouble(value);
                if (!item) return nullptr;
            }
            PyList_SET_ITEM(result.get(), slot, item);
        }
        return result.release();
    } catch (...) {
        return translate_exception();
    }
}

PyObject* ensemble_withdrawal_extremes(EnsembleObject* self, PyObject* args, PyObject* kwargs) {
    int32_t observation;
    if (!parse_observation(args, kwargs, "O:withdrawal_extremes", observation)) return nullptr;
    try {
        const auto extremes = self->engine->withdrawal_extremes(observation);
        return Py_BuildValue("((ld)(ld))",
            static_cast<long>(extremes.first.first), static_cast<double>(extremes.first.second),
            static_cast<long>(extremes.second.first), static_cast<double>(extremes.second.second));
    } catch (...) {
        return translate_exception();
    }
}

PyObject* ensemble_count(EnsembleObject* self, void*) {
    return PyLong_FromSize_t(self->engine->count());
}

PyObject* ensemble_total_weight(EnsembleObject* self, void*) {
    return PyLong_FromUnsignedLongLong(self->engine->total_weight());
}

PyMethodDef methods[] = {
    {"set", reinterpret_cast<PyCFunction>(ensemble_set), METH_VARARGS | METH_KEYWORDS,
        "set($self, slot, value, weight=1)\n--\n\nInsert or replace a weighted member."},
    {"erase", reinterpret_cast<PyCFunction>(ensemble_erase), METH_VARARGS | METH_KEYWORDS,
        "erase($self, slot)\n--\n\nRemove a member."},
    {"score", reinterpret_cast<PyCFunction>(ensemble_score), METH_VARARGS | METH_KEYWORDS,
        "score($self, observation)\n--\n\nCompute the current weighted empirical CRPS."},
    {"leave_one_out", reinterpret_cast<PyCFunction>(ensemble_leave_one_out),
        METH_VARARGS | METH_KEYWORDS,
        "leave_one_out($self, observation)\n--\n\n"
        "Return each slot's score after withdrawing its member. Inactive slots or withdrawals "
        "that leave no members have value None."},
    {"withdrawal_extremes", reinterpret_cast<PyCFunction>(ensemble_withdrawal_extremes),
        METH_VARARGS | METH_KEYWORDS,
        "withdrawal_extremes($self, observation)\n--\n\n"
        "Return ((value, lowest score after withdrawal), (value, highest score after withdrawal)). "
        "Ties favor the smallest forecast value. Requires at least two active unit-weight members."},
    {nullptr, nullptr, 0, nullptr}
};

PyGetSetDef properties[] = {
    {"count", reinterpret_cast<getter>(ensemble_count), nullptr, "Active members.", nullptr},
    {"total_weight", reinterpret_cast<getter>(ensemble_total_weight), nullptr,
        "Sum of active member weights.", nullptr},
    {nullptr, nullptr, nullptr, nullptr, nullptr}
};

PyType_Slot type_slots[] = {
    {Py_tp_new, reinterpret_cast<void*>(ensemble_new)},
    {Py_tp_dealloc, reinterpret_cast<void*>(ensemble_dealloc)},
    {Py_tp_doc, const_cast<char*>("Ensemble(support, slots)\n--\n\nA mutable ensemble with a fixed member-value support.")},
    {Py_tp_methods, methods},
    {Py_tp_getset, properties},
    {0, nullptr}
};

PyType_Spec type_spec = {
    "recastcrps.Ensemble", sizeof(EnsembleObject), 0, Py_TPFLAGS_DEFAULT, type_slots
};

PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "_core", "Native weighted CRPS implementation.", 0,
    nullptr, nullptr, nullptr, nullptr, nullptr
};
}  // namespace

PyMODINIT_FUNC PyInit__core() {
    OwnedRef result(PyModule_Create(&module));
    if (!result) return nullptr;
    OwnedRef type(PyType_FromSpec(&type_spec));
    if (!type) return nullptr;
    if (PyModule_AddObject(result.get(), "Ensemble", type.get()) < 0) return nullptr;
    type.release();
    return result.release();
}
