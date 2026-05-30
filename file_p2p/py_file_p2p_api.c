#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <pthread.h>
#include <stdio.h>
#include <time.h>
#include "file_p2p_api.h"

static PyObject *py_read_file(PyObject *self, PyObject *args)
{
    int dev_fd = 0;
    const char *file_name = NULL;
    const char *bdev_name = NULL;
    unsigned long bdev_offset = 0;
    unsigned short devid = 0;
    unsigned short vfid = 0;
    unsigned int size = 0;
    unsigned long addr = 0;
    int ret = 0;

    if (!PyArg_ParseTuple(args, "isskkIHH", &dev_fd, &file_name, &bdev_name, &bdev_offset, &addr, &size, &devid, &vfid)) {
        return NULL;
    }

    struct read_parameter param = {
        .file_name = file_name,
        .bdev_name = bdev_name,
        .bdev_offset = bdev_offset,
        .devid = devid,
        .vfid = vfid,
        .size = size,
        .addr = addr,
    };

    Py_BEGIN_ALLOW_THREADS
    ret = read_file(dev_fd, &param);
    Py_END_ALLOW_THREADS

    return PyLong_FromLong((long)ret);
}

static PyObject *py_read_file_batch(PyObject *self, PyObject *args)
{
    int dev_fd = 0;
    const char *file_name = NULL;
    const char *bdev_name = NULL;
    unsigned short devid = 0;
    unsigned short vfid = 0;
    PyObject *py_list = NULL;
    int ret = 0;

    if (!PyArg_ParseTuple(args, "issO", &dev_fd, &file_name, &bdev_name, &py_list)) {
        return PyLong_FromLong((long)-1);
    }

    if (!PyList_Check(py_list)) {
        PyErr_SetString(PyExc_TypeError, "third arg must be a list");
        return PyLong_FromLong((long)-1);
    }

    Py_ssize_t n = PyList_Size(py_list);
    if (n == 0) {
        PyErr_SetString(PyExc_TypeError, "third arg must be a non-empty list");
        return PyLong_FromLong((long)-1);
    }

    struct read_parameter *params = malloc(n * sizeof(struct read_parameter));
    if (params == NULL) {
        PyErr_SetString(PyExc_MemoryError, "malloc read_parameter failed");
        return PyLong_FromLong((long)-1);
    }

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *py_item = PyList_GetItem(py_list, i);
        if (!PyTuple_Check(py_item) && !PyList_Check(py_item)) {
            PyErr_SetString(PyExc_TypeError, "third arg must be a list/tuple of at least 3 elements (bdev_offset, addr, size)");
            free(params);
            return PyLong_FromLong((long)-1);
        }

        PyObject *seq = PySequence_Fast(py_item, "entry must be a sequence");
        if (seq == NULL) {
            free(params);
            return PyLong_FromLong((long)-1);
        }

        Py_ssize_t len = PySequence_Fast_GET_SIZE(seq);
        if (len < 3) {
            Py_DECREF(seq);
            PyErr_SetString(PyExc_TypeError, "entry must be a sequence of at least 3 elements (bdev_offset, addr, size)");
            free(params);
            return PyLong_FromLong((long)-1);
        }

        PyObject **items = PySequence_Fast_ITEMS(seq);

        params[i].file_name = file_name;
        params[i].bdev_name = bdev_name;
        params[i].devid = devid;
        params[i].vfid = vfid;
        params[i].bdev_offset = PyLong_AsUnsignedLongLong(items[0]);
        params[i].addr = PyLong_AsUnsignedLongLong(items[1]);
        params[i].size = (unsigned int)PyLong_AsUnsignedLongLong(items[2]);

        Py_DECREF(seq);
    }

    Py_BEGIN_ALLOW_THREADS
    ret = read_file_batch(dev_fd, params, (int)n);
    Py_END_ALLOW_THREADS

    free(params);
    return PyLong_FromLong((long)ret);
}

