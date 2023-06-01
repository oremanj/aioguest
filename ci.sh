#!/bin/bash

set -ex -o pipefail

# Log some general info about the environment
uname -a
env | sort

################################################################
# We have a Python environment!
################################################################

python -c "import sys, struct, ssl; print('#' * 70); print('python:', sys.version); print('version_info:', sys.version_info); print('bits:', struct.calcsize('P') * 8); print('openssl:', ssl.OPENSSL_VERSION, ssl.OPENSSL_VERSION_INFO); print('#' * 70)"

python -m pip install -U pip setuptools wheel
python -m pip --version

python setup.py sdist --formats=zip
python -m pip install dist/*.zip

if [ "$CHECK_FORMATTING" = "1" ]; then
    python -m pip install -r test-requirements.txt
    source check.sh
else
    # Actual tests
    python -m pip install -r test-requirements.txt

    # We run the tests from inside an empty directory, to make sure Python
    # doesn't pick up any .py files from our working dir.
    mkdir empty || true
    cd empty

    INSTALLDIR=$(python -c "import os, aioguest; print(os.path.dirname(aioguest.__file__))")
    cp ../pyproject.toml $INSTALLDIR

    pypy=$(python -c "import sys; print(sys.implementation.name)" | grep pypy || true)
    if pytest -r a --junitxml=../test-results.xml ../tests --cov --cov-report=xml --cov-config=../.coveragerc$pypy --verbose; then
        PASSED=true
    else
        PASSED=false
    fi

    $PASSED
fi
