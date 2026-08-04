#!/bin/bash

# Get to a predictable directory, the directory of this script
cd "$(dirname "$0")"

[ -e environment ] && . ./environment

if [[ -n "${VENV_DIR}" ]]; then
    echo "Activating virtual environment in ${VENV_DIR}."
    python3 -m venv "${VENV_DIR}"
    . "${VENV_DIR}/bin/activate"
fi

bootstrap_cairo() {
    if [[ "${TLE_CAIRO_BOOTSTRAP:-1}" == "0" ]]; then
        return
    fi

    local cairo_exports
    if cairo_exports="$(poetry run python -m tle.util.cairo_bootstrap)"; then
        while IFS= read -r cairo_export; do
            case "${cairo_export}" in
                LD_LIBRARY_PATH=*|PKG_CONFIG_PATH=*|TLE_ALLOW_COLOR_EMOJI=1)
                    export "${cairo_export}"
                    ;;
                "")
                    ;;
                *)
                    echo "Ignoring unexpected Cairo bootstrap output: ${cairo_export}" >&2
                    ;;
            esac
        done <<< "${cairo_exports}"
    else
        echo "Cairo bootstrap helper failed; continuing with default Cairo." >&2
    fi
}

while true; do
    git pull
    poetry install
    bootstrap_cairo
    export TLE_ALLOW_COLOR_EMOJI=0
    poetry run python -m tle

    echo '==================================================================='
    echo '=                       Restarting                                ='
    echo '==================================================================='
done