static PyObject *py_drain_read(PyObject *self, PyObject *args)
{
    int dev_fd = 0;
    int ret = 0;

    if (!PyArg_ParseTuple(args, "i", &dev_fd)) {
        return NULL;
    }

    ret = drain_read(dev_fd);

    return PyLong_FromLong((long)ret);
}

static PyObject *py_new_p2p_fd(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    int ret = new_p2p_fd();

    return PyLong_FromLong((long)ret);
}

static PyObject *py_close_p2p_fd(PyObject *self, PyObject *args)
{
    int dev_fd = 0;
    int ret = 0;

    if (!PyArg_ParseTuple(args, "i", &dev_fd)) {
        return NULL;
    }

    ret = close_p2p_fd(dev_fd);

    Py_RETURN_NONE;
}

static PyMethodDef FileP2PMethods[] = {
    {
        "read_file",
        py_read_file,
        METH_VARARGS,
        "read_file(dev_fd, file_name, bdev_name, bdev_offset, addr, size, devid, vfid) -> int\n\n"
        "Read file from p2p device.\n"
        "\n"
        "Parameters:\n"
        "    dev_fd (int): File descriptor of p2p device.\n"
        "    file_name (str): Name of file to read.\n"
        "    bdev_name (str): Name of block device to read.\n"
        "    bdev_offset (int): Offset in block device to read.\n"
        "    addr (int): Address in host memory to read.\n"
        "    size (int): Size in bytes to read.\n"
        "    devid (int): Device ID.\n"
        "    vfid (int): Virtual function ID.\n"
        "\n"
        "Returns:\n"
        "    int: 0 on success, non-zero on error.\n"
    },
    {
        "read_file_batch",
        py_read_file_batch,
        METH_VARARGS,
        "read_file_batch(dev_fd, file_name, bdev_name, bdev_offset, addr, size, devid, vfid) -> int\n\n"
        "Read file batch from p2p device.\n"
        "\n"
        "Parameters:\n"
        "    dev_fd (int): File descriptor of p2p device.\n"
        "    file_name (str): Name of file to read.\n"
        "    bdev_name (str): Name of block device to read.\n"
        "    bdev_offset (int): Offset in block device to read.\n"
        "    addr (int): Address in host memory to read.\n"
        "    size (int): Size in bytes to read.\n"
        "    devid (int): Device ID.\n"
        "    vfid (int): Virtual function ID.\n"
        "\n"
        "Returns:\n"
        "    int: 0 on success, non-zero on error.\n"
    },
    {
        "drain_read",
        py_drain_read,
        METH_VARARGS,
        "drain_read(dev_fd) -> int\n\n"
        "Drain read from p2p device.\n"
        "\n"
        "Parameters:\n"
        "    dev_fd (int): File descriptor of p2p device.\n"
        "\n"
        "Returns:\n"
        "    int: 0 on success, non-zero on error.\n"
    },
    {
        "new_p2p_fd",
        py_new_p2p_fd,
        METH_NOARGS,
        "new_p2p_fd() -> int\n\n"
        "New p2p device file descriptor.\n"
        "\n"
        "Returns:\n"
        "    int: File descriptor of p2p device on success, -1 on error.\n"
    },
    {
        "close_p2p_fd",
        py_close_p2p_fd,
        METH_VARARGS,
        "close_p2p_fd(dev_fd) -> None\n\n"
        "Close p2p device file descriptor.\n"
        "\n"
        "Parameters:\n"
        "    dev_fd (int): File descriptor of p2p device.\n"
        "\n"
        "Returns:\n"
        "    None\n"
    },
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef file_p2p_module = {
    PyModuleDef_HEAD_INIT,
    "file_p2p",
    "p2p file access",
    -1,
    FileP2PMethods,
};

PyMODINIT_FUNC PyInit_file_p2p(void)
{
    return PyModule_Create(&file_p2p_module);
}