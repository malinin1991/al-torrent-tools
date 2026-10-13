#!/bin/sh
unset PYTHON_GIL
exec /usr/local/bin/python "$@"
