#!/bin/sh
unset PYTHON_GIL
LD_LIBRARY_PATH=/opt/python315regular/lib exec /opt/python315regular/bin/python3.15 "$@"
